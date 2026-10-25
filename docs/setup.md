# Setting up the hub on your laptop

The hub is a small server that stores a copy of your Money Me data. It listens only on
`127.0.0.1` (this laptop, nothing else). Tailscale then publishes it to your phone over HTTPS.
Commands marked **(sudo)** change your system, so you run them yourself.

## 1. Install the dependencies

```bash
cd ~/Personal_files/personal-hub
uv sync
mkdir -p -m 700 ~/PersonalHub
```

The service runs from `.venv` (not `uv run`) because the unit makes the disk read-only except
`~/PersonalHub`, and `uv` wants to write to its cache. So re-run `uv sync` after changing dependencies.
`~/PersonalHub` must exist before the service starts, or systemd refuses to start it.
If you set a custom `HUB_DB` or `HUB_BACKUP_DIR`, it must live under `~/PersonalHub`, or you must add its
folder to `ReadWritePaths` in the unit (everything else is read-only for the service).

## 2. Create the token

```bash
uv run python scripts/new_token.py
```

This writes `~/.config/personal-hub/env` (mode 600) and does **not** print the token, so it does not
end up in your terminal history or screen recordings. To read it deliberately (for the phone):

```bash
cat ~/.config/personal-hub/env
```

Move it to the phone with a password manager, or type it in. Never send it through chat, email or AI
tools, and avoid `cat` in shared terminals or while screen-recording.

## 3. Install the systemd units

```bash
mkdir -p ~/.config/systemd/user
cp deploy/personal-hub.service deploy/personal-hub-backup.service deploy/personal-hub-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now personal-hub.service
systemctl --user enable --now personal-hub-backup.timer
```

These are *user* units, so no sudo. Linger is already on for your account, so they start at boot.
If the repo lives somewhere other than `~/Personal_files/personal-hub`, edit `WorkingDirectory` and `ExecStart` in the service files.

Right after `enable --now`, check the service really started:

```bash
systemctl --user status personal-hub
```

