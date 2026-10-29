"""Shared HTML parsing utilities for all portal adapters.

All adapters use lxml as the backing parser for speed and strict error
reporting.  BeautifulSoup is the API layer; we never use the built-in
html.parser because it silently swallows malformed markup.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from urja.client.exceptions import PortalShapeError


def make_soup(html: str) -> BeautifulSoup:
    """Parse *html* with the lxml backend."""
    return BeautifulSoup(html, "lxml")


def require(tag: Tag | None, description: str) -> Tag:
    """Return *tag* or raise :exc:`PortalShapeError` if it is None.

    Args:
        tag: Result of a BeautifulSoup find call.
        description: Human-readable name for the missing element
            (used in the exception message and ``field`` attribute).
    """
    if tag is None:
        raise PortalShapeError(
            f"Portal HTML is missing expected element: {description!r}. "
            "The portal may have been updated.",
            field=description,
        )
    return tag


def cell_text(tag: Tag | None) -> str:
    """Return stripped inner text of *tag*, or empty string if None."""
    if tag is None:
        return ""
    return tag.get_text(strip=True)


def parse_table_rows(
    table: Tag,
    expected_headers: list[str],
) -> list[dict[str, str]]:
    """Parse an HTML ``<table>`` into a list of ``{header: cell_text}`` dicts.

    Validates that the table contains *at least* the columns listed in
    *expected_headers* (case-insensitive, stripped).  Extra columns are
    preserved.  Raises :exc:`PortalShapeError` if any required column is
    absent.

    Args:
        table: The ``<table>`` Tag from BeautifulSoup.
        expected_headers: Column headers that must be present.

    Returns:
        List of row dicts; keys are the normalised (lower-stripped) header names.
    """
    # Collect headers
    header_row = table.find("thead")
    th_tags = (
        header_row.find_all("th") if header_row else table.find("tr").find_all(["th", "td"])  # type: ignore[union-attr]
    )

    raw_headers = [cell_text(th) for th in th_tags]
    norm_headers = [h.lower().strip() for h in raw_headers]

    for expected in expected_headers:
        if expected.lower() not in norm_headers:
            raise PortalShapeError(
                f"Table is missing expected column {expected!r}. "
                f"Found: {raw_headers}. Portal may have changed.",
                field=expected,
            )

    rows: list[dict[str, str]] = []
    tbody = table.find("tbody")
    tr_iter = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
    for tr in tr_iter:
        cells = tr.find_all(["td", "th"])
        row = {norm_headers[i]: cell_text(c) for i, c in enumerate(cells) if i < len(norm_headers)}
        if row:
            rows.append(row)
    return rows


# ── String normalisation helpers ──────────────────────────────────────


def normalise_meter_id(raw: str) -> str:
    """Return uppercase-stripped meter ID.  Flags nothing — IDs are opaque."""
    return raw.strip().upper()


def normalise_make(raw: str) -> str:
    """Unescape HTML entity ``L&amp;T`` → ``L&T`` and strip whitespace."""
    return raw.replace("&amp;", "&").replace("&AMP;", "&").strip()


_WHITESPACE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """Replace runs of whitespace (including newlines) with a single space."""
    return _WHITESPACE.sub(" ", text).strip()


def parse_float_flag(
    raw: str,
    field: str,
    flags: list[str],
) -> float | None:
    """Parse *raw* as float; append *field*_unparseable to *flags* and return
    None on failure instead of raising.
    """
    try:
        return float(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        flags.append(f"{field}_unparseable")
        return None


def _safe_find(parent: Tag, *args: Any, **kwargs: Any) -> Tag | None:
    """Thin wrapper around Tag.find that never raises."""
    try:
        return parent.find(*args, **kwargs)  # type: ignore[return-value]
    except Exception:
        return None
