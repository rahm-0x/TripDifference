"""
Where this app is allowed to write.

Locally that is the project directory. On Vercel the filesystem is read-only
except /tmp, so DATA_DIR is pointed there — which means state is **ephemeral**
and resets when the instance recycles. Fine for a demo URL, not for real
bookkeeping; see README.

Set DATA_DIR to override. Set DUMP_RESPONSES=0 to stop writing a JSON file per
API call (208MB locally; pointless on a serverless instance).
"""

import os
from pathlib import Path

_HERE = Path(__file__).parent

# On Vercel, VERCEL=1 is always present in the runtime environment.
ON_VERCEL = bool(os.environ.get("VERCEL"))

DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/tmp/tripdifference" if ON_VERCEL else _HERE))

# Response dumping is a local debugging aid, off by default in serverless.
DUMP_RESPONSES = os.environ.get("DUMP_RESPONSES", "0" if ON_VERCEL else "1") != "0"


def data_path(name):
    """A writable path for `name`, creating the directory on first use."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # read-only root; the caller's write will surface the real error
    return DATA_DIR / name
