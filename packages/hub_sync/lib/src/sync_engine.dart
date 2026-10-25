import 'dart:async';

import 'hub_client.dart';
import 'sync_store.dart';

/// Outcome of one [SyncEngine.sync] run. [error] is a short, token-free
/// message; null means success.
class SyncResult {
  const SyncResult({this.pushed = 0, this.pulled = 0, this.error});
  final int pushed;
  final int pulled;
  final String? error;
  bool get ok => error == null;
  @override
  String toString() =>
      'SyncResult(pushed: $pushed, pulled: $pulled, error: $error)';
}

/// Runs push -> pull cycles against the hub. Never throws.
class SyncEngine {
  SyncEngine({
    required this.client,
    required this.store,
    required this.app,
    this.batchSize = 500,
    this.maxIterations = 200,
    this.backoffBase = const Duration(seconds: 5),
    this.backoffMax = const Duration(minutes: 15),
    DateTime Function()? now,
  }) : _now = now ?? DateTime.now;

  final HubClient client;
  final SyncStore store;

  /// Hub app name (`expenses` or `notes`).
  final String app;
  final int batchSize;

  /// Safety cap on loop turns per run (the rest waits for the next trigger).
  final int maxIterations;
  final Duration backoffBase;
  final Duration backoffMax;
  final DateTime Function() _now;

  Future<SyncResult>? _inFlight;
  int _failures = 0;
  DateTime? _nextAllowedAt;

  /// Consecutive failed runs since the last success.
  int get failureCount => _failures;

  /// Earliest time automatic triggers should try again; null when healthy.
  DateTime? get nextAllowedAt => _nextAllowedAt;

  /// True while an automatic trigger should hold off. Manual "Sync now" may
  /// ignore this; [sync] itself always runs.
  bool get shouldBackOff {
    final t = _nextAllowedAt;
    return t != null && _now().isBefore(t);
  }

  bool get isSyncing => _inFlight != null;

  /// Starts a sync, or returns the one already running.
  Future<SyncResult> sync() {
    final running = _inFlight;
    if (running != null) return running;
    final future = _run().whenComplete(() => _inFlight = null);
    _inFlight = future;
    return future;
  }

  Future<SyncResult> _run() async {
    var pushed = 0;
    var pulled = 0;
    var resetHandled = false;
    // Rows per push for this run; halves on 413 and resets on the next run.
    var batch = batchSize;
    try {
      for (var i = 0; i < maxIterations; i++) {
        final dirty = await store.readDirty(batch);
        final cursor = await store.getCursor();
        final SyncResponse res;
        try {
          res = await client.sync(app, cursor, dirty);
        } on HubPayloadTooLargeException {
          if (dirty.isEmpty) rethrow;
          if (dirty.length == 1) {
            // Names the row only; never its content. It stays dirty.
            throw HubException(
                'Row too large to sync: ${dirty.first.table}/${dirty.first.id}');
          }
          batch = dirty.length ~/ 2;
          continue;
        }
        final seen = res.instanceId;
        final Object s = store;
        var instanceChanged = false;
        if (seen != null && s is InstanceAware) {
          final known = await s.getInstanceId();
          if (known == null) {
            await s.setInstanceId(seen); // first contact: nothing to redo
          } else {
            instanceChanged = known != seen;
          }
        }
        if (instanceChanged || res.cursor < cursor) {
          // The hub is behind us: it was reset or replaced. Our rows are not
          // there, so forget the cursor, have the store re-mark everything
          // dirty and go round again (once; a second time is an error).
          if (resetHandled) {
            throw HubException(
                'The hub keeps reporting a different or older state');
          }
          resetHandled = true;
          await store.setCursor(0);
          if (s is HubResetAware) await s.onHubReset();
          // Remember the new database only after the reset is done, so a crash
          // in between triggers the reset again.
          if (instanceChanged) await (s as InstanceAware).setInstanceId(seen!);
          continue;
        }
        // Apply before markSynced: a refused row (the hub holds a newer copy)
        // comes back in `res.changes` while our edit is still dirty, so the
        // store can keep our text (e.g. as a conflict copy) instead of
        // finding a clean row and overwriting it. markSynced then skips any
        // row whose updated_at no longer matches what was pushed. If apply
        // throws, the pushed rows stay dirty and are simply sent again.
        await store.applyRemote(res.changes);
        pulled += res.changes.length;
        await store.markSynced(dirty);
        pushed += dirty.length;
        if (res.cursor > cursor) await store.setCursor(res.cursor);
        if (!res.more && dirty.length < batch) break;
      }
      _failures = 0;
      _nextAllowedAt = null;
      return SyncResult(pushed: pushed, pulled: pulled);
    } on Object catch (e) {
      _failures++;
      final factor = 1 << (_failures - 1).clamp(0, 20);
      var delay = backoffBase * factor;
      if (delay > backoffMax) delay = backoffMax;
      _nextAllowedAt = _now().add(delay);
      final msg = switch (e) {
        HubException() => e.message,
        SyncStoreException() => e.message,
        _ => 'Sync failed: ${e.runtimeType}',
      };
      return SyncResult(pushed: pushed, pulled: pulled, error: msg);
    }
  }
}
