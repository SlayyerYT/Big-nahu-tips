"""Entry point for hosting panels.

Most Discord bot hosts (Pella, Railway, Replit, bot-hosting.net) either run
`main.py` by default or let you set a start command. This file exists so the
default works; `python bot.py` remains equivalent.

Configuration comes from the host's environment variables when they are set, and
from a .env file otherwise - so either style of panel works without code changes.
"""
from __future__ import annotations

import sys

# Printed before anything else can fail, to both streams, unbuffered. If a host's
# console shows nothing at all, this line distinguishes "the code crashed" from
# "the code never ran" - the two need completely different fixes.
print("[boot] main.py reached, python", sys.version.split()[0], flush=True)
print("[boot] stderr alive", file=sys.stderr, flush=True)

MIN_PYTHON = (3, 10)

if sys.version_info < MIN_PYTHON:
    have = ".".join(str(n) for n in sys.version_info[:3])
    need = ".".join(str(n) for n in MIN_PYTHON)
    sys.exit(
        f"This bot needs Python {need} or newer, but this host is running {have}.\n"
        f"Pick a newer Python version in your hosting panel."
    )

from bot import main  # noqa: E402  (must come after the version check)

if __name__ == "__main__":
    sys.exit(main())
