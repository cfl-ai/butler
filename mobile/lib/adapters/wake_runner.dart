/// 把「麦克风采样」接到「唤醒词引擎」——真机唤醒链路的最后一环。
///
/// 为什么需要它
/// ----------
/// `SherpaWakeWord.feed()` 只是一道入口：**没有东西持续喂音频，它就是死的**。
/// 端上唯一的音频来源是麦克风；而麦克风是独占资源 ——
/// 唤醒监听与语音管线不能同时持有同一个录音器，否则一方 stop 会把另一方也停掉。
/// 所以由本类统一管理「谁在拿麦克风」：
///
/// - `start()`  接上采样并开始喂引擎
/// - `pause()`  让出麦克风（交给语音管线用）
/// - `resume()` 管线结束后重新接回
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤2「集成后台常驻离线语音唤醒」
/// - 三、P7 触发时机「后台常驻监听，唤醒即启动全业务链路」
///
/// 这条链路的必要性不靠推理，靠实测：此前 App 只在登录后拉起前台服务，
/// **从未启动唤醒引擎、从未喂过一帧音频、也从未订阅检出事件**，
/// 于是「离线唤醒」在真机上等于没有。本类即为补上这段缺失的接线。
library;

import 'dart:async';
import 'dart:typed_data';

import 'package:aibutler_core/aibutler_core.dart';

import 'platform_adapters.dart';

/// 唤醒监听器：麦克风 → 唤醒词引擎。
class WakeRunner {
  WakeRunner({
    required MicrophonePort microphone,
    required SherpaWakeWord engine,
    LoggerPort logger = const ConsoleLogger(),
  })  : _mic = microphone,
        _engine = engine,
        _logger = logger;

  final MicrophonePort _mic;
  final SherpaWakeWord _engine;
  final LoggerPort _logger;

  StreamSubscription<List<double>>? _sub;
  int _frames = 0;

  bool get isRunning => _sub != null;

  /// 接上麦克风并开始喂引擎。
  ///
  /// 没有麦克风权限时 `MicrophonePort.start()` 会**抛错**（不静默装死），
  /// 由调用方决定怎么提示 —— 这里不吞异常。
  Future<void> start() async {
    if (_sub != null) return;
    await _mic.start();
    _sub = _mic.samples.listen(
      _onSamples,
      onError: (Object error) => _logger.error('唤醒采样中断', error),
    );
    _logger.info('唤醒监听已接入麦克风（16kHz 单声道），等待唤醒词');
  }

  void _onSamples(List<double> samples) {
    _frames++;
    // 只在第一帧记一次：证明「引擎真的在吃音频」，而不是状态位写着在跑
    if (_frames == 1) {
      _logger.info('唤醒引擎已收到首帧音频（${samples.length} 采样）');
    }
    _engine.feed(Float32List.fromList(samples), sampleRate: 16000);
  }

  /// 让出麦克风（语音管线要用）。引擎不销毁，只停止喂音频。
  Future<void> pause() async {
    final sub = _sub;
    _sub = null;
    await sub?.cancel();
    await _mic.stop();
    _logger.info('唤醒监听已暂停（让出麦克风）');
  }

  /// 管线结束后重新接回。麦克风可能已被管线停掉，所以这里重新 start。
  Future<void> resume() async {
    if (_sub != null) return;
    await start();
  }

  Future<void> dispose() async {
    final sub = _sub;
    _sub = null;
    await sub?.cancel();
    await _mic.stop();
  }
}
