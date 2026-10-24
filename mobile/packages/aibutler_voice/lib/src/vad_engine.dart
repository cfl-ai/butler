/// 端侧 VAD 人声检测（sherpa-onnx silero-vad）—— 裁定 U22。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤4「开发 **VAD 人声检测、实时收音、随时打断** 机制」
///
/// 为什么 VAD 必须在端上
/// --------------------
/// 「随时打断」的响应时间按毫秒算：用户一开口就要停播。走云端判断意味着
/// 一次网络往返（几十毫秒到几秒），播报会继续说完 —— 那就不是打断，是排队。
/// 所以 VAD 推理留在端侧，且模型只有 1.7MB，随包成本可接受。
///
/// 与 KWS 引擎同一条坑
/// -------------------
/// Windows 桌面验证必须先 `preloadNativeLibraries()`（见 keyword_spotter_engine.dart
/// 的长注释与 MEMORY E-13）；真机由打包好的原生库解决。
library;

import 'dart:typed_data';

import 'package:sherpa_onnx/sherpa_onnx.dart' as sherpa;

import 'keyword_spotter_engine.dart' show preloadNativeLibraries;

/// VAD 配置。默认值与 silero-vad 的训练口径一致，**不要随手改 windowSize**：
/// 它必须是 512（32ms @16k），改了模型会直接拒绝。
class VadEngineConfig {
  const VadEngineConfig({
    required this.modelPath,
    this.threshold = 0.5,
    this.minSilenceDuration = 0.35,
    this.minSpeechDuration = 0.15,
    this.maxSpeechDuration = 20.0,
    this.windowSize = 512,
    this.sampleRate = 16000,
    this.numThreads = 1,
  });

  final String modelPath;
  final double threshold;

  /// 判定「说完了」的静音时长。取 0.35s：短于 0.25s 会把句子中间的换气当成结束，
  /// 长于 0.5s 会让用户说完之后还要干等。
  final double minSilenceDuration;

  /// 判定「开始说了」的最短人声。太短会把键盘声/咳嗽当成一句话。
  final double minSpeechDuration;

  final double maxSpeechDuration;
  final int windowSize;
  final int sampleRate;
  final int numThreads;

  sherpa.VadModelConfig toSherpa() => sherpa.VadModelConfig(
        sileroVad: sherpa.SileroVadModelConfig(
          model: modelPath,
          threshold: threshold,
          minSilenceDuration: minSilenceDuration,
          minSpeechDuration: minSpeechDuration,
          maxSpeechDuration: maxSpeechDuration,
          windowSize: windowSize,
        ),
        sampleRate: sampleRate,
        numThreads: numThreads,
        provider: 'cpu',
        debug: false,
      );
}

/// 一段被 VAD 切出来的人声。
class VadSegment {
  const VadSegment({required this.samples, required this.start});

  final Float32List samples;

  /// 相对本次流开始的**采样点**偏移（不是秒）。
  /// 存采样点而不是秒：采样率随配置变化，先换算成秒会在改动采样率时算错。
  final int start;

  double durationSeconds(int sampleRate) => samples.length / sampleRate;
}

/// VAD 引擎。**不持有麦克风** —— 采样从外面喂进来（与 KWS 引擎同一设计）。
class VadEngine {
  VadEngine(this.config, {String? dllDir}) {
    preloadNativeLibraries(dllDir: dllDir);
    _vad = sherpa.VoiceActivityDetector(
      config: config.toSherpa(),
      bufferSizeInSeconds: 30,
    );
  }

  final VadEngineConfig config;
  late final sherpa.VoiceActivityDetector _vad;
  bool _freed = false;

  /// 累计喂进来的采样数。排障用：能直接看出「引擎到底有没有收到音频」，
  /// 而不是对着一个空片段列表猜是模型问题还是麦克风问题。
  int _consumed = 0;
  int get consumedSamples => _consumed;

  /// 当前是否正在说话（用于「播报中检测到人声就打断」）。
  bool get isSpeechDetected {
    if (_freed) return false;
    return _vad.isDetected();
  }

  /// 喂一段采样，返回**本次新切出**的完整人声片段。
  ///
  /// 逐窗口喂（windowSize 个采样一次）：一次喂一大块会丢掉内部的静音边界，
  /// 切出来的片段就变成「整段录音」，VAD 等于没做。
  List<VadSegment> feed(Float32List samples) {
    if (_freed) {
      throw StateError('VAD 引擎已释放');
    }
    final out = <VadSegment>[];
    final window = config.windowSize;
    var cursor = 0;
    while (cursor + window <= samples.length) {
      _vad.acceptWaveform(samples.sublist(cursor, cursor + window));
      cursor += window;
      _consumed += window;
      while (!_vad.isEmpty()) {
        final segment = _vad.front();
        out.add(VadSegment(samples: segment.samples, start: segment.start));
        _vad.pop();
      }
    }
    return out;
  }

  /// 收尾：把尾巴上最后半句冲出来。
  ///
  /// **不 flush 就永远丢最后一句** —— KWS 上踩过同一个坑（见 MEMORY E-13），
  /// 这里显式提供并让上层在收音结束时调用。
  List<VadSegment> flush() {
    if (_freed) return const [];
    // Dart 绑定没有 flush()：喂一段静音让模型自行收尾，效果等价且不依赖内部 API。
    final silence = Float32List(config.sampleRate ~/ 2);   // 0.5s
    final out = feed(silence);
    return out;
  }

  /// 重置状态（换一轮对话前调用，避免上一轮的残留段混进本轮）。
  void reset() {
    if (_freed) return;
    _vad.clear();
    _consumed = 0;
  }

  void free() {
    if (!_freed) {
      _vad.free();
      _freed = true;
    }
  }
}
