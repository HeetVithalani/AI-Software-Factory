"""
factory.__main__
================
Factory kernel boot entry point.

Run with: python -m factory

Boot sequence:
1. Load configuration (YAML + environment)
2. Initialize / migrate the database
3. Load/create kernel state
4. Check for emergency stop sentinel
5. Discover installed AI models (Ollama)
6. Print status summary
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def _setup_logging(level: str = "WARNING") -> None:
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        level=getattr(logging, level.upper(), logging.WARNING),
        stream=sys.stderr,
    )


def main() -> int:
    from factory.cli.main import main as cli_main
    return cli_main()

if __name__ == "__main__":
    sys.exit(main())
