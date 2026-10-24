import 'package:hub_sync/hub_sync.dart';

class _Row {
  _Row(this.change, this.dirty);
  SyncChange change;
  bool dirty;
}

/// In-memory [SyncStore] with test hooks. Applies the same rules a real
/// store must: remote apply never sets dirty, and a dirty local row that is
/// newer than the remote one is kept.
class MemoryStore implements SyncStore, HubResetAware {
  final Map<String, _Row> _rows = {};
  int _cursor = 0;

  /// Make [applyRemote] throw (to test cursor handling).
  Object? failApplyWith;
  int applyCalls = 0;
  int resetCalls = 0;

  /// Local edit: stores the row as dirty.
  void edit(String table, String id, int updatedAt,
      {Map<String, dynamic> data = const {}, bool deleted = false}) {
    _rows['$table/$id'] = _Row(
      SyncChange(
          table: table,
          id: id,
          updatedAt: updatedAt,
          deleted: deleted,
          data: data),
      true,
    );
  }

  SyncChange? get(String table, String id) => _rows['$table/$id']?.change;
  bool isDirty(String table, String id) => _rows['$table/$id']!.dirty;
  int get dirtyCount => _rows.values.where((r) => r.dirty).length;
  int get rowCount => _rows.length;

  @override
  Future<List<SyncChange>> readDirty(int limit) async => [
        for (final r in _rows.values)
          if (r.dirty) r.change,
      ].take(limit).toList();

  @override
  Future<void> markSynced(List<SyncChange> pushed) async {
    for (final p in pushed) {
      final r = _rows['${p.table}/${p.id}'];
      if (r != null && r.change.updatedAt == p.updatedAt) r.dirty = false;
    }
  }

  @override
  Future<void> applyRemote(List<SyncChange> changes) async {
    applyCalls++;
    final err = failApplyWith;
    if (err != null) throw err;
    for (final c in changes) {
      final local = _rows['${c.table}/${c.id}'];
      if (local != null &&
          local.dirty &&
          local.change.updatedAt > c.updatedAt) {
        continue;
      }
      _rows['${c.table}/${c.id}'] = _Row(c, false);
    }
  }

  @override
  Future<void> onHubReset() async {
    resetCalls++;
    for (final r in _rows.values) {
      r.dirty = true;
    }
  }

  @override
  Future<int> getCursor() async => _cursor;

  @override
  Future<void> setCursor(int cursor) async => _cursor = cursor;
}
