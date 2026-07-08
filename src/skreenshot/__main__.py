"""python -m skreenshot; also how the detached clipboard holder re-execs."""

import sys
import time

T0 = time.monotonic()

from skreenshot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(t0=T0))
