import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';

import 'hub_config.dart';
import 'sync_change.dart';

/// Base class for hub failures. [message] never contains the token.
class HubException implements Exception {
  HubException(this.message);
  final String message;
  @override
  String toString() => 'HubException: $message';
}

/// Hub unreachable or timed out on every configured URL.
class HubNetworkException extends HubException {
  HubNetworkException(super.message);
}

/// 401/403: the token is wrong. Retrying will not help.
class HubAuthException extends HubException {
  HubAuthException() : super('Hub rejected the token (401)');
}

/// Unexpected HTTP status or malformed body.
class HubServerException extends HubException {
  HubServerException(super.message, {this.statusCode});
  final int? statusCode;
}

/// Hub answer to a sync request.
class SyncResponse {
  const SyncResponse({
    required this.cursor,
    required this.changes,
    required this.more,
  });
  final int cursor;
  final List<SyncChange> changes;
  final bool more;
}

/// Result of GET /health.
class HubHealth {
  const HubHealth(this.raw);
  final Map<String, dynamic> raw;
  bool get ok => raw['status'] == 'ok';
  String? get hubVersion => raw['hub_version'] as String?;
}

/// HTTP client for the hub. Tries [HubConfig.urls] in order (starting with
/// the last one that worked) and moves on only for network errors, timeouts
/// and 5xx answers.
class HubClient {
  HubClient(
    this.config, {
    http.Client? httpClient,
    this.connectTimeout = const Duration(seconds: 3),
    this.totalTimeout = const Duration(seconds: 15),
  }) : _http = httpClient ??
            IOClient(HttpClient()..connectionTimeout = connectTimeout);

  final HubConfig config;
  final Duration connectTimeout;
  final Duration totalTimeout;
  final http.Client _http;
  int _preferred = 0;

  Map<String, String> get _headers => {
        'Authorization': 'Bearer ${config.token}',
        'Content-Type': 'application/json',
      };

  Future<HubHealth> health() async {
    final body = await _request(
      (base) => _http.get(Uri.parse('$base/health'), headers: _headers),
    );
    return HubHealth(body);
  }

  Future<SyncResponse> sync(
    String app,
    int cursor,
    List<SyncChange> changes,
  ) async {
    final payload = jsonEncode({
      'device_id': config.deviceId,
      'cursor': cursor,
      'changes': [for (final c in changes) c.toJson()],
    });
    final body = await _request(
      (base) => _http.post(Uri.parse('$base/sync/$app'),
          headers: _headers, body: payload),
    );
    try {
      return SyncResponse(
        cursor: (body['cursor'] as num).toInt(),
        changes: [
          for (final c in (body['changes'] as List))
            SyncChange.fromJson(Map<String, dynamic>.from(c as Map)),
        ],
        more: body['more'] == true,
      );
    } on Object {
      throw HubServerException('Malformed sync response');
    }
  }

  void close() => _http.close();

  Future<Map<String, dynamic>> _request(
    Future<http.Response> Function(String base) send,
  ) async {
    final urls = config.urls;
    if (urls.isEmpty) throw HubNetworkException('No hub URL configured');
    Object? lastError;
    for (var i = 0; i < urls.length; i++) {
      final idx = (_preferred + i) % urls.length;
      final base = urls[idx].replaceFirst(RegExp(r'/+$'), '');
      try {
        final res = await send(base).timeout(totalTimeout);
        if (res.statusCode == 401 || res.statusCode == 403) {
          throw HubAuthException();
        }
        if (res.statusCode >= 500) {
          lastError = HubServerException('Hub error ${res.statusCode}',
              statusCode: res.statusCode);
          continue;
        }
        if (res.statusCode != 200) {
          throw HubServerException('Unexpected status ${res.statusCode}',
              statusCode: res.statusCode);
        }
        _preferred = idx;
        final decoded = jsonDecode(utf8.decode(res.bodyBytes));
        if (decoded is! Map) throw HubServerException('Malformed response');
        return Map<String, dynamic>.from(decoded);
      } on HubException {
        rethrow;
      } on TimeoutException {
        lastError = HubNetworkException('Hub timed out');
      } on SocketException catch (e) {
        lastError = HubNetworkException('Cannot reach hub: ${e.message}');
      } on http.ClientException catch (e) {
        lastError = HubNetworkException('Cannot reach hub: ${e.message}');
      } on FormatException {
        throw HubServerException('Malformed response');
      }
    }
    throw lastError is HubException
        ? lastError
        : HubNetworkException('Cannot reach hub');
  }
}
