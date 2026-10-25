import os
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "new_token.py"


def run(home, *args):
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)


def token_of(home):
    line = (home / ".config" / "personal-hub" / "env").read_text().strip()
    assert line.startswith("HUB_TOKEN=")
    return line.split("=", 1)[1]


def test_creates_file_with_strong_token_and_modes(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    f = tmp_path / ".config" / "personal-hub" / "env"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert len(token_of(tmp_path)) >= 32


def test_never_prints_token(tmp_path):
    r = run(tmp_path)
    assert token_of(tmp_path) not in r.stdout + r.stderr


def test_refuses_overwrite_without_rotate(tmp_path):
    run(tmp_path)
    first = token_of(tmp_path)
    r = run(tmp_path)
    assert r.returncode != 0
    assert "--rotate" in r.stderr
    assert token_of(tmp_path) == first


def test_rotate_replaces_token_keeps_modes(tmp_path):
    run(tmp_path)
    first = token_of(tmp_path)
    r = run(tmp_path, "--rotate")
    assert r.returncode == 0
    assert token_of(tmp_path) != first
    assert token_of(tmp_path) not in r.stdout + r.stderr
    f = tmp_path / ".config" / "personal-hub" / "env"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
