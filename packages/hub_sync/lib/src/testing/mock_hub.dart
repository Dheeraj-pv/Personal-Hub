import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../sync_change.dart';

/// How an injected failure behaves.
enum MockFailure {
  /// Close the connection before the request is processed.
  dropBeforeProcessing,

  /// Process (store) the request, then close without answering. Simulates a
  /// lost response; the client must retry safely.
  dropAfterProcessing,

  /// Answer HTTP 500 without processing.
  serverError,
}

class _StoredRow {
  _StoredRow(this.change, this.deviceId);
  SyncChange change; // always has seq
  String deviceId;
}

/// In-process hub implementing the sync contract on an ephemeral port.
///
/// Rules: bearer-token check (401); every accepted change gets a rising
/// `seq`; incoming `updated_at >= stored` wins (ties keep incoming), else the
/// stored row is returned in `changes` regardless of cursor; pulls page at
/// [pageSize]; rows pushed by the requesting device are not echoed back.
class MockHub {
  MockHub._(this._server, this.token);

  static const pageSize = 500;
  static const maxBodyBytes = 2 * 1024 * 1024;
  static const maxChanges = 500;
  static const apps = {'expenses', 'notes'};

  final HttpServer _server;
  final String token;

  final Map<String, Map<String, _StoredRow>> _rows = {};
  final Map<String, DateTime> _lastSync = {};
  final Map<String, Set<String>> _devices = {};
  final List<MockFailure> _failures = [];
  int _seq = 0;
  int _generation = 0;

  /// Set false to behave like an older hub that sends no `instance_id`.
  bool sendInstanceId = true;

  /// Changes on [reset], like the real hub's `instance_id` after a wipe.
  String get instanceId => 'mock-instance-$_generation';

  /// Added before every request is processed (use to trigger client timeouts).
  Duration latency = Duration.zero;

  /// Called after the body is parsed, before it is processed. Lets a test do
  /// something "while the sync is in flight" (e.g. edit a local row).
  Future<void> Function(Map<String, dynamic> body)? onSyncRequest;

  /// Number of /sync requests received (including dropped ones).
  int syncRequestCount = 0;

  /// Bodies of /sync requests that were processed or dropped, in order.
  final List<Map<String, dynamic>> receivedBodies = [];

