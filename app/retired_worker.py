from __future__ import annotations

import asyncio
import logging
import signal


RETIREMENT_VERSION = "canonical-archive-20260906"
logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info(
        "Pattern Workbench worker retired; database connections and job claims are disabled (%s)",
        RETIREMENT_VERSION,
    )
    await stop.wait()


if __name__ == "__main__":
    asyncio.run(run())
