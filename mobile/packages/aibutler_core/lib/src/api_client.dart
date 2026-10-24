/// P7 云端对接：请求带会话 Token；会话失效统一转成可识别的异常。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤5「对接云端流式接口…**请求带上会话 Token**；
///   **收到会话失效返回弹窗提示重新登录**」
/// - 三、P7 步骤7「上报携带会话身份」
/// - 端云分层：移动端**不做任何智能运算**，只发起请求并渲染结果
library;

import 'dart:convert';
import 'dart:typed_data';

import 'access_policy.dart';
import 'models.dart';
import 'ports.dart';
import 'session.dart';

/// 会话失效。UI 层捕获后弹窗引导重新登录（计划书三、P7 步骤5）。
class SessionExpiredException implements Exception {
  const SessionExpiredException([this.message = '会话已失效，请重新登录']);
  final String message;
  @override
  String toString() => 'SessionExpiredException($message)';
}

/// 未登录时尝试发起私有请求。
class NotSignedInException implements Exception {
  const NotSignedInException([this.message = '未登录，拒绝发起私有请求']);
  final String message;
  @override
  String toString() => 'NotSignedInException($message)';
}

/// 能力不足（服务端 403）。**与「坏了」严格区分**：这个是可以靠登录解决的。
class CapabilityRequiredException implements Exception {
  const CapabilityRequiredException(this.message, {this.capability = '', this.tier = ''});

  final String message;
  final String capability;
  final String tier;

  bool get needsLogin => tier == 'guest';

  @override
  String toString() =>
      'CapabilityRequiredException($capability/$tier): $message';
}

/// 语音链路失败。带服务端 code，让界面能区分「模型没配」与「音频有问题」。
class VoiceFailedException implements Exception {
  const VoiceFailedException(this.message, {this.code = 'unknown', this.statusCode = 0});

  final String message;
  final String code;
  final int statusCode;

  bool get isUnavailable => code == 'speech_unavailable' || statusCode == 503;
  bool get isAudioRejected => code == 'audio_rejected';

  @override
  String toString() => 'VoiceFailedException($statusCode/$code: $message)';
}

/// 图文识别失败。
///
/// 带上服务端的 `code` 与 HTTP 状态码，**让界面能区分「未配置模型」和「图片有问题」**：
/// 前者要引导用户去配模型，后者要引导用户换图，提示语完全不同。
class VisionFailedException implements Exception {
  const VisionFailedException(this.message, {this.code = 'unknown', this.statusCode = 0});

  final String message;
  final String code;
  final int statusCode;

  /// 是否属于「云端没有可用的视觉模型」这类配置问题
  bool get isUnavailable => code == 'vision_unavailable';

  @override
  String toString() => 'VisionFailedException($statusCode/$code: $message)';
}

class ApiClient {
  ApiClient({required TransportPort transport, required SessionManager session})
      : _transport = transport,
        _session = session;

  final TransportPort _transport;
  final SessionManager _session;

  static const String loginPath = '/api/session/login';
  static const String guestPath = '/api/session/guest';
  static const String profilePath = '/api/access/profile';
  static const String chatPath = '/api/chat/stream';
  static const String devicePath = '/api/device/report';
  static const String visionPath = '/api/vision/recognize';
  static const String visionAvailabilityPath = '/api/vision/availability';
  static const String asrPath = '/api/voice/asr';
  static const String ttsPath = '/api/voice/tts';
  static const String voiceAvailabilityPath = '/api/voice/availability';
  static const String speakerEnrollPath = '/api/voice/speaker/enroll';
  static const String speakerVerifyPath = '/api/voice/speaker/verify';
  static const String retrievalPlanPath = '/api/retrieval/plan';

  /// 访客凭据。**刻意不放进 SessionManager**：
  /// `isSignedIn` 的含义是「这是已登录的成员」，设备上报、语音管线、唤醒
  /// 全都依赖它。把访客塞进去会让这些规则以为「用户登录了」，
  /// 从而静默放开本不该开放的行为（E-21 声明≠能力）。
  GuestSession? _guest;

