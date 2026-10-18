/// P7 移动端数据模型。
///
/// 计划书依据：三、P7（账号登录、会话凭证、流式文字、设备上报、本地缓存）
/// 端云分层：模型只描述**交互所需的数据**，不含任何业务决策逻辑。
library;

/// 会话凭证。**不包含密码**——明文密码永不在本地持久化（三、P7 步骤3）。
class SessionCredentials {
  const SessionCredentials({
    required this.userId,
    required this.token,
    required this.issuedAt,
    required this.expiresAt,
    this.idleTimeoutSeconds = 0,
    this.deviceId = '',
  });

  final String userId;
  final String token;
  final DateTime issuedAt;
  final DateTime expiresAt;

  /// 空闲超时（秒）。0 表示不按空闲判定。
  final int idleTimeoutSeconds;
  final String deviceId;

  /// 本地判定是否已过期。长时间静置凭此提示重登（三、P7 步骤8）。
  bool isExpired(DateTime now, {DateTime? lastActiveAt}) {
    if (!now.isBefore(expiresAt)) return true;
    if (idleTimeoutSeconds > 0 && lastActiveAt != null) {
      final idle = now.difference(lastActiveAt).inSeconds;
      if (idle >= idleTimeoutSeconds) return true;
    }
    return false;
  }

  Map<String, dynamic> toJson() => {
        'userId': userId,
        'token': token,
        'issuedAt': issuedAt.toIso8601String(),
        'expiresAt': expiresAt.toIso8601String(),
        'idleTimeoutSeconds': idleTimeoutSeconds,
        'deviceId': deviceId,
      };

  static SessionCredentials fromJson(Map<String, dynamic> json) =>
      SessionCredentials(
        userId: json['userId'] as String,
        token: json['token'] as String,
        issuedAt: DateTime.parse(json['issuedAt'] as String),
        expiresAt: DateTime.parse(json['expiresAt'] as String),
        idleTimeoutSeconds: (json['idleTimeoutSeconds'] as num?)?.toInt() ?? 0,
        deviceId: (json['deviceId'] as String?) ?? '',
      );
}

/// 一条对话消息。
enum ChatRole { user, assistant }

class ChatMessage {
  ChatMessage({
    required this.role,
    required this.text,
    DateTime? at,
    this.sessionExpired = false,
    this.riskNoted = false,
  }) : at = at ?? DateTime.now();

  final ChatRole role;
  String text;
  final DateTime at;

  /// 该条是否因会话失效被中断（三、P7 步骤5：收到会话失效返回弹窗提示重新登录）
  bool sessionExpired;

  /// 该条是否带风险标注（来自 P5/P6 的风险标注透传）
  bool riskNoted;

  Map<String, dynamic> toJson() => {
        'role': role.name,
        'text': text,
        'at': at.toIso8601String(),
        'sessionExpired': sessionExpired,
        'riskNoted': riskNoted,
      };

  static ChatMessage fromJson(Map<String, dynamic> json) => ChatMessage(
        role: ChatRole.values.firstWhere((r) => r.name == json['role'],
            orElse: () => ChatRole.assistant),
        text: (json['text'] as String?) ?? '',
        at: DateTime.tryParse((json['at'] as String?) ?? '') ?? DateTime.now(),
        sessionExpired: (json['sessionExpired'] as bool?) ?? false,
        riskNoted: (json['riskNoted'] as bool?) ?? false,
      );
}

/// 云端下发的流式分片。
///
/// 计划书三、P7 步骤5「逐字文字渲染 + 同步 TTS 播报」：
/// 文本片用于逐字渲染，携带的 speech 片交给 TTS；两者同序同源才能同步。
class StreamChunk {
  const StreamChunk({
    required this.index,
    required this.text,
    this.isLast = false,
    this.speech = '',
    this.carriesRisk = false,
  });

  final int index;
  final String text;
  final bool isLast;

  /// 该片对应的播报文本；空表示只渲染不播报。
  final String speech;

  /// 是否携带风险提示（移动端据此做视觉/语音上的区别处理）
  final bool carriesRisk;

  static StreamChunk fromJson(Map<String, dynamic> json) => StreamChunk(
        index: (json['index'] as num?)?.toInt() ?? 0,
        text: (json['text'] as String?) ?? '',
        isLast: (json['is_last'] as bool?) ?? false,
        speech: (json['speech'] as String?) ?? '',
        carriesRisk: (json['carries_risk'] as bool?) ?? false,
      );
}

/// 设备上报载荷（三、P7 步骤7）。
class DeviceReport {
  const DeviceReport({
    required this.userId,
    required this.sessionId,
    required this.reportedAt,
    this.latitude,
    this.longitude,
    this.batteryLevel,
    this.networkType,
  });

  final String userId;
  final String sessionId;
  final DateTime reportedAt;
  final double? latitude;
  final double? longitude;
  final int? batteryLevel;
  final String? networkType;

  Map<String, dynamic> toJson() => {
        'user_id': userId,
        'session_id': sessionId,
        'reported_at': reportedAt.toIso8601String(),
        if (latitude != null) 'latitude': latitude,
        if (longitude != null) 'longitude': longitude,
        if (batteryLevel != null) 'battery_level': batteryLevel,
        if (networkType != null) 'network_type': networkType,
      };
}
