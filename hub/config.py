"""Settings from environment variables. The token is never printed or repr'd."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

MIN_TOKEN_LENGTH = 32
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_PLACEHOLDER_TOKENS = frozenset({"changeme", "change-me", "your-token-here", "token", "secret"})

log = logging.getLogger("hub")
DEFAULT_DB = "~/PersonalHub/data/hub.db"
DEFAULT_NOTES_DIR = "~/PersonalHub/notes"


def db_path_from_env(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("HUB_DB") or DEFAULT_DB).expanduser()


def notes_dir_from_env(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("HUB_NOTES_DIR") or DEFAULT_NOTES_DIR).expanduser()


class ConfigError(RuntimeError):
    """Raised at startup for unusable configuration. Messages never include the token."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8787
    allow_non_loopback: bool = False
    # None disables the markdown export (e.g. tests); load_settings always sets it.
    notes_dir: Path | None = None

    def __post_init__(self) -> None:
        lowered = self.token.strip().lower()
        if lowered.startswith("replace-with") or lowered in _PLACEHOLDER_TOKENS:
            raise ConfigError(
                "HUB_TOKEN is still a placeholder; generate a real one with scripts/new_token.py"
            )
        if self.host not in LOOPBACK_HOSTS and not self.allow_non_loopback:
            raise ConfigError(
                "HUB_HOST must be a loopback address (127.0.0.1, ::1, localhost): "
                "Tailscale Serve publishes the hub, nothing else should reach it. "
                "Set HUB_ALLOW_NON_LOOPBACK=1 to override."
            )
        if len(self.token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"HUB_TOKEN is missing or shorter than {MIN_TOKEN_LENGTH} characters; "
                "generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    try:
        port = int(env.get("HUB_PORT", "8787"))
    except ValueError:
        raise ConfigError("HUB_PORT must be an integer") from None
    allow = env.get("HUB_ALLOW_NON_LOOPBACK") == "1"
    settings = Settings(
        token=env.get("HUB_TOKEN", ""),
        db_path=db_path_from_env(env),
        host=env.get("HUB_HOST") or "127.0.0.1",
        port=port,
        allow_non_loopback=allow,
        notes_dir=notes_dir_from_env(env),
    )
    if settings.host not in LOOPBACK_HOSTS:
        log.warning(
            "hub is configured to listen on a non-loopback address; anyone who can reach "
            "this port only needs the token"
        )
    return settings
