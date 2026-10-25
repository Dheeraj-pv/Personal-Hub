# hub_sync

Pure-Dart sync client for the personal hub (no Flutter dependency). Apps keep a
local database; this package pushes dirty rows to the hub and pulls other
devices' changes.

## Usage

```dart
final client = HubClient(HubConfig(
  urls: ['https://laptop.tailnet.ts.net'], // tried in order
  token: token, deviceId: deviceId, appName: 'expenses',
));
final engine = SyncEngine(client: client, store: MyStore(), app: 'expenses');

if (!engine.shouldBackOff) {
  final result = await engine.sync(); // never throws
  if (!result.ok) showLastError(result.error);
}
```

Implement `SyncStore` over your database (`readDirty`, `markSynced`,
`applyRemote`, `getCursor`, `setCursor`). Rules: `markSynced` clears `dirty`
only if the row's `updated_at` still equals the pushed one; `applyRemote`
never sets `dirty` and applies in the given order.

Optional: also implement `HubResetAware` (`onHubReset()`: mark every row dirty) and `InstanceAware`
(`getInstanceId()` / `setInstanceId(id)`: persist the hub's `instance_id` next to the cursor). With both, a
hub that was wiped and refilled past your cursor is detected and everything is re-pushed.

## Contract

- Rows: `id` (UUID), `updated_at` (UTC ms), `deleted` (0/1), local-only `dirty`.
- `POST /sync/{app}` (`expenses` | `notes`), `Authorization: Bearer <token>`,
  body `{device_id, cursor, changes:[{table,id,updated_at,deleted,data}]}`,
  response `{cursor, changes:[{..., seq}], more, instance_id}` (`instance_id` may be absent on old hubs). `GET /health`.
- Hub assigns a rising `seq` per accepted change. If incoming `updated_at` >=
  stored, incoming wins (new seq); otherwise the stored row is returned in
  `changes` regardless of cursor.
- Max 500 changes per request each way; engine loops while `more` or more dirty.
- Max request body 2 MiB (413). On 413 the engine halves the push batch (down to 1) for the rest of that run
  and retries; a single row that is still too large ends the run with `Row too large to sync: <table>/<id>`
  (no row content, row stays dirty and, being first in line, keeps blocking later pushes until it is made smaller).
- Timeouts: connect 3 s, total 15 s. Failures are quiet: rows stay dirty, the
  cursor only moves after `applyRemote` succeeded, next trigger retries.
- Pushes are idempotent. Tombstones are never purged.

## Testing

`package:hub_sync/testing.dart` exports `MockHub`, an in-process `HttpServer`
(ephemeral port) with token check, seq, conflict rule, paging, `/health`,
`failNext`, `latency`, `onSyncRequest` and `seedFromOtherDevice`.

    dart test

## Testing against the real hub

`test/real_hub_test.dart` (tag `real-hub`) runs the real `HubClient` and
`SyncEngine` against the Python hub from this repo: it starts
`uv run python -m hub.main` on a free loopback port with a temporary database
and a random token, and always kills it afterwards. A plain `dart test` skips
these tests (a skipped test counts as passed), so it stays fast and needs no
Python. To run them, set `HUB_REPO` to the hub repo root (the folder with
`pyproject.toml`; needs `uv`):

    HUB_REPO=../.. dart test -t real-hub

`-t real-hub` only narrows the run to those tests; the skip is decided by
`HUB_REPO` being unset (the test runner cannot see which tags were selected).