  GuestSession? get guest => _guest;

  /// 落一次访客凭据（登出或登录成功后必须清掉）。
  void adoptGuest(GuestSession? guest) => _guest = guest;

  bool get hasGuest => _guest != null && !_guest!.isExpired(DateTime.now());

  /// 是否存在**成员**会话。语音/视觉/上报这些「登录才有」的判断都看它，
  /// 不看 `hasGuest` —— 访客有凭据但不是成员。
  bool get hasMemberSession {
    final token = _session.bearerToken;
    return token != null && token.isNotEmpty;
  }

  /// 账号密码登录。
  ///
  /// **密码只在本方法内使用一次，不写入任何本地存储**（计划书三、P7 步骤3）。
  Future<SessionCredentials> login({
    required String account,
    required String password,
    required String deviceId,
    required DateTime now,
  }) async {
    final response = await _transport.post(loginPath, body: {
      'account': account,
      'password': password,
      'device_id': deviceId,
    });
    if (!response.ok) {
      throw StateError('登录失败（HTTP ${response.statusCode}）');
    }
    final body = response.body;
    final ttl = (body['expires_in_seconds'] as num?)?.toInt() ?? 0;
    final credentials = SessionCredentials(
      userId: (body['user_id'] as String?) ?? account,
      token: (body['token'] as String?) ?? '',
      issuedAt: now,
      expiresAt: now.add(Duration(seconds: ttl)),
      idleTimeoutSeconds: (body['idle_timeout_seconds'] as num?)?.toInt() ?? 0,
      deviceId: deviceId,
    );
    // 登录成功即丢弃访客身份：两个身份同时存在时，「用哪个 token」会变成猜的
    _guest = null;
    await _session.adopt(credentials, now);
    return credentials;
  }

  /// 取得访客凭据（U64）。未登录也能进「只文字」的对话。
  ///
  /// 服务端关闭访客模式时抛 [CapabilityRequiredException]——
  /// 那种部署形态下仍然要求先登录，端侧不该假装还能用。
  Future<GuestSession> startGuest({
    required DateTime now,
    String deviceId = 'phone',
  }) async {
    final response = await _transport.post(guestPath, body: {'device_id': deviceId});
    if (response.statusCode == 403) {
      throw CapabilityRequiredException(
        (response.body['message'] as String?) ?? '访客模式不可用，请登录后使用',
        capability: 'text_query',
        tier: 'guest',
      );
    }
    if (!response.ok) {
      throw StateError('访客凭据获取失败（HTTP ${response.statusCode}）');
    }
    final body = response.body;
    final ttl = (body['expires_in_seconds'] as num?)?.toInt() ?? 0;
    final guest = GuestSession(
      userId: (body['user_id'] as String?) ?? '',
      token: (body['token'] as String?) ?? '',
      expiresAt: now.add(Duration(seconds: ttl)),
      textQueryBudget: (body['text_query_budget'] as num?)?.toInt() ?? 0,
    );
    if (guest.token.isEmpty) {
      throw StateError('访客凭据为空：服务端未返回 token');
    }
    _guest = guest;
    return guest;
  }

  /// 当前身份的能力档案。**端侧渲染按钮状态以此为准**（服务端是权限真值源）。
  Future<AccessProfile> accessProfile({DateTime? now}) async {
    final token = _token();
    final response = await _transport.post(profilePath, body: const {}, token: token);
    if (response.statusCode == 401) throw const SessionExpiredException();
    if (response.statusCode == 403) {
      final body = response.body;
      throw CapabilityRequiredException(
        (body['message'] as String?) ?? '能力不足',
        capability: (body['capability'] as String?) ?? '',
        tier: (body['tier'] as String?) ?? '',
      );
    }
    if (!response.ok) {
      throw StateError('能力档案获取失败（HTTP ${response.statusCode}）');
    }
    return AccessProfile.fromJson(response.body);
  }

