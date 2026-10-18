/// P7 离线语音唤醒：**按平台分档**（裁定 U23）。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤2「集成后台常驻离线语音唤醒」
/// - 三、P7 触发时机「后台常驻监听，唤醒即启动全业务链路；唤醒后先校验本地登录会话状态」
/// - **裁定 U23（人工授权）**：「计划书**未按平台分档**」——
///   仅 Android 承载后台唤醒，**iOS 降级为前台唤醒**。
///
/// 这条分档是对计划书原文的**范围补充**，由人工裁定，不是 AI 自行决定；
/// 因此代码里把它显式建模成两种模式，而不是含糊地「尽力而为」。
library;

import 'ports.dart';
import 'session.dart';

enum WakeWordMode {
  /// 后台常驻监听（仅 Android）
  background,

  /// 前台唤醒（iOS；以及 Android 用户主动关闭后台权限时的降级）
  foreground,
}

class WakeWordService {
  WakeWordService({
    required WakeWordPort wakeWord,
    required SessionManager session,
    LoggerPort logger = const ConsoleLogger(),
  })  : _wake = wakeWord,
        _session = session,
        _logger = logger;

  final WakeWordPort _wake;
  final SessionManager _session;
  final LoggerPort _logger;

  WakeWordMode? _mode;

  WakeWordMode? get mode => _mode;
  bool get isListening => _mode != null;

  /// 依据平台能力决定模式。**绝不在不支持的平台上假装后台常驻。**
  WakeWordMode resolveMode() =>
      _wake.supportsBackgroundWake ? WakeWordMode.background : WakeWordMode.foreground;

  /// 启动监听。唤醒后先校验本地会话状态（计划书触发时机），未登录交给 UI 引导登录。
  Future<({WakeWordMode mode, bool signedIn})> start({required DateTime now}) async {
    final mode = resolveMode();
    await _wake.start(background: mode == WakeWordMode.background);
    _mode = mode;
    final downgradeNote = mode == WakeWordMode.foreground
        ? '（该平台不支持后台常驻，已按 U23 降级）'
        : '';
    _logger.info('唤醒监听已启动，模式=${mode.name}$downgradeNote');
    final signedIn = await _session.restore(now);
    return (mode: mode, signedIn: signedIn);
  }

  Future<void> stop() async {
    await _wake.stop();
    _mode = null;
  }

  /// 唤醒事件流。**未登录时不触发业务链路**——交由 UI 引导登录。
  Stream<String> get detections => _wake.detections;
}
