/// P7 设备上报：GPS 与设备状态定时上报。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤7「开发 GPS、设备状态定时上报；**上报携带会话身份**；
///   **未登录不上报用户私有设备信息**」
///
/// 注意最后一句是**硬约束**：未登录不是「上报个空值」，而是**根本不发请求**。
library;

import 'api_client.dart';
import 'models.dart';
import 'ports.dart';
import 'session.dart';

class DeviceReporter {
  DeviceReporter({
    required LocationPort location,
    required DeviceInfoPort deviceInfo,
    required ApiClient api,
    required SessionManager session,
    LoggerPort logger = const ConsoleLogger(),
  })  : _location = location,
        _deviceInfo = deviceInfo,
        _api = api,
        _session = session,
        _logger = logger;

  final LocationPort _location;
  final DeviceInfoPort _deviceInfo;
  final ApiClient _api;
  final SessionManager _session;
  final LoggerPort _logger;

  int _skippedBecauseSignedOut = 0;
  int _reported = 0;

  int get reportedCount => _reported;
  int get skippedCount => _skippedBecauseSignedOut;

  /// 单次上报。未登录直接跳过并计数，**不发起任何网络请求**。
  Future<bool> reportOnce({required DateTime now}) async {
    final userId = _session.userId;
    if (userId == null || userId.isEmpty) {
      _skippedBecauseSignedOut++;
      _logger.info('未登录，跳过设备上报（不上报用户私有设备信息）');
      return false;
    }

    final fix = await _location.current();
    final report = DeviceReport(
      userId: userId,
      sessionId: _session.current?.deviceId ?? '',
      reportedAt: now,
      latitude: fix?.latitude,
      longitude: fix?.longitude,
      batteryLevel: await _deviceInfo.batteryLevel(),
      networkType: await _deviceInfo.networkType(),
    );

    try {
      await _api.reportDevice(report);
      _reported++;
      await _session.touch(now);
      return true;
    } on SessionExpiredException {
      _logger.warn('设备上报时会话已失效，交由 UI 引导重登');
      return false;
    } on NotSignedInException {
      _skippedBecauseSignedOut++;
      return false;
    }
  }

  /// 定时上报。间隔由调用方给出（实现参数，计划书未给数值）。
  Stream<bool> periodic({required Duration interval, required DateTime Function() clock}) async* {
    while (true) {
      yield await reportOnce(now: clock());
      await Future<void>.delayed(interval);
    }
  }
}