  /// 流式对话（**保留旧接口**：`VoicePipeline` 与既有测试都消费 `StreamChunk`）。
  Stream<StreamChunk> chatStream({
    required String utterance,
    required DateTime now,
    bool manualInput = false,
  }) async* {
    var index = 0;
    await for (final event in chatEvents(utterance: utterance, now: now,
        manualInput: manualInput)) {
      if (event.isChunk || event.replacesAnswer) {
        index++;
        yield StreamChunk(
          index: index,
          text: event.text,
          speech: event.speech,
          isLast: false,
        );
      }
    }
  }

  /// 流式对话的事件接口（U64/U66）。**新代码走这个**，才能拿到 `plan`/`error`/`done`。
  Stream<ChatStreamEvent> chatEvents({
    required String utterance,
    required DateTime now,
    bool manualInput = true,
  }) async* {
    final token = _token();
    if (_session.isSignedIn) await _session.touch(now);
    final stream = _transport.stream(chatPath, body: {
      'utterance': utterance,
      'input_mode': manualInput ? 'text' : 'voice',
      'session_id': _session.current?.deviceId ?? '',
    }, token: token);

    await for (final raw in stream) {
      if ((raw['status_code'] as num?)?.toInt() == 401) {
        throw const SessionExpiredException();
      }
      yield ChatStreamEvent.fromJson(raw);
    }
  }

  /// 上报设备信息。**未登录直接拒绝**，不发出任何请求。
  Future<void> reportDevice(DeviceReport report) async {
    // **设备上报只认成员身份**（三、P7 步骤7「上报携带会话身份」）：
    // 用访客 token 上报等于给匿名方建档，与「未登录不上报」直接冲突。
    final token = _requireMemberToken();
    final response = await _transport.post(devicePath,
        body: report.toJson(), token: token);
    if (response.statusCode == 401) {
      throw const SessionExpiredException();
    }
    if (!response.ok) {
      throw StateError('设备上报失败（HTTP ${response.statusCode}）');
    }
  }

  /// 图文识别：图片 → 文字（识别）→ 图文（文字 + 结果卡片）。
  ///
  /// 台账 U61/U62：计划书 P7 的「双输入」只写了语音与文字，
  /// 图片输入由人工指令追加，这里按同一套会话鉴权与错误语义实现。
  Future<VisionResult> recognizeImage({
    required List<int> bytes,
    required DateTime now,
    String? prompt,
    bool wantCard = true,
    String cardTitle = '图文识别结果',
  }) async {
    // 图文识别属成员能力（U64）：访客拿到 403 之前不该先传一张图上去
    final token = _requireMemberToken();
    await _session.touch(now);
    final response = await _transport.post(visionPath, token: token, body: {
      'image_base64': base64Encode(bytes),
      if (prompt != null && prompt.trim().isNotEmpty) 'prompt': prompt.trim(),
      'want_card': wantCard,
      'card_title': cardTitle,
    });
    if (response.statusCode == 401) {
      throw const SessionExpiredException();
    }
    if (!response.ok) {
      // 把服务端的原因原样带上来：它是「去配模型」还是「换张图」，完全不同
      throw VisionFailedException(
        (response.body['message'] as String?) ??
            '图文识别失败（HTTP ${response.statusCode}）',
        code: (response.body['code'] as String?) ?? 'unknown',
        statusCode: response.statusCode,
      );
    }
    return VisionResult.fromJson(response.body);
  }

  // ---------------- 语音链路（P7 / U63 / U65）----------------
  // 这几个接口**一律要求成员身份**：服务端按 U64 的能力表挡访客，
  // 端侧也先挡一次，免得白传一次音频再收 403。

  Future<Map<String, dynamic>> voiceAvailability() async {
    final response = await _transport.post(voiceAvailabilityPath,
        body: const {}, token: _requireMemberToken());
    return _guard(response, '语音组件自检');
  }

  /// 上传一段 16k 单声道 PCM16 WAV，拿识别文本。
  Future<AsrResult> transcribe(Uint8List wav) async {
    final response = await _transport.post(asrPath, token: _requireMemberToken(), body: {
      'audio_base64': base64Encode(wav),
    });
    return AsrResult.fromJson(_guard(response, '语音识别'));
  }

