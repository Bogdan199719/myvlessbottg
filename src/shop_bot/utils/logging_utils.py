"""Logging filters shared by the production entrypoint."""

from __future__ import annotations

import logging
import re


_URL_WITH_PATH_RE = re.compile(
    r"(?P<origin>https?://[^/\s'\"<>]+)(?P<path>/[^\s'\"<>]*)",
    re.IGNORECASE,
)


class SensitiveUrlFilter(logging.Filter):
    """Hide URL paths, which may contain panel tokens, from emitted logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _URL_WITH_PATH_RE.sub(r"\g<origin>/<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
