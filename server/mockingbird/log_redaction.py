"""Keep URL query strings out of the logs.

`--no-access-log` stops uvicorn's access log, but uvicorn still logs every WebSocket accept on
`uvicorn.error` with the path and its query string: `/ws/interview/{id}?token=...`. The token
is single use and expires after 60 s, so this is hygiene, not an active leak. The filter turns
every query string into `?[redacted]` before any handler sees the record."""

import logging
import re

LOGGERS = ("uvicorn.error", "uvicorn.access", "uvicorn")
_QUERY = re.compile(r"\?[^\s\"']+")
REDACTED = "?[redacted]"


def _redact(text: str) -> str:
    return _QUERY.sub(REDACTED, text)


class RedactQueryStrings(logging.Filter):
    """Rewrites every URL query string in a record to `?[redacted]`. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in args)
        elif not args and isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        return True


def install_log_redaction() -> None:
    """Add the filter to uvicorn's loggers. Calling it again adds nothing."""
    for name in LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactQueryStrings) for f in logger.filters):
            logger.addFilter(RedactQueryStrings())
