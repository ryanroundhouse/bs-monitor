"""Tiny zero-dependency .env loader.

Reads `KEY=VALUE` pairs from a `.env` file into `os.environ` so credentials and
config load automatically — no `source .env` needed, and no new dependency.

Real environment variables always win: a key already set in the environment is
never overwritten by the file. Looks for `.env` in the current working
directory first, then in the repo root (next to the `moodful_responder`
package), unless an explicit path is given.
"""

import os
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_candidates():
    seen = set()
    for p in (Path.cwd() / ".env", _REPO_ROOT / ".env"):
        if p not in seen:
            seen.add(p)
            yield p


def load_dotenv(path: Optional[str] = None) -> Optional[Path]:
    """Load the first `.env` found into os.environ. Returns the path used."""
    candidates = [Path(path)] if path else list(_default_candidates())
    for p in candidates:
        if p.is_file():
            _parse_into_environ(p)
            return p
    return None


def _parse_into_environ(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
