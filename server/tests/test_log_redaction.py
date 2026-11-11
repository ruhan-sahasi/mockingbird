"""Query strings stay out of the logs: uvicorn logs the interview socket's path with its
one-time token (`/ws/interview/{id}?token=...`) on every accept."""

import asyncio
import logging
import socket

import pytest
import uvicorn
import websockets

from mockingbird.app import create_app
from mockingbird.log_redaction import LOGGERS, RedactQueryStrings, install_log_redaction
from mockingbird.store import MemoryStore
from tests.conftest import test_settings

ACCEPT = '%s - "WebSocket %s" [accepted]'


def make_record(msg, args):
    return logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)


@pytest.fixture
def clean_loggers():
    saved = {name: list(logging.getLogger(name).filters) for name in LOGGERS}
    for name in LOGGERS:
        logging.getLogger(name).filters.clear()
    yield
    for name, filters in saved.items():
        logging.getLogger(name).filters[:] = filters


def test_filter_redacts_the_query_of_uvicorns_accept_line():
    record = make_record(ACCEPT, ("127.0.0.1:5000", "/ws/interview/abc?token=SECRET"))
    assert RedactQueryStrings().filter(record) is True
    message = record.getMessage()
    assert '"WebSocket /ws/interview/abc?[redacted]" [accepted]' in message
    assert "SECRET" not in message
    assert "127.0.0.1:5000" in message


def test_filter_redacts_a_message_with_no_args():
    record = make_record("GET /x?token=SECRET&a=b done", None)
    RedactQueryStrings().filter(record)
    assert record.getMessage() == "GET /x?[redacted] done"


@pytest.mark.parametrize(
    ("msg", "args"),
    [
        ("plain line", None),
        ("%s at %s", ("a", "/no/query")),
        ("%(a)s", ({"a": "b"},)),
        ("%d and %s", (3, "x")),
        ("%s", (None,)),
    ],
)
def test_filter_leaves_other_records_alone(msg, args):
    record = make_record(msg, args)
    expected = record.getMessage()
    assert RedactQueryStrings().filter(record) is True
    assert record.getMessage() == expected


def test_real_logging_through_uvicorn_error_is_redacted(clean_loggers, caplog):
    install_log_redaction()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logging.getLogger("uvicorn.error").info(
            ACCEPT, "127.0.0.1:5000", "/ws/interview/abc?token=SECRET"
        )
    assert "SECRET" not in caplog.text
    assert "/ws/interview/abc?[redacted]" in caplog.text


def test_install_is_idempotent(clean_loggers):
    install_log_redaction()
    install_log_redaction()
    for name in LOGGERS:
        mine = [f for f in logging.getLogger(name).filters if isinstance(f, RedactQueryStrings)]
        assert len(mine) == 1


async def test_a_real_uvicorn_accept_never_logs_the_token(clean_loggers, caplog):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(test_settings(), store=MemoryStore())  # installs the redaction
    # log_config=None keeps uvicorn from replacing its loggers' handlers, so caplog sees them.
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="info", ws="websockets"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        with caplog.at_level(logging.INFO):
            while not server.started:
                await asyncio.sleep(0.01)
            url = f"ws://127.0.0.1:{port}/ws/interview/abc?token=SECRET"
            async with websockets.connect(url) as ws:
                with pytest.raises(websockets.ConnectionClosed):
                    await ws.recv()  # the route accepts, then closes 4401 for the bad token
    finally:
        server.should_exit = True
        await serving
        sock.close()
    assert "[accepted]" in caplog.text  # it was logged, so no SECRET means something
    assert "SECRET" not in caplog.text
    assert "/ws/interview/abc?[redacted]" in caplog.text
