"""Centralised logging configuration with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from typing import ClassVar

from urja.settings import settings


class SecretRedactingFilter(logging.Filter):
    """Filters log records to prevent passwords, session tokens, and secrets from leaking into logs.

    Redacts:
    - Passwords in key-value strings (e.g. password=..., "password": "...")
    - Session cookies & tokens (e.g. session=..., urja_session=...)
    - Authorization headers (e.g. Bearer ..., Basic ...)
    - The actual configured URJA_PASSWORD if present and non-empty.
    """

    _PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r'((?:password|secret)["\']?\s*[:=]\s*["\'])([^"\']+)(["\'])', re.IGNORECASE),
        re.compile(
            r'((?:session|token|auth)["\']?\s*[:=]\s*["\'])([^"\'\s]{8,})(["\'])', re.IGNORECASE
        ),
        re.compile(r"(Bearer\s+)[A-Za-z0-9\-_\.=]+", re.IGNORECASE),
        re.compile(r"(Basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE),
    ]

    def __init__(self, name: str = "", extra_secret: str | None = None) -> None:
        super().__init__(name)
        self.extra_secret = extra_secret

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True

    def _redact(self, text: str) -> str:
        for pat in self._PATTERNS:
            text = pat.sub(
                r"\1***REDACTED***\3" if r"\3" in pat.pattern else r"\1***REDACTED***", text
            )
        if self.extra_secret and len(self.extra_secret) > 2 and self.extra_secret in text:
            text = text.replace(self.extra_secret, "***REDACTED***")
        return text


def setup_logging() -> None:
    """Configure root logger with a clean, readable format and secret scrubbing."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Attach secret redactor filter to handler
    configured_pwd = settings.urja_password if settings.urja_password else None
    handler.addFilter(SecretRedactingFilter(extra_secret=configured_pwd))

    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers to prevent duplicate logging or un-filtered handlers
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
