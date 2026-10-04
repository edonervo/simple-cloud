"""`python3 -m cloudsync` — the entry point that makes the exit codes observable to a shell."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
