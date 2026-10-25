# Personal Hub

A small FastAPI + SQLite server that runs on my laptop and keeps a copy of my phone apps' data
(Money Me expenses and notes). Phones sync to it through Tailscale Serve over HTTPS. The hub stores each
row's `data` as JSON and knows nothing about what is inside.

## Layout

```
hub/        the service (main.py app, config, db, sync logic, auth) and backup.py (python -m hub.backup)
tests/      pytest tests
deploy/     systemd user units (service, backup service + timer) and env.example
scripts/    new_token.py (creates the token file)
docs/       setup.md (full install guide)
packages/hub_sync/   the Dart client library the phone apps use (the sync contract lives there)
```

## Run locally for development

```bash
uv sync
HUB_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))") \
HUB_DB=/tmp/hub-dev.db \
uv run uvicorn --factory hub.main:create_app --host 127.0.0.1 --port 8787
```

Settings come from environment variables: `HUB_TOKEN` (required, at least 32 characters, placeholders
are rejected), `HUB_DB`, `HUB_HOST` (loopback only; `HUB_ALLOW_NON_LOOPBACK=1` to override), `HUB_PORT`,
`HUB_BACKUP_DIR`. The systemd unit hard-codes host and port. Generate the token with `scripts/new_token.py`
and move it to the phone with a password manager (never via chat, email or AI tools).

Each database has an `instance_id` (in `/sync` responses and authenticated `/health`). A wiped or replaced
hub gets a new one, which clients use to notice and re-send everything; a hub restored from a backup keeps
its id and is detected only through the lower cursor. Our log lines never include tokens or request bodies;
the server may print tracebacks to the journal.
Tailscale Serve is only private if your tailnet has no other users or shared machines (otherwise use ACLs);
after publishing, check `tailscale funnel status` shows Funnel off (details in docs/setup.md).

## Tests

```bash
uv run python -m pytest -q
```

## Production setup

See [docs/setup.md](docs/setup.md) (systemd, backups, Tailscale, phone settings).
The sync contract and Dart client are in [packages/hub_sync](packages/hub_sync).
