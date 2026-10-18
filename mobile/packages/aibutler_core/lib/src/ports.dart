/// P7 移动端平台端口（Ports & Adapters）。
///
/// 为什么这样分层
/// --------------
/// sherpa_onnx / flutter_secure_storage / geolocator 都走**平台通道**，
/// 在 flutter test（Dart VM，无平台）里不可用。若业务逻辑直接调用它们，
/// 整条链路就只能靠真机验证，回归极慢。
///
/// 因此把平台能力抽象成端口，业务逻辑（会话、缓存、上报、语音管线、唤醒策略）
/// 全部只依赖端口；真机实现放 adapters，测试注入假实现。
/// 这样「明文密码不入盘」「切换账号清缓存」「未登录不上报」这类**规则**
/// 才能在单元测试里被钉死。
library;

/// 安全存储端口（真机实现走 flutter_secure_storage，U05）。
abstract class SecureStorePort {
  Future<void> write(String key, String value);
  Future<String?> read(String key);
  Future<void> delete(String key);
  Future<void> deleteAll();
}

/// 本地文件缓存端口（按账号目录隔离）。
abstract class CacheStorePort {
  Future<void> put(String accountId, String key, String value);
  Future<String?> get(String accountId, String key);
  Future<void> clearAccount(String accountId);
  Future<List<String>> accounts();
}

/// 定位端口（三、P7 步骤7 GPS 上报）。
abstract class LocationPort {
  Future<({double latitude, double longitude})?> current();
}

/// 设备信息端口（电量、网络类型等）。
abstract class DeviceInfoPort {
  Future<int?> batteryLevel();
  Future<String?> networkType();
}

/// 云端传输端口（真机实现走 HTTP/SSE）。
abstract class TransportPort {
  Future<TransportResponse> post(
    String path, {
    required Map<String, dynamic> body,
    String? token,
  });

  /// 流式接口：按片返回（三、P7 步骤5）。
  Stream<Map<String, dynamic>> stream(
    String path, {
    required Map<String, dynamic> body,
    String? token,
  });
}

class TransportResponse {
  const TransportResponse({required this.statusCode, required this.body});

  final int statusCode;
  final Map<String, dynamic> body;

  bool get ok => statusCode >= 200 && statusCode < 300;
}

/// 唤醒能力端口（U06 / U23）。
abstract class WakeWordPort {
  /// 该平台是否支持后台常驻唤醒。**U23：仅 Android 支持；iOS 只做前台唤醒。**
  bool get supportsBackgroundWake;

  Future<void> start({required bool background});
  Future<void> stop();
  Stream<String> get detections;
}

/// VAD 人声检测端口（U22）。
abstract class VadPort {
  Stream<bool> get speechActivity;
  Future<void> start();
  Future<void> stop();
}

/// 流式 ASR 端口（U20）。
abstract class AsrPort {
  Stream<String> get partialText;
  Future<void> start();
  Future<void> feed(List<double> samples);
  Future<void> stop();
}

/// TTS 播报端口（U21）。
abstract class TtsPort {
  Future<void> speak(String text);
  Future<void> stop();
}

/// 麦克风收音端口。
abstract class MicrophonePort {
  Stream<List<double>> get samples;
  Future<void> start();
  Future<void> stop();
}

/// 日志端口。
abstract class LoggerPort {
  void info(String message);
  void warn(String message);
  void error(String message, [Object? error]);
}

class ConsoleLogger implements LoggerPort {
  const ConsoleLogger();

  @override
  void info(String message) => print('[INFO] $message');
  @override
  void warn(String message) => print('[WARN] $message');
  @override
  void error(String message, [Object? error]) =>
      print('[ERROR] $message ${error?.toString() ?? ''}');
}
