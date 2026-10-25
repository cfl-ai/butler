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

/// 定位权限的当前状态（U107）。
///
/// 区分 `denied` 与 `deniedForever` 是这一版的重点：前者还能再弹一次系统
/// 授权框，后者（用户勾了「不再询问」）**再问也没用**，只能引导去系统
/// 设置页。不区分的话，用户点了「重新定位」毫无反应，看起来像 App 坏了。
enum LocationAccess {
  /// 已授权
  granted,

  /// 系统定位服务未开启
  serviceOff,

  /// 被拒绝但**仍可再申请**（下次会弹系统授权框）
  denied,

  /// 被永久拒绝（勾了「不再询问」）——只能引导去系统设置
  deniedForever,

  /// 取不到状态（真机以外的环境）
  unavailable,
}

/// 定位端口（三、P7 步骤7 GPS 上报）。
abstract class LocationPort {
  Future<({double latitude, double longitude})?> current();

  /// 只**查询**权限状态，不触发申请框。用于决定要不要显示「去设置」入口。
  Future<LocationAccess> probe() async => LocationAccess.unavailable;

  /// 打开系统设置页（deniedForever 时唯一的出路）。返回是否真的打开了。
  Future<bool> openSystemSettings() async => false;
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

  /// 喂入一段采样（16k 单声道 [-1,1]）。
  ///
  /// **必须由外部驱动**：VAD 自己不持有麦克风。之前没有这个方法，
  /// 于是管线启动了 VAD 却从不喂数据，`speechActivity` 永远是空流 ——
  /// 「随时打断」实际上从未生效（靠真机联调才发现，见 P7 缺陷记录）。
  void feed(List<double> samples);
}

/// 流式 ASR 端口（U20）。
abstract class AsrPort {
  Stream<String> get partialText;
  Future<void> start();
  Future<void> feed(List<double> samples);
  Future<void> stop();

  /// 收尾并返回**最终**识别文本。
  ///
  /// 与 `partialText` 的分工：后者是过程（可能不断被改写），
  /// 前者是终态。管线只信终态 —— 过程文本可能是「你好管家今天」而终态是
  /// 「你好管家，今天天气怎么样」。
  Future<String> finish();
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