  static Future<MockHub> start({String token = 'test-token'}) async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final hub = MockHub._(server, token);
    server.listen(hub._handle);
    return hub;
  }

  String get url => 'http://127.0.0.1:${_server.port}';
  int get latestSeq => _seq;

  /// Forget every stored row and restart `seq` from 0, like a wiped or
  /// replaced hub.
  void reset() {
    _rows.clear();
    _devices.clear();
    _lastSync.clear();
    _seq = 0;
    _generation++;
  }

  Future<void> stop() => _server.close(force: true);

  /// Make the next [count] sync requests fail in the given [mode].
  void failNext(int count,
      [MockFailure mode = MockFailure.dropBeforeProcessing]) {
    for (var i = 0; i < count; i++) {
      _failures.add(mode);
    }
  }

  /// Rows stored for [app] (including tombstones), by `table/id`.
  List<SyncChange> rows(String app) =>
      [for (final r in _rows[app]?.values ?? <_StoredRow>[]) r.change];

  SyncChange? row(String app, String table, String id) =>
      _rows[app]?['$table/$id']?.change;

  /// Pretend another device pushed [changes]; they get new seqs (the same
  /// conflict rule applies).
  void seedFromOtherDevice(String app, List<SyncChange> changes,
      {String deviceId = 'other-device'}) {
    _devices.putIfAbsent(app, () => {}).add(deviceId);
    for (final c in changes) {
      _accept(app, c, deviceId);
    }
  }

  // Returns the stored row if the incoming change was refused.
  SyncChange? _accept(String app, SyncChange c, String deviceId) {
    final map = _rows.putIfAbsent(app, () => {});
    final key = '${c.table}/${c.id}';
    final existing = map[key];
    if (existing != null && c.updatedAt < existing.change.updatedAt) {
      return existing.change;
    }
    map[key] = _StoredRow(
      SyncChange(
        table: c.table,
        id: c.id,
        updatedAt: c.updatedAt,
        deleted: c.deleted,
        data: c.data,
        seq: ++_seq,
      ),
      deviceId,
    );
    return null;
  }

  Future<void> _handle(HttpRequest req) async {
    try {
      await _route(req);
    } on Object {
      // Client went away (timeout) or response already detached.
    }
  }

  Future<void> _route(HttpRequest req) async {
    final path = req.uri.path;
    final authed = req.headers.value('authorization') == 'Bearer $token';
    if (req.method == 'GET' && path == '/health') {
      final body = <String, dynamic>{'status': 'ok'};
      if (authed) {
        body['hub_version'] = 'mock-1';
        if (sendInstanceId) body['instance_id'] = instanceId;
        body['server_time'] = DateTime.now().toUtc().millisecondsSinceEpoch;
        body['apps'] = {
          for (final a in _rows.keys)
            a: {
              'last_sync_at': _lastSync[a]?.toUtc().toIso8601String(),
              'row_count': _rows[a]!.length,
              'devices': (_devices[a] ?? {}).toList(),
            },
        };
      }
      return _json(req, 200, body);
    }
    final m = RegExp(r'^/sync/([^/]+)$').firstMatch(path);
    if (req.method != 'POST' || m == null) {
      return _json(req, 404, {'error': 'not found'});
    }
    syncRequestCount++;
    if (!authed) return _json(req, 401, {'error': 'unauthorized'});
    final app = m.group(1)!;
    if (!apps.contains(app)) return _json(req, 404, {'error': 'unknown app'});

    final bytes = <int>[];
    var total = 0;
    await for (final chunk in req) {
      total += chunk.length;
      if (total <= maxBodyBytes) bytes.addAll(chunk); // keep draining if over
    }
    if (total > maxBodyBytes) {
      return _json(req, 413, {'error': 'body too large'});
    }
    final Map<String, dynamic> body;
    try {
      body = jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
    } on Object {
      return _json(req, 400, {'error': 'bad json'});
    }
    if ((body['changes'] as List).length > maxChanges) {
      return _json(req, 400, {'error': 'more than $maxChanges changes'});
    }
    receivedBodies.add(body);

    final failure = _failures.isEmpty ? null : _failures.removeAt(0);
    if (failure == MockFailure.dropBeforeProcessing) return _drop(req);
    if (failure == MockFailure.serverError) {
      return _json(req, 500, {'error': 'injected'});
    }
    if (latency > Duration.zero) await Future<void>.delayed(latency);
    await onSyncRequest?.call(body);

    final result = _process(app, body);
    if (failure == MockFailure.dropAfterProcessing) return _drop(req);
    return _json(req, 200, result);
  }

  Map<String, dynamic> _process(String app, Map<String, dynamic> body) {
    final deviceId = body['device_id'] as String;
    final cursor = (body['cursor'] as num).toInt();
    _devices.putIfAbsent(app, () => {}).add(deviceId);
    _lastSync[app] = DateTime.now();

    final refused = <SyncChange>[];
    for (final raw in (body['changes'] as List)) {
      final c = SyncChange.fromJson(Map<String, dynamic>.from(raw as Map));
      final stored = _accept(app, c, deviceId);
      if (stored != null) refused.add(stored);
    }

    final limit = (pageSize - refused.length).clamp(0, pageSize);
    final candidates = (_rows[app]?.values ?? <_StoredRow>[])
        .where((r) => r.change.seq! > cursor)
        .toList()
      ..sort((a, b) => a.change.seq!.compareTo(b.change.seq!));
    final page = candidates.take(limit).toList();
    final more = candidates.length > page.length;
    final newCursor = more
        ? page.last.change.seq!
        : (candidates.isEmpty ? cursor : candidates.last.change.seq!);
    final out = <SyncChange>[
      ...refused,
      for (final r in page)
        if (r.deviceId != deviceId) r.change,
    ];
    // A client ahead of this hub (hub was reset) is told the hub's position.
    final reported =
        cursor > _seq ? _seq : (newCursor > cursor ? newCursor : cursor);
    return {
      'cursor': reported,
      'changes': [for (final c in out) c.toJson()],
      'more': more,
      if (sendInstanceId) 'instance_id': instanceId,
    };
  }

  Future<void> _drop(HttpRequest req) async {
    final socket = await req.response.detachSocket(writeHeaders: false);
    socket.destroy();
  }

  Future<void> _json(HttpRequest req, int status, Object body) async {
    req.response
      ..statusCode = status
      ..headers.contentType = ContentType.json
      ..write(jsonEncode(body));
    await req.response.close();
  }
}
