"""Create ~/.config/personal-hub/env with a fresh HUB_TOKEN.

    uv run python scripts/new_token.py            # first time
    uv run python scripts/new_token.py --rotate   # replace the token

The token is never printed. Read it on purpose with: cat ~/.config/personal-hub/env
"""

import argparse
import os
import secrets
import sys
from pathlib import Path


def env_path() -> Path:
    return Path.home() / ".config" / "personal-hub" / "env"


def write_token(rotate: bool = False) -> Path:
    path = env_path()
    if path.exists() and not rotate:
        raise SystemExit(f"{path} already exists. Use --rotate to replace the token.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    # Created with mode 600 up front, so the secret is never briefly world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"HUB_TOKEN={secrets.token_urlsafe(32)}\n")
    os.replace(tmp, path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rotate", action="store_true", help="replace an existing token")
    args = parser.parse_args()
    path = write_token(args.rotate)
    print(f"Token written to {path} (mode 600). It is not shown here.")
    print(f"To read it when you need it (e.g. for the phone): cat {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
