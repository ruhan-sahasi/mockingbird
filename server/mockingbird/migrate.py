"""Apply the SQL files in server/migrations/ to the database, each one once, in name order.

    DATABASE_URL=postgresql://... python -m mockingbird.migrate

Each file runs in its own transaction together with the row that records it in
`schema_migrations`, so a file either lands completely and is recorded, or does not land at all.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

# server/migrations sits next to the package, not inside it, so find it from this file rather
# than from the working directory.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# An arbitrary number, the same in every process, naming the lock that makes migrators take turns.
_LOCK_KEY = 7_262_019


async def apply_migrations(
    conn: asyncpg.Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Run every file in `migrations_dir` that has not run yet. Returns the names it ran."""
    applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        async with conn.transaction():
            # The ECS service never runs two tasks at once (minimum healthy 0%, maximum 100%),
            # but two runners can still meet, for example a manual migrate during a deploy.
            # This lock makes the second one wait, then find the file already applied.
            # It lasts until the transaction ends, so it also works through a transaction
            # pooler (a session-level lock would not).
            await conn.execute("select pg_advisory_xact_lock($1)", _LOCK_KEY)
            await conn.execute(
                "create table if not exists schema_migrations ("
                " name text primary key, applied_at timestamptz not null default now());"
                # Same reason as in 001_init.sql: keep Supabase's REST API out of our tables.
                " alter table schema_migrations enable row level security"
            )
            if await conn.fetchval("select 1 from schema_migrations where name = $1", path.name):
                continue
            await conn.execute(path.read_text())
            await conn.execute("insert into schema_migrations (name) values ($1)", path.name)
            applied.append(path.name)
            log.info("applied migration %s", path.name)
    return applied


async def migrate(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Connect to `dsn`, apply what is pending, and disconnect. Returns the names it ran."""
    # statement_cache_size=0 for the same reason as store.create_pool: it keeps this working
    # through Supabase's transaction pooler.
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        return await apply_migrations(conn, migrations_dir)
    finally:
        await conn.close()


def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set, so there is no database to migrate.")
    logging.basicConfig(level=logging.INFO)
    applied = asyncio.run(migrate(dsn))
    print(
        f"applied {len(applied)} migration(s): {', '.join(applied) or 'none, already up to date'}"
    )


if __name__ == "__main__":
    main()
