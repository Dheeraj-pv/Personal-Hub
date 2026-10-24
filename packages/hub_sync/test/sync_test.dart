import 'dart:async';

import 'package:hub_sync/hub_sync.dart';
import 'package:hub_sync/testing.dart';
import 'package:test/test.dart';

import 'support/memory_store.dart';

void main() {
  late MockHub hub;
  late MemoryStore store;
  late HubClient client;
  late SyncEngine engine;

  HubClient makeClient({
    String? token,
    List<String>? urls,
    String device = 'dev-a',
    Duration total = const Duration(seconds: 15),
  }) =>
      HubClient(
        HubConfig(
          urls: urls ?? [hub.url],
          token: token ?? hub.token,
          deviceId: device,
          appName: 'expenses',
        ),
        totalTimeout: total,
      );

  SyncChange change(String id, int ts,
          {bool deleted = false, Map<String, dynamic>? data}) =>
      SyncChange(
          table: 'transactions',
          id: id,
          updatedAt: ts,
          deleted: deleted,
          data: data ?? {'v': ts});

  setUp(() async {
    hub = await MockHub.start();
    store = MemoryStore();
    client = makeClient();
    engine = SyncEngine(client: client, store: store, app: 'expenses');
  });

  tearDown(() async {
    client.close();
    await hub.stop();
  });

  group('conflict rule', () {
    test('newer incoming replaces stored and gets a new seq', () async {
      hub.seedFromOtherDevice('expenses', [change('a', 100)]);
      final seq0 = hub.row('expenses', 'transactions', 'a')!.seq!;
      store.edit('transactions', 'a', 200, data: {'v': 'mine'});
      final r = await engine.sync();
      expect(r.ok, isTrue);
      final stored = hub.row('expenses', 'transactions', 'a')!;
      expect(stored.data['v'], 'mine');
      expect(stored.seq, greaterThan(seq0));
      expect(store.isDirty('transactions', 'a'), isFalse);
    });

    test('older incoming is refused and the stored row comes back', () async {
      hub.seedFromOtherDevice('expenses', [
        change('a', 300, data: {'v': 'theirs'})
      ]);
      store.edit('transactions', 'a', 200, data: {'v': 'mine'});
      final r = await engine.sync();
      expect(r.ok, isTrue);
      expect(hub.row('expenses', 'transactions', 'a')!.data['v'], 'theirs');
      expect(store.get('transactions', 'a')!.data['v'], 'theirs');
      expect(store.isDirty('transactions', 'a'), isFalse);
    });

    test('equal updated_at keeps the incoming row', () async {
      hub.seedFromOtherDevice('expenses', [
        change('a', 200, data: {'v': 'theirs'})
      ]);
      store.edit('transactions', 'a', 200, data: {'v': 'mine'});
      await engine.sync();
      expect(hub.row('expenses', 'transactions', 'a')!.data['v'], 'mine');
    });

    test('tombstones sync both ways', () async {
      hub.seedFromOtherDevice('expenses', [change('a', 100, deleted: true)]);
      await engine.sync();
      expect(store.get('transactions', 'a')!.deleted, isTrue);
      expect(store.isDirty('transactions', 'a'), isFalse);
    });
  });

  group('paging', () {
    test('600 dirty rows go out in two batches and all clear', () async {
      for (var i = 0; i < 600; i++) {
        store.edit('transactions', 'id$i', 1000 + i);
      }
      final r = await engine.sync();
      expect(r.ok, isTrue);
      expect(r.pushed, 600);
      expect(hub.syncRequestCount, 2);
      expect((hub.receivedBodies[0]['changes'] as List).length, 500);
      expect((hub.receivedBodies[1]['changes'] as List).length, 100);
      expect(store.dirtyCount, 0);
      expect(hub.rows('expenses'), hasLength(600));
    });

    test('pull pages at 500 and continues while more is true', () async {
      hub.seedFromOtherDevice(
          'expenses', [for (var i = 0; i < 1250; i++) change('r$i', 10 + i)]);
      final r = await engine.sync();
      expect(r.ok, isTrue);
      expect(r.pulled, 1250);
      expect(hub.syncRequestCount, 3);
      expect(store.rowCount, 1250);
      expect(store.dirtyCount, 0);
      expect(await store.getCursor(), hub.latestSeq);
    });
  });

  group('failures', () {
    test('dropped response: rows stay dirty, retry is idempotent', () async {
      store.edit('transactions', 'a', 100);
      hub.failNext(1, MockFailure.dropAfterProcessing);
      final r1 = await engine.sync();
      expect(r1.ok, isFalse);
      expect(store.isDirty('transactions', 'a'), isTrue);
      expect(hub.rows('expenses'), hasLength(1)); // hub did get it

      final r2 = await engine.sync();
      expect(r2.ok, isTrue);
      expect(hub.rows('expenses'), hasLength(1));
      expect(hub.row('expenses', 'transactions', 'a')!.updatedAt, 100);
      expect(store.dirtyCount, 0);
    });

    test('timeout returns an error and leaves rows dirty', () async {
      final slow = makeClient(total: const Duration(milliseconds: 200));
      final e = SyncEngine(client: slow, store: store, app: 'expenses');
      store.edit('transactions', 'a', 100);
      hub.latency = const Duration(milliseconds: 800);
      final r = await e.sync();
      expect(r.ok, isFalse);
      expect(r.error, contains('timed out'));
      expect(store.isDirty('transactions', 'a'), isTrue);
      slow.close();
      await Future<void>.delayed(const Duration(milliseconds: 700));
    });

    test('wrong token fails with an auth error and leaks nothing', () async {
      final bad = makeClient(token: 'nope');
      final e = SyncEngine(client: bad, store: store, app: 'expenses');
      store.edit('transactions', 'a', 100);
      final r = await e.sync();
      expect(r.ok, isFalse);
      expect(r.error, contains('401'));
      expect(r.error, isNot(contains('nope')));
      expect(store.isDirty('transactions', 'a'), isTrue);
      expect(hub.rows('expenses'), isEmpty);
      bad.close();
    });

    test('unreachable hub fails quietly; second URL is used as fallback',
        () async {
      final c = makeClient(urls: ['http://127.0.0.1:1', hub.url]);
      final e = SyncEngine(client: c, store: store, app: 'expenses');
      store.edit('transactions', 'a', 100);
      final r = await e.sync();
      expect(r.ok, isTrue);
      expect(store.dirtyCount, 0);
      c.close();

      final dead = makeClient(urls: ['http://127.0.0.1:1']);
      final e2 = SyncEngine(client: dead, store: store, app: 'expenses');
      store.edit('transactions', 'b', 100);
      final r2 = await e2.sync();
      expect(r2.ok, isFalse);
      expect(store.isDirty('transactions', 'b'), isTrue);
      dead.close();
    });

    test('cursor is not advanced when applyRemote throws', () async {
      hub.seedFromOtherDevice('expenses', [change('r1', 10)]);
      store.failApplyWith = StateError('missing parent');
      final r = await engine.sync();
      expect(r.ok, isFalse);
      expect(r.error, contains('StateError'));
      expect(await store.getCursor(), 0);

      store.failApplyWith = null;
      final r2 = await engine.sync();
      expect(r2.ok, isTrue);
      expect(store.get('transactions', 'r1'), isNotNull);
      expect(await store.getCursor(), hub.latestSeq);
    });

    test('a SyncStoreException message is reported, other types by name',
        () async {
      hub.seedFromOtherDevice('expenses', [change('r1', 10)]);
      store.failApplyWith = SyncStoreException('Could not apply x/1: bad');
      expect((await engine.sync()).error, 'Could not apply x/1: bad');
      store.failApplyWith = StateError('secret ${hub.token}');
      final r = await engine.sync();
      expect(r.error, 'Sync failed: StateError');
    });

    test('hub reset: cursor drops to 0, rows re-marked dirty and re-pushed',
        () async {
      store.edit('transactions', 'a', 100);
      store.edit('transactions', 'b', 200);
      expect((await engine.sync()).ok, isTrue);
      hub.seedFromOtherDevice('expenses', [change('c', 300)]);
      await engine.sync();
      final before = await store.getCursor();
      expect(before, greaterThan(0));
      expect(store.dirtyCount, 0);

      hub.reset();
      expect(hub.rows('expenses'), isEmpty);
      final r = await engine.sync();
      expect(r.ok, isTrue, reason: r.error);
      expect(store.resetCalls, 1);
      expect(hub.rows('expenses').map((c) => c.id).toSet(), {'a', 'b', 'c'});
      expect(store.dirtyCount, 0);
      expect(await store.getCursor(), lessThanOrEqualTo(hub.latestSeq));
    });

    test('a store without HubResetAware still resets its cursor', () async {
      final plain = _PlainStore();
      final e = SyncEngine(client: client, store: plain, app: 'expenses');
      plain.cursor = 99;
      expect((await e.sync()).ok, isTrue);
      expect(plain.cursor, lessThan(99));
    });

    test('backoff grows on failure and resets on success', () async {
      var now = DateTime.utc(2026, 1, 1);
      final e = SyncEngine(
        client: client,
        store: store,
        app: 'expenses',
        now: () => now,
      );
      hub.failNext(2, MockFailure.serverError);
      expect(e.shouldBackOff, isFalse);
      await e.sync();
      expect(e.shouldBackOff, isTrue);
      expect(e.nextAllowedAt, now.add(const Duration(seconds: 5)));
      await e.sync();
      expect(e.nextAllowedAt, now.add(const Duration(seconds: 10)));
      now = now.add(const Duration(seconds: 11));
      expect(e.shouldBackOff, isFalse);
      final ok = await e.sync();
      expect(ok.ok, isTrue);
      expect(e.nextAllowedAt, isNull);
      expect(e.failureCount, 0);
    });
  });

  group('concurrency', () {
    test('two concurrent syncs run once', () async {
      store.edit('transactions', 'a', 100);
      final f1 = engine.sync();
      final f2 = engine.sync();
      expect(identical(f1, f2), isTrue);
      await Future.wait([f1, f2]);
      expect(hub.syncRequestCount, 1);
      expect(engine.isSyncing, isFalse);
      await engine.sync();
      expect(hub.syncRequestCount, 2);
    });

    test('markSynced does not clear a row edited mid-sync', () async {
      store.edit('transactions', 'a', 100, data: {'v': 1});
      hub.onSyncRequest = (_) async {
        store.edit('transactions', 'a', 150, data: {'v': 2});
      };
      final r = await engine.sync();
      expect(r.ok, isTrue);
      expect(store.isDirty('transactions', 'a'), isTrue);
      expect(store.get('transactions', 'a')!.data['v'], 2);

      hub.onSyncRequest = null;
      await engine.sync();
      expect(store.isDirty('transactions', 'a'), isFalse);
      expect(hub.row('expenses', 'transactions', 'a')!.data['v'], 2);
    });
  });

  test('health works without and with a token', () async {
    final anon = makeClient(token: 'wrong');
    final h0 = await anon.health();
    expect(h0.ok, isTrue);
    expect(h0.hubVersion, isNull);
    final h1 = await client.health();
    expect(h1.hubVersion, 'mock-1');
    anon.close();
  });

  test('SyncChange JSON round trip', () {
    final c = SyncChange(
        table: 't',
        id: 'x',
        updatedAt: 5,
        deleted: true,
        data: {'a': 1},
        seq: 9);
    final j = c.toJson();
    expect(j['deleted'], 1);
    final back = SyncChange.fromJson(j);
    expect(back.deleted, isTrue);
    expect(back.seq, 9);
    expect(back.data, {'a': 1});
  });
}

/// Minimal store that does not implement [HubResetAware].
class _PlainStore implements SyncStore {
  int cursor = 0;
  @override
  Future<List<SyncChange>> readDirty(int limit) async => const [];
  @override
  Future<void> markSynced(List<SyncChange> pushed) async {}
  @override
  Future<void> applyRemote(List<SyncChange> changes) async {}
  @override
  Future<int> getCursor() async => cursor;
  @override
  Future<void> setCursor(int c) async => cursor = c;
}
