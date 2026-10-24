/// Connection settings for the hub.
class HubConfig {
  const HubConfig({
    required this.urls,
    required this.token,
    required this.deviceId,
    required this.appName,
  });

  /// Base URLs (no trailing path), tried in order. Usually just one.
  final List<String> urls;

  /// Bearer token. Never log this.
  final String token;

  /// Stable id of this install, sent with every sync request.
  final String deviceId;

  /// Hub app namespace: `expenses` or `notes`.
  final String appName;
}
