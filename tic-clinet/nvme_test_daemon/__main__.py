"""入口: python -m nvme_test_daemon"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    settings = Settings.load()
    uvicorn.run(
        "nvme_test_daemon.daemon:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
