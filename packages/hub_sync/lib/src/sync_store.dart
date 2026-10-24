import 'sync_change.dart';

/// What the sync engine needs from the app's local database.
abstract interface class SyncStore {
  /// Up to [limit] rows with dirty = 1 (including tombstones), parents
  /// (e.g. categories) before children (e.g. transactions).
  Future<List<SyncChange>> readDirty(int limit);

  /// Called after the hub accepted [pushed]. Clear `dirty` for a row only if
  /// it was not modified since it was read, i.e. its current `updated_at`
  /// still equals the pushed one. Otherwise leave it dirty.
  Future<void> markSynced(List<SyncChange> pushed);

  /// Apply rows from the hub, in the given (seq) order. Must NOT set dirty.
  /// May throw; the engine then keeps the cursor where it was.
  Future<void> applyRemote(List<SyncChange> changes);

  /// Highest hub `seq` already applied (0 if none).
  Future<int> getCursor();
  Future<void> setCursor(int cursor);
}

/// Optional add-on for a [SyncStore] (kept separate so existing stores keep
/// compiling). The engine calls [onHubReset] when the hub reports a cursor
/// lower than ours, meaning it was reset or replaced and has none of our rows:
/// mark every row dirty so the next push sends everything. The engine itself
/// resets the cursor to 0.
abstract interface class HubResetAware {
  Future<void> onHubReset();
}

/// Thrown by a store when it cannot apply a pulled row. [message] is shown as
/// the sync error, so it must name the row (e.g. `categories/<id>`) and never
/// contain a token or other secret. Any other exception type is reported only
/// as `Sync failed: <type>`.
class SyncStoreException implements Exception {
  SyncStoreException(this.message);
  final String message;
  @override
  String toString() => 'SyncStoreException: $message';
}