  /// 合成播报音频。端侧没有本地 TTS 模型时靠它兜底（U21）。
  Future<Uint8List> synthesize(String text, {int? speakerId, double? speed}) async {
    final response = await _transport.post(ttsPath, token: _requireMemberToken(), body: {
      'text': text,
      if (speakerId != null) 'speaker_id': speakerId,
      if (speed != null) 'speed': speed,
    });
    final body = _guard(response, '语音合成');
    final encoded = (body['audio_base64'] as String?) ?? '';
    if (encoded.isEmpty) throw const VoiceFailedException('服务端未返回音频');
    return base64Decode(encoded);
  }

  /// 登记声纹（U63）。服务端只留 embedding，不留音频。
  Future<VoiceprintResult> enrollVoiceprint(Uint8List wav) async {
    final response = await _transport.post(speakerEnrollPath,
        token: _requireMemberToken(), body: {'audio_base64': base64Encode(wav)});
    return VoiceprintResult.fromJson(_guard(response, '声纹登记'));
  }

  /// 校验声纹。**不匹配返回 403 + matched=false，不是异常**：
  /// 「不是本人」是正常结论，不是服务故障。
  Future<VoiceprintResult> verifyVoiceprint(Uint8List wav) async {
    final response = await _transport.post(speakerVerifyPath,
        token: _requireMemberToken(), body: {'audio_base64': base64Encode(wav)});
    if (response.statusCode == 403) {
      return VoiceprintResult.fromJson(response.body);
    }
    if (response.statusCode == 409) {
      return VoiceprintResult.fromJson(response.body);
    }
    return VoiceprintResult.fromJson(_guard(response, '声纹校验'));
  }

  /// 检索规划查询（U66）。用于调试与「为什么这么查」的可解释展示。
  Future<Map<String, dynamic>> retrievalPlan(String query) async {
    final response = await _transport.post(retrievalPlanPath,
        token: _token(), body: {'query': query});
    return _guard(response, '检索规划');
  }

  Map<String, dynamic> _guard(TransportResponse response, String what) {
    if (response.statusCode == 401) throw const SessionExpiredException();
    if (response.statusCode == 403) {
      final body = response.body;
      final code = (body['code'] as String?) ?? '';
      if (code == 'capability_required') {
        throw CapabilityRequiredException(
          (body['message'] as String?) ?? '$what：能力不足',
          capability: (body['capability'] as String?) ?? '',
          tier: (body['tier'] as String?) ?? '',
        );
      }
      throw StateError('$what 被拒绝（HTTP 403）');
    }
    if (!response.ok) {
      throw VoiceFailedException(
        (response.body['message'] as String?) ?? '$what 失败（HTTP ${response.statusCode}）',
        code: (response.body['code'] as String?) ?? 'unknown',
        statusCode: response.statusCode,
      );
    }
    return response.body;
  }

  /// 「登录后才有」的接口用的 token：**只认成员会话**，访客一律拒绝发请求。
  ///
  /// 抛 `NotSignedInException` 而不是 `CapabilityRequiredException`：
  /// 这一层的事实是「本地没有成员会话」，不是「服务端拒绝了能力」。
  /// 后者由服务端 403 转成（见 `_guard`），两者不能混为一谈 ——
  /// 混了之后就分不清「该去登录」与「登录了也没这个权限」。
  String _requireMemberToken([String capability = 'voice_input']) {
    final token = _session.bearerToken;
    if (token == null || token.isEmpty) {
      throw NotSignedInException('该功能需要登录后使用（$capability）');
    }
    return token;
  }

  /// 普通接口的 token：成员优先，其次访客。
  String _token() {
    final member = _session.bearerToken;
    if (member != null && member.isNotEmpty) return member;
    final guest = _guest;
    if (guest != null && guest.token.isNotEmpty) return guest.token;
    throw const NotSignedInException();
  }

}
