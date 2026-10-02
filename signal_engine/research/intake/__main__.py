"""`python -m signal_engine.research.intake` entry point."""

import sys

from signal_engine.research.intake.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
