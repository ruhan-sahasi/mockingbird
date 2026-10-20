"""Values that tie files together with no code between them, read from the files themselves.

Each pair here fails at deploy time or under load, not in any other test, when one side changes
and the other does not: the keep-alive gives intermittent 502s, and a secret name that does not
match leaves a setting empty or a placeholder unchecked.
"""

import json
import re
from pathlib import Path

from mockingbird.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_uvicorn_keeps_an_idle_connection_open_longer_than_the_alb_does():
    # The ALB reuses an idle connection to the task for up to idle_timeout seconds. If uvicorn
    # closes it first, the next request the ALB sends down it gets a 502.
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", read("Dockerfile"), re.MULTILINE).group(1))
    keep_alive = int(cmd[cmd.index("--timeout-keep-alive") + 1])
    idle = int(re.search(r"^\s*idle_timeout\s*=\s*(\d+)", read("infra/alb.tf"), re.M).group(1))

    assert keep_alive > idle
    assert "--no-access-log" in cmd  # the access log would record the path secrets and tokens


def secret_names_in_terraform() -> set[str]:
    block = re.search(r"secret_names = toset\(\[(.*?)\]\)", read("infra/secrets.tf"), re.S)
    return set(re.findall(r'"([A-Z_]+)"', block.group(1)))


def secret_names_in_the_entrypoint() -> set[str]:
    loop = re.search(r"^for name in (.*); do$", read("scripts/entrypoint.sh"), re.MULTILINE)
    return set(loop.group(1).split())


def test_terraform_the_entrypoint_and_settings_name_the_same_secrets():
    # Every text setting is a secret except PUBLIC_BASE_URL, which ecs.tf sets in the open. The
    # numbers have defaults and are not secrets either.
    settings = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if field.annotation is not int and name != "public_base_url"
    }

    assert secret_names_in_terraform() == settings
    assert secret_names_in_the_entrypoint() == settings
    assert re.search(r'name = "PUBLIC_BASE_URL", value = ', read("infra/ecs.tf"))


def test_env_example_lists_every_setting():
    listed = set(re.findall(r"^([A-Z_]+)=", read(".env.example"), re.MULTILINE))
    assert listed == {name.upper() for name in Settings.model_fields}
