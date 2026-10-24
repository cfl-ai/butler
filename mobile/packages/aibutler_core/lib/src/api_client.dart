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
  // 账号与隐私（U67~U69）
  static const String registerPath = '/api/session/register';
  static const String forgotPath = '/api/session/forgot';
  static const String resetPath = '/api/session/reset';
  static const String accountMePath = '/api/accounts/me';
  static const String nicknamePath = '/api/accounts/nickname';
  static const String nicknameRandomPath = '/api/accounts/nickname/random';
  static const String nicknameSuggestPath = '/api/accounts/nickname/suggest';
  static const String passwordPath = '/api/accounts/password';
  // 模型（U70）—— 用 POST 别名，因为 TransportPort 只有 post/stream
  static const String modelsListPath = '/api/models/list';
  static const String modelsSelectPath = '/api/models/select';
  static const String modelsByokPath = '/api/models/byok';
  static const String modelsRemovePath = '/api/models/remove';
  // 定位 / 推荐 / 导航（U71）
  static const String recommendPath = '/api/location/recommend';
  static const String navigationPath = '/api/location/navigation';

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

  // ================= 账号 / 昵称 / 密码（U67~U69） ================= //

  /// 注册（手机号或邮箱）。成功后**服务端直接签发成员会话**。
  Future<AccountProfile> register({
    required String kind,
    required String identifier,
    required String password,
    required DateTime now,
    String deviceId = 'phone',
    String? nickname,
  }) async {
    final response = await _transport.post(registerPath, body: {
      'kind': kind,
      'identifier': identifier,
      'password': password,
      'device_id': deviceId,
      if (nickname != null) 'nickname': nickname,
    });
    if (response.statusCode == 409) {
      throw AccountException('该手机号/邮箱已注册，请直接登录或找回密码',
          code: 'identifier_taken');
    }
    final body = _guardAccount(response, '注册');
    await _adoptMemberSession(body, now: now, deviceId: deviceId);
    return AccountProfile.fromJson(
        (body['profile'] as Map?)?.cast<String, dynamic>() ?? const {});
  }

  /// 用手机号/邮箱登录。**带 `kind` 走账号体系**；不带则退回旧账号表。
  Future<AccountProfile> loginWithIdentifier({
    required String kind,
    required String identifier,
    required String password,
    required DateTime now,
    String deviceId = 'phone',
  }) async {
    final response = await _transport.post(loginPath, body: {
      'kind': kind,
      'identifier': identifier,
      'password': password,
      'device_id': deviceId,
    });
    if (response.statusCode == 401) {
      final body = response.body;
      final code = (body['code'] as String?) ?? 'invalid_credentials';
      throw AccountException(
          (body['message'] as String?) ?? '账号或密码不正确', code: code);
    }
    final body = _guardAccount(response, '登录');
    await _adoptMemberSession(body, now: now, deviceId: deviceId);
    return AccountProfile.fromJson(
        (body['profile'] as Map?)?.cast<String, dynamic>() ?? const {});
  }

  Future<void> _adoptMemberSession(Map<String, dynamic> body,
      {required DateTime now, required String deviceId}) async {
    final token = (body['token'] as String?) ?? '';
    if (token.isEmpty) throw StateError('登录成功但服务端未返回 token');
    final ttl = (body['expires_in_seconds'] as num?)?.toInt() ?? 0;
    final credentials = SessionCredentials(
      userId: (body['user_id'] as String?) ?? '',
      token: token,
      issuedAt: now,
      expiresAt: now.add(Duration(seconds: ttl)),
      idleTimeoutSeconds: (body['idle_timeout_seconds'] as num?)?.toInt() ?? 0,
      deviceId: deviceId,
    );
    // 登录成功后丢弃访客身份（U64）：两个身份并存会让「用哪个 token」变成猜的
    _guest = null;
    await _session.adopt(credentials, now);
  }

  /// 申请重置。返回值里的 `devToken` **只在开发通道**才有；
  /// 生产通道拿不到 —— 这条边界由服务端 `build_channel` 兜底。
  Future<({String message, String? devToken})> requestPasswordReset({
    required String kind,
    required String identifier,
  }) async {
    final response = await _transport.post(forgotPath,
        body: {'kind': kind, 'identifier': identifier});
    final body = _guardAccount(response, '找回密码');
    return (
      message: (body['message'] as String?) ?? '如果该账号存在，重置方式已发送',
      devToken: body['dev_token'] as String?,
    );
  }

  /// 用令牌设置新密码。成功后服务端会吊销该账号全部会话，端侧需重新登录。
  Future<int> resetPassword({
    required String token,
    required String newPassword,
  }) async {
    final response = await _transport.post(resetPath,
        body: {'token': token, 'new_password': newPassword});
    final body = _guardAccount(response, '重置密码');
    await _session.signOut();
    return (body['revoked_sessions'] as num?)?.toInt() ?? 0;
  }

  /// 当前账号资料（脱敏）。
  Future<AccountProfile> accountProfile() async {
    final response = await _transport.post(accountMePath,
        token: _requireMemberToken('profile_edit'), body: const {});
    return AccountProfile.fromJson(_guardAccount(response, '账号资料'));
  }

  Future<String> setNickname(String nickname) async {
    final response = await _transport.post(nicknamePath, body: {
      'nickname': nickname,
    }, token: _requireMemberToken('profile_edit'));
    final body = _guardAccount(response, '修改昵称');
    return (body['nickname'] as String?) ?? nickname;
  }

  /// 随机刷新昵称（服务端有频控，超了会抛 `too_many_requests`）。
  Future<String> refreshNickname() async {
    final response = await _transport.post(nicknameRandomPath,
        body: const {}, token: _requireMemberToken('profile_edit'));
    final body = _guardAccount(response, '刷新昵称');
    return (body['nickname'] as String?) ?? '';
  }

  /// 注册前取一个昵称建议。公开接口，无需登录。
  ///
  /// 走服务端而不是端侧自己拼：**词表与校验规则只能有一处**，
  /// 否则端上能填、提交报非法的割裂迟早出现。
  Future<String> suggestNickname() async {
    final response = await _transport.post(nicknameSuggestPath, body: const {});
    final body = _guardAccount(response, '昵称建议');
    return (body['nickname'] as String?) ?? '';
  }

  Future<int> changePassword({
    required String oldPassword,
    required String newPassword,
  }) async {
    final response = await _transport.post(passwordPath, body: {
      'old_password': oldPassword,
      'new_password': newPassword,
    }, token: _requireMemberToken('profile_edit'));
    final body = _guardAccount(response, '修改密码');
    await _session.signOut();
    return (body['revoked_sessions'] as num?)?.toInt() ?? 0;
  }

  // ================= 模型（U70） ================= //

  /// 可选模型 + 当前绑定 + 额度。返回原样 Map：字段多且会随裁定演进，
  /// 端侧按需读取，不为了"每个字段建一个类"而拖慢迭代。
  Future<Map<String, dynamic>> models() async {
    final response = await _transport.post(modelsListPath,
        body: const {}, token: _requireMemberToken('profile_edit'));
    return _guardAccount(response, '模型列表');
  }

  Future<Map<String, dynamic>> selectModel(String modelKey) async {
    final response = await _transport.post(modelsSelectPath,
        body: {'model_key': modelKey}, token: _requireMemberToken('profile_edit'));
    final body = _guardAccount(response, '选择模型');
    return (body['binding'] as Map?)?.cast<String, dynamic>() ?? const {};
  }

  /// 接入自带模型。**`apiKey` 只上行，不回传**：服务端只存密文、只回掩码。
  Future<Map<String, dynamic>> bindByokModel({
    required String baseUrl,
    required String model,
    required String apiKey,
    String displayName = '',
  }) async {
    final response = await _transport.post(modelsByokPath, body: {
      'base_url': baseUrl,
      'model': model,
      'api_key': apiKey,
      'display_name': displayName,
    }, token: _requireMemberToken('profile_edit'));
    final body = _guardAccount(response, '接入自带模型');
    return (body['binding'] as Map?)?.cast<String, dynamic>() ?? const {};
  }

  Future<bool> removeModelBinding() async {
    final response = await _transport.post(modelsRemovePath,
        body: const {}, token: _requireMemberToken('profile_edit'));
    final body = _guardAccount(response, '解除模型绑定');
    return body['removed'] == true;
  }

  // ================= 定位 / 推荐 / 导航（U71） ================= //

  /// 推荐。**交给服务端的坐标必须先粗化** —— 精确坐标不出端侧。
  ///
  /// 端侧这一层也粗化一次（调用方可能忘了），服务端再粗化一次：
  /// 两道防线不嫌多，因为这条数据的性质是"泄漏了收不回来"。
  Future<RecommendResult> recommend({
    double? latitude,
    double? longitude,
    List<String> prefs = const [],
    int? limit,
  }) async {
    final body = <String, dynamic>{
      'prefs': prefs,
      if (limit != null) 'limit': limit,
    };
    if (latitude != null && longitude != null) {
      body['lat'] = _coarsen(latitude);
      body['lon'] = _coarsen(longitude);
    }
    final response = await _transport.post(recommendPath,
        body: body, token: _requireMemberToken('location_share'));
    return RecommendResult.fromJson(_guardAccount(response, '推荐'));
  }

  /// 生成三大导航 App 的链接。起点**默认不传**，让各家 App 自己取定位。
  Future<List<NavLink>> navigationLinks({
    required double destLat,
    required double destLon,
    required String destName,
  }) async {
    final response = await _transport.post(navigationPath, body: {
      'dest_lat': destLat,
      'dest_lon': destLon,
      'dest_name': destName,
    }, token: _requireMemberToken('location_share'));
    final body = _guardAccount(response, '导航链接');
    final raw = (body['links'] as List?) ?? const [];
    return raw
        .whereType<Map>()
        .map((item) => NavLink.fromJson(item.cast<String, dynamic>()))
        .toList(growable: false);
  }

  /// 与 `geo.privacy_grid(decimals: 3)` 同口径：按位截断，不四舍五入。
  static double _coarsen(double value) {
    final scaled = value * 1000;
    return (scaled >= 0 ? scaled.floor() : scaled.ceil()) / 1000;
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

  /// 账号/模型/定位类接口的错误映射。
  ///
  /// 401 / 403 仍交给 `_guard`：**「会话失效」「能力不足」和「业务规则不满足」
  /// 是三件事**，混成一个异常会让界面把「密码太短」也弹成"请重新登录"。
  /// 其余非 2xx 统一抛 `AccountException` 并带上服务端 `code`：
  /// 界面据此给不同的下一步（去登录 / 去改密码 / 15 分钟后再试）。
  ///
  /// 之前这些接口直接复用 `_guard`，于是注册失败会抛
  /// `VoiceFailedException` —— 异常类型在说一件没发生的事。
  Map<String, dynamic> _guardAccount(TransportResponse response, String what) {
    final status = response.statusCode;
    if (response.ok || status == 401 || status == 403) {
      return _guard(response, what);
    }
    throw AccountException(
      (response.body['message'] as String?) ?? '$what失败（HTTP $status）',
      code: (response.body['code'] as String?) ?? 'unknown',
    );
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
