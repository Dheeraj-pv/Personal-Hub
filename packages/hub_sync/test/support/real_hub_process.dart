import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

/// Runs the Python hub (`python -m hub.main`) on a free loopback port with a
/// temporary database and a random token. The token is never printed.
class RealHubProcess {
  RealHubProcess._(this._repo, this._tmp, this.port, this.token);

  final Directory _repo;
  final Directory _tmp;
  final int port;
  final String token;
  Process? _proc;
  final StringBuffer _log = StringBuffer();

  String get url => 'http://127.0.0.1:$port';
  File get dbFile => File('${_tmp.path}/hub.db');

  /// Hub repo from `HUB_REPO`, or two levels up from the package.
  static Directory repoDir() {
    final env = Platform.environment['HUB_REPO'];
    return Directory(env != null && env.isNotEmpty ? env : '../..').absolute;
  }

  static Future<RealHubProcess> create() async {
    final repo = repoDir();
    if (!File('${repo.path}/pyproject.toml').existsSync()) {
      throw StateError('HUB_REPO does not look like the hub repo: ${repo.path}');
    }
    final tmp = await Directory.systemTemp.createTemp('real_hub_test_');
    final rnd = Random.secure();
    final token = base64Url
        .encode([for (var i = 0; i < 30; i++) rnd.nextInt(256)]); // 40 chars
    return RealHubProcess._(repo, tmp, await _freePort(), token);
  }

  static Future<int> _freePort() async {
    final s = await ServerSocket.bind(InternetAddress.loopbackIPv4, 0);
    final p = s.port;
    await s.close();
    return p;
  }

  /// Starts the hub and waits until /health answers.
  Future<void> start({Duration timeout = const Duration(seconds: 60)}) async {
    final proc = await Process.start(
      'uv',
      ['run', 'python', '-m', 'hub.main'],
      workingDirectory: _repo.path,
      environment: {
        'HUB_TOKEN': token,
        'HUB_DB': dbFile.path,
        'HUB_PORT': '$port',
        'HUB_HOST': '127.0.0.1',
      },
    );
    _proc = proc;
    // Drain output so the pipes never fill up; keep a bounded tail for errors.
    void keep(List<int> d) {
      if (_log.length < 4000) _log.write(utf8.decode(d, allowMalformed: true));
    }

    proc.stdout.listen(keep);
    proc.stderr.listen(keep);
    var exited = false;
    unawaited(proc.exitCode.then((_) => exited = true));

    final deadline = DateTime.now().add(timeout);
    while (!await _healthy()) {
      if (exited || DateTime.now().isAfter(deadline)) {
        final out = _log.toString().replaceAll(token, '<token>');
        await stop();
        throw StateError('Hub did not become healthy. Output:\n$out');
      }
      await Future<void>.delayed(const Duration(milliseconds: 100));
    }
  }

  Future<bool> _healthy() async {
    final c = HttpClient()..connectionTimeout = const Duration(seconds: 1);
    try {
      final req = await c.getUrl(Uri.parse('$url/health'));
      final res = await req.close();
      await res.drain<void>();
      return res.statusCode == 200;
    } on Object {
      return false;
    } finally {
      c.close(force: true);
    }
  }

  /// Stops the hub and waits until the port no longer answers.
  Future<void> stop() async {
    final proc = _proc;
    _proc = null;
    if (proc == null) return;
    proc.kill(ProcessSignal.sigterm);
    try {
      await proc.exitCode.timeout(const Duration(seconds: 10));
    } on TimeoutException {
      proc.kill(ProcessSignal.sigkill);
      await proc.exitCode;
    }
    final deadline = DateTime.now().add(const Duration(seconds: 10));
    while (await _healthy() && DateTime.now().isBefore(deadline)) {
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }
  }

  /// Deletes hub.db, hub.db-wal and hub.db-shm (hub must be stopped).
  void wipeDb() {
    for (final suffix in ['', '-wal', '-shm', '-journal']) {
      final f = File('${dbFile.path}$suffix');
      if (f.existsSync()) f.deleteSync();
    }
  }

  /// Kills the hub (even after a failed test) and removes the temp dir.
  Future<void> dispose() async {
    await stop();
    if (_tmp.existsSync()) await _tmp.delete(recursive: true);
  }
}