If it fails with namespace errors or exit status `226/NAMESPACE`, the cause is the sandbox options in the unit
(Ubuntu's AppArmor restriction on user namespaces blocks them for user services). The fix is to remove the
`ProtectSystem` and `PrivateTmp` lines from `~/.config/systemd/user/personal-hub.service`, run
`systemctl --user daemon-reload` and restart it. Do not give up on the service; the hub is still bound to
loopback and protected by the token without them.

## 4. Check it works locally

```bash
systemctl --user status personal-hub
curl -s http://127.0.0.1:8787/health
```

Without a token you only get `{"status":"ok"}`. That is deliberate. For details:

```bash
TOKEN=$(grep '^HUB_TOKEN=' ~/.config/personal-hub/env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/health
```

Test the backup once: `systemctl --user start personal-hub-backup.service`, then `ls -l ~/PersonalHub/backups`.
(Do this after the hub has created its database, i.e. after the first start.)

## 5. Tailscale on the laptop

1. **(sudo)** Install Tailscale using the official instructions for your distro: https://tailscale.com/download/linux
2. **(sudo)** `sudo tailscale up` and sign in via the link it prints.
3. Install the Tailscale app on your phone and sign in with the **same account**.

## 6. Tailscale admin console

Open https://login.tailscale.com/admin and:

1. **DNS** page: turn on **MagicDNS**, and turn on **HTTPS Certificates**. Serve needs both to give you an `https://` address.
2. **Machines** page: next to the laptop, open the `...` menu and choose **Disable key expiry**.
   Why: device keys expire after about 180 days. Without this, sync would silently stop one day and you would have to re-login.

## 7. Publish the hub with Tailscale Serve

Flags have changed between Tailscale versions, so first look at what yours supports:

```bash
tailscale serve --help
```

Then use the current syntax for "serve `localhost:8787` over HTTPS, in the background". The most likely modern command is below (**check against `--help`**):

```bash
sudo tailscale serve --bg 8787
```

(Some versions need `sudo` the first time, or `tailscale set --operator=$USER` once so you can run it without.)
Find your address:

```bash
tailscale serve status
tailscale status
```

It looks like `https://<machine>.<tailnet>.ts.net`. Check it from the laptop:
`curl -s https://<machine>.<tailnet>.ts.net/health`.
Serve (not Funnel) keeps this private to your own devices. Never use `funnel`; that would expose it to the whole internet.

Confirm Funnel is off after publishing:

```bash
tailscale serve status
tailscale funnel status
```

`serve status` should show your address as tailnet-only, and `funnel status` should say Funnel is off
(nothing listed). If it shows `Funnel on`, run `tailscale funnel reset` or turn it off before continuing.

Serve is only private if your tailnet has no other users or shared machines. If it does, restrict who can
reach the laptop with Tailscale ACLs.

## 8. Phone

Money Me > Settings > Laptop sync:

- **Address:** `https://<machine>.<tailnet>.ts.net`
- **Token:** the value after `HUB_TOKEN=` in `~/.config/personal-hub/env` (copy it via a password manager or type it; see step 2)
- **Device name:** anything, e.g. "Pixel"
- Switch **on**, then tap **Sync now**.

The Tailscale app on the phone must be connected. Android allows only one VPN at a time, so another VPN app will clash.

## Everyday things

**Logs**
```bash
journalctl --user -u personal-hub -f      # follow
journalctl --user -u personal-hub-backup  # backup runs
```
Our own log lines contain no tokens or request bodies. If the hub crashes on a request, the server may still
print a Python traceback to the journal (it holds code locations, not request data).

**When the laptop sleeps or is off:** the hub is unreachable and the phone shows a quiet "last problem". Nothing is lost: the phone keeps its changes and pushes them on the next successful sync.

**Restore from a backup**
```bash
systemctl --user stop personal-hub
cd ~/PersonalHub/data
mv hub.db hub.db.bad
mv -f hub.db-wal hub.db-wal.bad; mv -f hub.db-shm hub.db-shm.bad   # ignore "No such file" errors
cp ~/PersonalHub/backups/hub-YYYY-MM-DD.db hub.db
systemctl --user start personal-hub
```
Move the old database and its `-wal`/`-shm` files aside **before** copying the backup in: they belong to the old
database and would corrupt the restored one, and this way you still have the old file if the backup is wrong.
Backups are kept daily, newest 14.
A restored backup keeps the same instance id, so the phone notices only through the lower cursor (the hub's
position is behind the phone's) and re-sends whatever the backup is missing. If other devices have pushed past
the backup's position since, a phone's cursor can be lower than the hub's and the missing rows may not be
re-sent; treat restore as a last resort.

**Delete hub.db to rebuild from the phone:** stop the service, remove `~/PersonalHub/data/hub.db*`, start it, then tap **Sync now** on the phone. The new database gets a new instance id, so the phone sees the hub was replaced and re-pushes everything
(this works even if another device has already refilled the hub past the phone's position).
(Anything that exists *only* on the hub is lost this way, so this is only safe while the phone has the full data.)

**Before wiping `hub.db`, keep a manual copy** (`cp -a ~/PersonalHub/data ~/hub-data-copy`). An empty database also
passes the backup integrity check, so within two weeks the nightly backups would rotate out all 14 good copies.

**Notes as markdown files:** after each sync of the `notes` app the hub writes one `.md` file per note to
`~/PersonalHub/notes` (override with `HUB_NOTES_DIR`; keep it under `~/PersonalHub`, or add its folder to
`ReadWritePaths`). Deleted notes move to `notes/.trash/`. The files are a one-way copy; edit notes in the app.
After a hub reset or restore, rebuild every file from the database (safe to repeat; stop nothing, run as yourself):
```bash
HUB_DB=~/PersonalHub/data/hub.db uv run python -m hub.notes_export
```

**Rotate the token** (if it leaked, or you pasted it somewhere):
```bash
uv run python scripts/new_token.py --rotate
systemctl --user restart personal-hub
cat ~/.config/personal-hub/env
```
Then paste the new token into Money Me > Settings > Laptop sync. Until you do, the phone gets a 401 and stops retrying.

**Disk encryption:** the hub database and its backups are plain SQLite files holding your full finance history. File modes (600/700) stop other users, not someone holding the laptop. Turn on full-disk encryption (LUKS, usually offered at Ubuntu install; check with `lsblk -f` for `crypto_LUKS`) and use a screen lock. Backups sit on the same disk, so copy `~/PersonalHub/backups` somewhere else now and then if you want protection against losing the laptop.
