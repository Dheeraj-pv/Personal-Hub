@Tags(['real-hub'])
library;

import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import 'package:hub_sync/hub_sync.dart';
import 'package:test/test.dart';

import 'support/memory_store.dart';
import 'support/real_hub_process.dart';

/// Counts the requests that reach the hub (through the real HTTP stack).
class _CountingClient extends http.BaseClient {
  final IOClient _inner = IOClient();
  int syncPosts = 0;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    if (request.method == 'POST') syncPosts++;
    return _inner.send(request);
  }

  @override
  void close() => _inner.close();
}

class _Device {
  _Device(this.id, this.store, this.http, this.client, this.engine);
  final String id;
  final MemoryStore store;
  final _CountingClient http;
  final HubClient client;
  final SyncEngine engine;
}

const _table = 'transactions';

void main() {
  // Runs only when HUB_REPO is set (see README, "Testing against the real hub").
  final skip = Platform.environment['HUB_REPO'] == null
      ? 'set HUB_REPO to the hub repo path (see README)'
      : false;

  group('real Python hub', skip: skip, () {
    late RealHubProcess hub;
    final devices = <_Device>[];

    _Device device(String id, {String? token}) {
      final counting = _CountingClient();
      final client = HubClient(
        HubConfig(
          urls: [hub.url],
          token: token ?? hub.token,
          deviceId: id,
          appName: 'expenses',
        ),
        httpClient: counting,
        totalTimeout: const Duration(seconds: 20),
      );
      final store = MemoryStore();
      final d = _Device(id, store, counting, client,
          SyncEngine(client: client, store: store, app: 'expenses'));
      devices.add(d);
      return d;
    }

    setUp(() async {
      hub = await RealHubProcess.create();
      await hub.start();
    });

    tearDown(() async {
      for (final d in devices) {
        d.client.close();
      }
      devices.clear();
      await hub.dispose();
    });

    String idOf(int i) => 'row-${i.toString().padLeft(5, '0')}';

    /// id -> "updatedAt|deleted|data" for every id in [ids] (null if absent).
    Map<String, String?> snapshot(MemoryStore s, Iterable<String> ids) => {
          for (final id in ids)
            id: s.get(_table, id) == null
                ? null
                : '${s.get(_table, id)!.updatedAt}|'
                    '${s.get(_table, id)!.deleted}|'
                    '${s.get(_table, id)!.data}',
        };

    test('600+ dirty rows push in two batches and all dirty flags clear',
        () async {
      final a = device('dev-a');
      for (var i = 0; i < 650; i++) {
        a.store.edit(_table, idOf(i), 1000 + i, data: {'n': i});
      }
      final r = await a.engine.sync();
      expect(r.ok, isTrue, reason: '$r');
      expect(r.pushed, 650);
      expect(a.http.syncPosts, 2, reason: '500 + 150');
      expect(a.store.dirtyCount, 0);

      // The hub really holds them all: a fresh device pulls 650 rows.
      final c = device('dev-c');
      final rc = await c.engine.sync();
      expect(rc.ok, isTrue, reason: '$rc');
      expect(c.store.rowCount, 650);
      expect(snapshot(c.store, [for (var i = 0; i < 650; i++) idOf(i)]),
          snapshot(a.store, [for (var i = 0; i < 650; i++) idOf(i)]));
    });

    test('a fresh device pulls 1,250 rows over 3 requests and matches',
        () async {
      final a = device('dev-a');
      for (var i = 0; i < 1250; i++) {
        a.store.edit(_table, idOf(i), 1000 + i,
            data: {'n': i}, deleted: i % 97 == 0);
      }
      expect((await a.engine.sync()).ok, isTrue);
      expect(a.store.dirtyCount, 0);

      final b = device('dev-b');
      final r = await b.engine.sync();
      expect(r.ok, isTrue, reason: '$r');
      expect(r.pulled, 1250);
      expect(b.http.syncPosts, 3, reason: '500 + 500 + 250');
      expect(b.store.rowCount, 1250);
      expect(b.store.dirtyCount, 0);
      final ids = [for (var i = 0; i < 1250; i++) idOf(i)];
      expect(snapshot(b.store, ids), snapshot(a.store, ids));
      expect(await b.store.getCursor(), 1250);
    });

    test('conflicts: newer wins both ways and refused rows overwrite stale',
        () async {
      final a = device('dev-a');
      final b = device('dev-b');
      a.store.edit(_table, 'x', 100, data: {'v': 'a1'});
      expect((await a.engine.sync()).ok, isTrue);
      expect((await b.engine.sync()).ok, isTrue);
      expect(b.store.get(_table, 'x')!.data['v'], 'a1');

      // B's newer edit replaces the hub copy, then reaches A.
      b.store.edit(_table, 'x', 200, data: {'v': 'b'});
      expect((await b.engine.sync()).ok, isTrue);
      expect((await a.engine.sync()).ok, isTrue);
      expect(a.store.get(_table, 'x')!.data['v'], 'b');

      // A (stale, clean copy at 200) edits at 150 < 200: refused, hub copy
      // comes back and overwrites A's dirty row; A stays clean.
      a.store.edit(_table, 'x', 150, data: {'v': 'a2'});
      final r = await a.engine.sync();
      expect(r.ok, isTrue, reason: '$r');
      expect(a.store.get(_table, 'x')!.data['v'], 'b');
      expect(a.store.get(_table, 'x')!.updatedAt, 200);
      expect(a.store.isDirty(_table, 'x'), isFalse);

      // The hub kept B's row: a fresh device sees it.
      final c = device('dev-c');
      expect((await c.engine.sync()).ok, isTrue);
      expect(c.store.get(_table, 'x')!.data['v'], 'b');
    });

    test('two devices with interleaved edits and deletes converge, tombstones '
        'included', () async {
      final a = device('dev-a');
      final b = device('dev-b');
      for (var i = 1; i <= 6; i++) {
        a.store.edit(_table, 'r$i', 100 + i, data: {'v': 'a0-$i'});
      }
      expect((await a.engine.sync()).ok, isTrue);
      expect((await b.engine.sync()).ok, isTrue);

      // Concurrent offline work on both devices.
      a.store.edit(_table, 'r2', 400, data: {'v': 'a-r2'}); // loses to B@500
      a.store.edit(_table, 'r4', 600, data: {'v': 'a-r4'});
      a.store.edit(_table, 'r5', 610, deleted: true);
      b.store.edit(_table, 'r2', 500, data: {'v': 'b-r2'});
      b.store.edit(_table, 'r3', 510, deleted: true);
      b.store.edit(_table, 'r7', 520, data: {'v': 'b-r7'});

      for (final d in [b, a, b, a]) {
        final r = await d.engine.sync();
        expect(r.ok, isTrue, reason: '${d.id}: $r');
      }

      final ids = [for (var i = 1; i <= 7; i++) 'r$i'];
      final snapA = snapshot(a.store, ids);
      expect(snapshot(b.store, ids), snapA);
      expect(a.store.dirtyCount, 0);
      expect(b.store.dirtyCount, 0);
      expect(a.store.get(_table, 'r2')!.data['v'], 'b-r2');
      expect(a.store.get(_table, 'r3')!.deleted, isTrue);
      expect(b.store.get(_table, 'r5')!.deleted, isTrue);
      expect(b.store.get(_table, 'r4')!.data['v'], 'a-r4');

      final c = device('dev-c');
      expect((await c.engine.sync()).ok, isTrue);
      expect(snapshot(c.store, ids), snapA);
    });

    test('wrong token: quiet auth failure, rows stay dirty, no token in error',
        () async {
      const wrong = 'wrong-token-0123456789-abcdefghijklmnopqrstuvwxyz';
      final a = device('dev-a', token: wrong);
      a.store.edit(_table, 'x', 100, data: {'v': 1});
      final r = await a.engine.sync();
      expect(r.ok, isFalse);
      expect(r.error, contains('401'));
      expect(r.error, isNot(contains(wrong)));
      expect(r.error, isNot(contains(hub.token)));
      expect(a.store.isDirty(_table, 'x'), isTrue);
      expect(await a.store.getCursor(), 0);
    });

    test('wiped hub: client sees the lower cursor, resets, re-pushes all',
        () async {
      final a = device('dev-a');
      for (var i = 0; i < 40; i++) {
        a.store.edit(_table, idOf(i), 1000 + i,
            data: {'n': i}, deleted: i % 10 == 0);
      }
      expect((await a.engine.sync()).ok, isTrue);
      expect(await a.store.getCursor(), 40);
      expect(a.store.dirtyCount, 0);

      await hub.stop();
      hub.wipeDb();
      expect(hub.dbFile.existsSync(), isFalse);
      await hub.start();

      final r = await a.engine.sync();
      expect(r.ok, isTrue, reason: '$r');
      expect(a.store.resetCalls, 1);
      expect(r.pushed, 40);
      expect(a.store.dirtyCount, 0);
      expect(await a.store.getCursor(), 40);

      // The hub is fully rebuilt: a fresh device gets everything.
      final c = device('dev-c');
      expect((await c.engine.sync()).ok, isTrue);
      final ids = [for (var i = 0; i < 40; i++) idOf(i)];
      expect(c.store.rowCount, 40);
      expect(snapshot(c.store, ids), snapshot(a.store, ids));
    });

    test('wiped hub refilled past the client cursor is detected by instance id',
        () async {
      final a = device('dev-a');
      for (var i = 0; i < 5; i++) {
        a.store.edit(_table, idOf(i), 1000 + i, data: {'n': i});
      }
      expect((await a.engine.sync()).ok, isTrue);
      expect(await a.store.getCursor(), 5);
      final firstId = await a.store.getInstanceId();
      expect(firstId, isNotNull);

      await hub.stop();
      hub.wipeDb();
      await hub.start();

      // Another device refills the new hub past A's cursor (5), so the
      // lower-cursor check alone sees nothing wrong.
      final b = device('dev-b');
      for (var i = 0; i < 12; i++) {
        b.store.edit(_table, 'b-${idOf(i)}', 2000 + i, data: {'n': i});
      }
      expect((await b.engine.sync()).ok, isTrue);

      final r = await a.engine.sync();
      expect(r.ok, isTrue, reason: '$r');
      expect(a.store.resetCalls, 1);
      expect(r.pushed, 5, reason: 'A re-pushes its own 5 rows');
      expect(await a.store.getInstanceId(), isNot(firstId));
      expect(a.store.rowCount, 17);

      final c = device('dev-c');
      expect((await c.engine.sync()).ok, isTrue);
      expect(c.store.rowCount, 17);
    });

    test('unreachable hub: quiet error, rows stay dirty, next sync succeeds',
        () async {
      final a = device('dev-a');
      a.store.edit(_table, 'x', 100, data: {'v': 1});
      expect((await a.engine.sync()).ok, isTrue);

      await hub.stop();
      a.store.edit(_table, 'y', 200, data: {'v': 2});
      final down = await a.engine.sync();
      expect(down.ok, isFalse);
      expect(down.error, isNot(contains(hub.token)));
      expect(a.store.isDirty(_table, 'y'), isTrue);
      expect(a.engine.failureCount, 1);

      await hub.start(); // same port, same database
      final up = await a.engine.sync();
      expect(up.ok, isTrue, reason: '$up');
      expect(a.store.isDirty(_table, 'y'), isFalse);
      expect(a.engine.failureCount, 0);

      final c = device('dev-c');
      expect((await c.engine.sync()).ok, isTrue);
      expect(c.store.get(_table, 'y')!.data['v'], 2);
    });
  });
}
