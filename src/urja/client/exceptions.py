"""Exceptions for Urja portal client."""

from __future__ import annotations


class PortalError(Exception):
    """Base exception for portal client operations."""


class ReadOnlyGuardError(PortalError):
    """Raised when an attempt is made to call non-GET methods on the portal."""


class AuthenticationError(PortalError):
    """Raised when authentication fails or credentials are invalid."""


class PortalRequestError(PortalError):
    """Raised when a request to the portal fails after exhausting retries."""


class PortalTimeoutError(PortalRequestError):
    """Raised when portal requests repeatedly time out."""


class PortalUnavailableError(PortalRequestError):
    """Raised when portal is unreachable or repeatedly returns 5xx."""


class PortalShapeError(PortalError):
    """Raised when portal HTML structure doesn't match known format.

    This means the portal may have been updated/redesigned.
    The ``field`` attribute names the specific element or column that was missing
    or unparseable so callers can triage quickly.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field
