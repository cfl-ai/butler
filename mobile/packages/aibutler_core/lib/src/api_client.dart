/// P7 云端对接：请求带会话 Token；会话失效统一转成可识别的异常。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤5「对接云端流式接口…**请求带上会话 Token**；
///   **收到会话失效返回弹窗提示重新登录**」
/// - 三、P7 步骤7「上报携带会话身份」
/// - 端云分层：移动端**不做任何智能运算**，只发起请求并渲染结果
library;

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

class ApiClient {
  ApiClient({required TransportPort transport, required SessionManager session})
      : _transport = transport,
        _session = session;

  final TransportPort _transport;
  final SessionManager _session;

  static const String loginPath = '/api/session/login';
  static const String chatPath = '/api/chat/stream';
  static const String devicePath = '/api/device/report';

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
    await _session.adopt(credentials, now);
    return credentials;
  }

  /// 流式对话。返回逐片结果，供「逐字渲染 + 同步 TTS」消费。
  Stream<StreamChunk> chatStream({
    required String utterance,
    required DateTime now,
    bool manualInput = false,
  }) async* {
    final token = _requireToken();
    await _session.touch(now);
    final stream = _transport.stream(chatPath, body: {
      'utterance': utterance,
      'input_mode': manualInput ? 'text' : 'voice',
      'session_id': _session.current?.deviceId ?? '',
    }, token: token);

    await for (final raw in stream) {
      final status = (raw['status_code'] as num?)?.toInt();
      if (status == 401) {
        throw const SessionExpiredException();
      }
      yield StreamChunk.fromJson(raw);
    }
  }

  /// 上报设备信息。**未登录直接拒绝**，不发出任何请求。
  Future<void> reportDevice(DeviceReport report) async {
    final token = _requireToken();
    final response = await _transport.post(devicePath,
        body: report.toJson(), token: token);
    if (response.statusCode == 401) {
      throw const SessionExpiredException();
    }
    if (!response.ok) {
      throw StateError('设备上报失败（HTTP ${response.statusCode}）');
    }
  }

  String _requireToken() {
    final token = _session.bearerToken;
    if (token == null || token.isEmpty) {
      throw const NotSignedInException();
    }
    return token;
  }
}
