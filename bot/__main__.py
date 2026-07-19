"""Entry point: python -m bot"""

from __future__ import annotations

import logging

from .client import RaidClient
from .config import require_token

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)


def main() -> None:
    bot = RaidClient()
    try:
        bot.run(require_token(), log_handler=None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
