/// One synced row on the wire: `{table, id, updated_at, deleted, data, seq?}`.
///
/// [updatedAt] is UTC milliseconds since epoch. [seq] is assigned by the hub
/// and is null for changes that have not been pushed yet.
class SyncChange {
  const SyncChange({
    required this.table,
    required this.id,
    required this.updatedAt,
    required this.deleted,
    required this.data,
    this.seq,
  });

  factory SyncChange.fromJson(Map<String, dynamic> json) => SyncChange(
        table: json['table'] as String,
        id: json['id'] as String,
        updatedAt: (json['updated_at'] as num).toInt(),
        deleted: json['deleted'] == true || json['deleted'] == 1,
        data: Map<String, dynamic>.from(
            (json['data'] as Map?) ?? const <String, dynamic>{}),
        seq: (json['seq'] as num?)?.toInt(),
      );

  final String table;
  final String id;
  final int updatedAt;
  final bool deleted;
  final Map<String, dynamic> data;
  final int? seq;

  /// Wire form. `seq` is only included when set (the hub ignores it on push).
  Map<String, dynamic> toJson() => {
        'table': table,
        'id': id,
        'updated_at': updatedAt,
        'deleted': deleted ? 1 : 0,
        'data': data,
        if (seq != null) 'seq': seq,
      };

  @override
  String toString() => 'SyncChange($table/$id @$updatedAt'
      '${deleted ? ' deleted' : ''}${seq != null ? ' seq=$seq' : ''})';
}
