/// P7 移动端数据模型。
///
/// 计划书依据：三、P7（账号登录、会话凭证、流式文字、设备上报、本地缓存）
/// 端云分层：模型只描述**交互所需的数据**，不含任何业务决策逻辑。
library;

import 'dart:convert';
import 'dart:typed_data';

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
    this.imageBytes,
    this.outputImage,
  }) : at = at ?? DateTime.now();

  final ChatRole role;
  String text;
  final DateTime at;

  /// 该条是否因会话失效被中断（三、P7 步骤5：收到会话失效返回弹窗提示重新登录）
  bool sessionExpired;

  /// 该条是否带风险标注（来自 P5/P6 的风险标注透传）
  bool riskNoted;

  /// 用户随消息上传的图片原始字节（图片输入，台账 U61）。
  /// 存字节而不是路径：路径会失效，而这条消息要能长期回看。
  Uint8List? imageBytes;

  /// 云端返回的图文输出图（结果卡片，台账 U62）。
  Uint8List? outputImage;

  Map<String, dynamic> toJson() => {
        'role': role.name,
        'text': text,
        'at': at.toIso8601String(),
        'sessionExpired': sessionExpired,
        'riskNoted': riskNoted,
        // 图片不入本地缓存：体积大且含隐私，只保留在内存中的当前会话
        if (imageBytes != null) 'hasImage': true,
        if (outputImage != null) 'hasOutputImage': true,
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

/// 图文输出中的一个内容块。**顺序有意义**：正文在前、结果图在后。
///
/// 台账 U61/U62：图片输入与图文输出为计划书未定义项，由人工指令追加。
class VisionBlock {
  VisionBlock({
    required this.type,
    this.text = '',
    this.mime = '',
    this.purpose = '',
    Uint8List? bytes,
  }) : bytes = bytes ?? Uint8List(0);

  /// 'text' 或 'image'
  final String type;
  final String text;
  final String mime;

  /// 图片用途，例如 'card'（识别结果卡片）
  final String purpose;

  /// 图片原始字节（服务端给的 base64 已在此解出）
  final Uint8List bytes;

  bool get isImage => type == 'image';
  bool get isText => type == 'text';

  static VisionBlock fromJson(Map<String, dynamic> json) {
    final raw = json['data_base64'];
    Uint8List bytes = Uint8List(0);
    if (raw is String && raw.isNotEmpty) {
      // base64 解不开就抛 FormatException，由上层当失败处理 ——
      // 绝不返回一张"空图"让界面显示成识别成功
      bytes = base64Decode(raw);
    }
    return VisionBlock(
      type: (json['type'] as String?) ?? 'text',
      text: (json['text'] as String?) ?? '',
      mime: (json['mime'] as String?) ?? '',
      purpose: (json['purpose'] as String?) ?? '',
      bytes: bytes,
    );
  }
}

/// 一次图文识别的完整结果。
class VisionResult {
  const VisionResult({
    required this.text,
    required this.provider,
    required this.model,
    required this.elapsedMs,
    required this.blocks,
    this.facts = const <String, dynamic>{},
  });

  /// 模型输出的识别文本
  final String text;

  /// 实际使用的供应商与模型 —— **如实回报，便于追责与排查**
  final String provider;
  final String model;
  final int elapsedMs;

  /// 有序内容块（文字 + 图片）
  final List<VisionBlock> blocks;

  /// 客观图像事实（尺寸、亮度、主色），不依赖模型
  final Map<String, dynamic> facts;

  /// 所有图片块
  List<VisionBlock> get images => blocks.where((b) => b.isImage).toList();

  /// 结果卡片（图文输出里的那一张图），没有则为 null
  VisionBlock? get card {
    for (final block in blocks) {
      if (block.isImage && block.purpose == 'card') return block;
    }
    return null;
  }

  static VisionResult fromJson(Map<String, dynamic> json) {
    final rawBlocks = json['blocks'];
    final blocks = <VisionBlock>[];
    if (rawBlocks is List) {
      for (final item in rawBlocks) {
        if (item is Map) {
          blocks.add(VisionBlock.fromJson(Map<String, dynamic>.from(item)));
        }
      }
    }
    // 服务端没给 blocks 时用 text 兜底：至少文字不能丢
    if (blocks.isEmpty) {
      blocks.add(VisionBlock(
          type: 'text', text: (json['text'] as String?) ?? ''));
    }
    return VisionResult(
      text: (json['text'] as String?) ?? '',
      provider: (json['provider'] as String?) ?? '',
      model: (json['model'] as String?) ?? '',
      elapsedMs: (json['elapsed_ms'] as num?)?.toInt() ?? 0,
      blocks: blocks,
      facts: (json['facts'] as Map?)?.cast<String, dynamic>() ??
          const <String, dynamic>{},
    );
  }
}

/// 文字查询链路的 SSE 事件（U64/U66）。
///
/// 服务端契约：`accepted` → `plan` → `chunk`* → [`revise`] → `done`；
/// 歧义走 `clarify`，异常走 `error`。
///
/// **`revise` 的语义是「覆盖」，不是「追加」**：服务端逐字下发后又做了回答复检，
/// 复检弃答时把整段答案换掉。UI 必须把已有文本替换成 `text`，否则用户会看到
/// 「一半是编的、一半是弃答说明」的拼接结果。
enum ChatEventType {
  accepted,
  plan,
  chunk,
  revise,
  clarify,
  error,
  done;

  static ChatEventType parse(String? raw) {
    for (final type in ChatEventType.values) {
      if (type.name == raw) return type;
    }
    return ChatEventType.chunk;   // 未知事件按文本片处理：宁可多渲染一句，也别吞内容
  }
}

class ChatStreamEvent {
  const ChatStreamEvent({
    required this.type,
    this.text = '',
    this.speech = '',
    this.data = const <String, dynamic>{},
  });

  factory ChatStreamEvent.fromJson(Map<String, dynamic> json) => ChatStreamEvent(
        type: ChatEventType.parse(json['type'] as String?),
        text: (json['text'] as String?) ?? '',
        speech: (json['speech'] as String?) ?? '',
        data: (json['data'] as Map?)?.cast<String, dynamic>() ??
            const <String, dynamic>{},
      );

  final ChatEventType type;
  final String text;

  /// 播报文本。访客层级服务端不下发（无 voice_output 能力）。
  final String speech;

  final Map<String, dynamic> data;

  bool get isChunk => type == ChatEventType.chunk;
  bool get isTerminal =>
      type == ChatEventType.done || type == ChatEventType.error;
  bool get replacesAnswer => type == ChatEventType.revise;

  /// 分类（来自 `plan` 事件的 `data.category`）。
  String? get category => data['category'] as String?;

  /// 工具序列（来自 `done` 事件的 `data.tools`）。
  List<String> get tools {
    final raw = data['tools'];
    return raw is List ? raw.map((e) => e.toString()).toList() : const [];
  }

  /// 服务端给的机器可读错误码。
  String? get code => data['code'] as String?;
}

/// 语音识别结果（`POST /api/voice/asr`）。
class AsrResult {
  const AsrResult({
    required this.text,
    this.audioSeconds = 0,
    this.speechSeconds = 0,
    this.elapsedMs = 0,
    this.model = '',
  });

  factory AsrResult.fromJson(Map<String, dynamic> json) => AsrResult(
        text: (json['text'] as String?) ?? '',
        audioSeconds: (json['audio_seconds'] as num?)?.toDouble() ?? 0,
        speechSeconds: (json['speech_seconds'] as num?)?.toDouble() ?? 0,
        elapsedMs: (json['elapsed_ms'] as num?)?.toInt() ?? 0,
        model: (json['model'] as String?) ?? '',
      );

  final String text;
  final double audioSeconds;
  final double speechSeconds;
  final int elapsedMs;
  final String model;

  /// 「听到了，但没人说话」与「听清了」是两件事，UI 提示语不同。
  bool get isEmpty => text.trim().isEmpty;
}

/// 声纹校验结论（U63）。**永远带分数**，不给「大概是他」。
class VoiceprintResult {
  const VoiceprintResult({
    required this.matched,
    required this.score,
    this.threshold = 0.35,
    this.speakerId,
    this.code = '',
    this.message = '',
  });

  factory VoiceprintResult.fromJson(Map<String, dynamic> json) => VoiceprintResult(
        matched: (json['matched'] as bool?) ?? false,
        score: (json['score'] as num?)?.toDouble() ?? 0,
        threshold: (json['threshold'] as num?)?.toDouble() ?? 0.35,
        speakerId: json['speaker_id'] as String?,
        code: (json['code'] as String?) ?? '',
        message: (json['message'] as String?) ?? '',
      );

  final bool matched;
  final double score;
  final double threshold;
  final String? speakerId;
  final String code;
  final String message;

  bool get notEnrolled => code == 'not_enrolled';
}

/// 访客凭据（`POST /api/session/guest`）。
class GuestSession {
  const GuestSession({
    required this.userId,
    required this.token,
    required this.expiresAt,
    this.textQueryBudget = 0,
  });

  final String userId;
  final String token;
  final DateTime expiresAt;
  final int textQueryBudget;

  bool isExpired(DateTime now) => !now.isBefore(expiresAt);
}
