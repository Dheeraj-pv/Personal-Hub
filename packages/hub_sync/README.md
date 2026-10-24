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

## Contract

- Rows: `id` (UUID), `updated_at` (UTC ms), `deleted` (0/1), local-only `dirty`.
- `POST /sync/{app}` (`expenses` | `notes`), `Authorization: Bearer <token>`,
  body `{device_id, cursor, changes:[{table,id,updated_at,deleted,data}]}`,
  response `{cursor, changes:[{..., seq}], more}`. `GET /health`.
- Hub assigns a rising `seq` per accepted change. If incoming `updated_at` >=
  stored, incoming wins (new seq); otherwise the stored row is returned in
  `changes` regardless of cursor.
- Max 500 changes per request each way; engine loops while `more` or more dirty.
- Timeouts: connect 3 s, total 15 s. Failures are quiet: rows stay dirty, the
  cursor only moves after `applyRemote` succeeded, next trigger retries.
- Pushes are idempotent. Tombstones are never purged.

## Testing

`package:hub_sync/testing.dart` exports `MockHub`, an in-process `HttpServer`
(ephemeral port) with token check, seq, conflict rule, paging, `/health`,
`failNext`, `latency`, `onSyncRequest` and `seedFromOtherDevice`.

    dart test
