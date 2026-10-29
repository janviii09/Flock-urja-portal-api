"""Entrypoint for running Urja sync from the command line: `python -m urja.sync`."""

from __future__ import annotations

import argparse
import asyncio
import logging

from urja.client.portal import PortalClient
from urja.logging import setup_logging
from urja.settings import settings
from urja.store.repository import Store
from urja.sync.engine import SyncEngine


async def _run(sync_energy: bool, energy_limit: int | None, max_pages: int | None):
    setup_logging()
    logger = logging.getLogger("urja.sync")
    logger.info("Initializing Urja synchronization against %s", settings.urja_base_url)

    store = Store(settings.db_path)
    engine = SyncEngine(store)

    async with PortalClient() as client:
        run = await engine.sync_all(
            client=client,
            max_pages=max_pages,
            sync_energy=sync_energy,
            energy_limit=energy_limit,
        )
        logger.info(
            "Sync completed: status=%s, new=%d, changed=%d, failed=%d",
            run.status.value,
            run.items_new,
            run.items_changed,
            run.items_failed,
        )


def main():
    parser = argparse.ArgumentParser(description="Synchronize Urja Meter Ops data to SQLite.")
    parser.add_argument("--no-energy", action="store_true", help="Skip energy readings sync.")
    parser.add_argument(
        "--energy-limit",
        type=int,
        default=None,
        help="Limit number of meters for energy telemetry sync (default: all).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Max pages if falling back to paginated search.",
    )
    args = parser.parse_args()

    asyncio.run(
        _run(
            sync_energy=not args.no_energy,
            energy_limit=args.energy_limit,
            max_pages=args.max_pages,
        )
    )


if __name__ == "__main__":
    main()
