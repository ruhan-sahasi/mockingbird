#!/bin/sh
# The container's entrypoint: bring the database schema up to date, then run CMD (uvicorn).
#
# create_app does not migrate, so this is where Supabase gets its tables. Without DATABASE_URL
# the server keeps interviews in memory and there is nothing to migrate. If a migration fails,
# the container exits instead of serving on a half-applied schema.
set -eu

if [ -n "${DATABASE_URL:-}" ]; then
  uv run python -m mockingbird.migrate
fi

# exec, so uvicorn (through uv) gets ECS's stop signal directly and can shut down cleanly.
exec "$@"
