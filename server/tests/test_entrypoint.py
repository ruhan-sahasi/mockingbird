"""The container entrypoint refuses to start while a secret is missing or still Terraform's
placeholder. Every SSM parameter starts as the public string "unset" (infra/secrets.tf), so a
missed put-parameter must stop the task, not serve with a guessable owner key or path secret."""

import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "scripts" / "entrypoint.sh"

# What ECS injects from SSM, with a distinct value each, so a test can tell whether any value
# was printed.
SECRETS = {
    "ANTHROPIC_API_KEY": "real-anthropic-key-value",
    "VAPI_API_KEY": "real-vapi-key-value",
    "DATABASE_URL": "postgresql://mb:real-db-password@db.test:5432/postgres",
    "BRAIN_SECRET": "real-brain-secret-value",
    "WEBHOOK_SECRET": "real-webhook-secret-value",
    "OWNER_KEY": "real-owner-key-value",
    "VISITOR_SALT": "real-visitor-salt-value",
}
# DATABASE_URL may be absent: without it the server keeps interviews in memory.
REQUIRED = [name for name in SECRETS if name != "DATABASE_URL"]

MIGRATE = "run python -m mockingbird.migrate"


class Entrypoint:
    """Runs scripts/entrypoint.sh with a stand-in CMD and a fake `uv` that only logs its
    arguments, so a test sees whether the migration and the command ran."""

    def __init__(self, tmp_path: Path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_uv = bin_dir / "uv"
        fake_uv.write_text('#!/bin/sh\necho "$@" >> "$FAKE_UV_LOG"\n')
        fake_uv.chmod(0o755)
        self.uv_log = tmp_path / "uv.log"
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def run(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(ENTRYPOINT), "sh", "-c", "echo command-ran"],
            env={"PATH": self.path, "FAKE_UV_LOG": str(self.uv_log), **env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def uv_calls(self) -> list[str]:
        return self.uv_log.read_text().splitlines() if self.uv_log.exists() else []

    def assert_refused(self, result: subprocess.CompletedProcess[str], name: str) -> None:
        assert result.returncode == 1
        assert "command-ran" not in result.stdout
        assert self.uv_calls() == []  # refused before migrating
        assert f"entrypoint: {name} " in result.stderr
        for value in SECRETS.values():
            assert value not in result.stdout + result.stderr


@pytest.fixture
def entrypoint(tmp_path: Path) -> Entrypoint:
    return Entrypoint(tmp_path)


def test_migrates_then_runs_the_command_when_every_secret_is_set(entrypoint):
    result = entrypoint.run(SECRETS)
    assert result.returncode == 0
    assert result.stdout.strip() == "command-ran"
    assert entrypoint.uv_calls() == [MIGRATE]


def test_skips_the_migration_without_a_database_url(entrypoint):
    env = {name: value for name, value in SECRETS.items() if name != "DATABASE_URL"}
    result = entrypoint.run(env)
    assert result.returncode == 0
    assert result.stdout.strip() == "command-ran"
    assert entrypoint.uv_calls() == []


@pytest.mark.parametrize("name", list(SECRETS))
def test_refuses_to_start_while_a_secret_is_still_the_placeholder(entrypoint, name):
    result = entrypoint.run({**SECRETS, name: "unset"})
    entrypoint.assert_refused(result, name)


@pytest.mark.parametrize("name", REQUIRED)
def test_refuses_to_start_when_a_required_secret_is_empty(entrypoint, name):
    result = entrypoint.run({**SECRETS, name: ""})
    entrypoint.assert_refused(result, name)


@pytest.mark.parametrize("name", REQUIRED)
def test_refuses_to_start_when_a_required_secret_is_missing(entrypoint, name):
    env = {key: value for key, value in SECRETS.items() if key != name}
    entrypoint.assert_refused(entrypoint.run(env), name)
