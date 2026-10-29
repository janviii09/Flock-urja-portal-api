"""Client (transport) layer — portal HTTP session management."""

from __future__ import annotations

from urja.client.exceptions import (
    AuthenticationError,
    PortalError,
    PortalRequestError,
    PortalShapeError,
    PortalTimeoutError,
    PortalUnavailableError,
    ReadOnlyGuardError,
)
from urja.client.portal import PortalClient

__all__ = [
    "AuthenticationError",
    "PortalClient",
    "PortalError",
    "PortalRequestError",
    "PortalShapeError",
    "PortalTimeoutError",
    "PortalUnavailableError",
    "ReadOnlyGuardError",
]
