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
