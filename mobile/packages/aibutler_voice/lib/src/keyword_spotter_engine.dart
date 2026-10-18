/// 唤醒词引擎（sherpa-onnx）。
///
/// 裁定依据
/// --------
/// - **U06**：离线唤醒用 sherpa-onnx KWS + wenetspeech ZipFormer（int8 约 4.9MB）
/// - **U22**：VAD 用 silero-vad（sherpa-onnx 内置）
///
/// Windows 上必须先预加载 onnxruntime.dll（**这一条是崩溃的根因**）
/// ------------------------------------------------------------------
/// Windows 解析 DLL 依赖按「exe 目录 → **System32** → PATH」的顺序找，
/// 而 System32 里躺着系统自带的 **onnxruntime.dll 1.10**（Windows ML 的）。
/// 它抢先生效后，sherpa-onnx 拿到 ORT API 10 而库需要 28，于是打印
/// ~{The given version [28] is not supported, only version 1 to 10 is supported in this build.~}~
/// 紧接着**访问冲突崩溃**。
///
/// 解法：**先按绝对路径把正确的那份 load 进进程**，后续依赖解析会复用已加载模块。
/// 见 ~{preloadNativeLibraries~}~。这一坑排查见 MEMORY E-13。
///
/// 关键词 token 规则（另一个坑）
/// ---------------------------
/// 每个音节要拆成「**声母 + 韵母**」两个 token：
/// 介绍 = j + iè + sh + ào；写成整音节 shì 会报 Cannot find ID for token。
library;

import 'dart:ffi';
import 'dart:io';
import 'dart:typed_data';

import 'package:sherpa_onnx/sherpa_onnx.dart' as sherpa;

bool _nativeReady = false;

/// 预加载原生库并初始化绑定。**必须在创建任何引擎之前调用一次。**
///
/// [dllDir] 为 null 时按系统搜索路径找（真机上由打包好的原生库解决，无需指定）。
void preloadNativeLibraries({String? dllDir}) {
  if (_nativeReady) {
    return;
  }
  if (Platform.isWindows && dllDir != null) {
    final separator = Platform.pathSeparator;
    // 顺序不能反：先依赖（onnxruntime），再被依赖者（sherpa-onnx-c-api）
    for (final name in const ['onnxruntime.dll', 'sherpa-onnx-c-api.dll']) {
      DynamicLibrary.open(dllDir + separator + name);
    }
  }
  sherpa.initBindings();
  _nativeReady = true;
}

/// 引擎配置：模型文件路径与阈值。
class WakeWordEngineConfig {
  const WakeWordEngineConfig({
    required this.encoder,
    required this.decoder,
    required this.joiner,
    required this.tokens,
    required this.keywordsFile,
    this.numThreads = 1,
    this.keywordsScore = 1.0,
    this.keywordsThreshold = 0.25,
  });

  final String encoder;
  final String decoder;
  final String joiner;
  final String tokens;
  final String keywordsFile;
  final int numThreads;
  final double keywordsScore;
  final double keywordsThreshold;

  sherpa.KeywordSpotterConfig toSherpa() => sherpa.KeywordSpotterConfig(
        model: sherpa.OnlineModelConfig(
          transducer: sherpa.OnlineTransducerModelConfig(
              encoder: encoder, decoder: decoder, joiner: joiner),
          tokens: tokens,
          numThreads: numThreads,
        ),
        keywordsFile: keywordsFile,
        keywordsScore: keywordsScore,
        keywordsThreshold: keywordsThreshold,
      );
}

/// 一次检出结果。
class WakeWordHit {
  const WakeWordHit({required this.keyword, this.startTime = 0.0});

  /// 检出的关键词文本（如「你好管家」）。
  final String keyword;

  /// 相对音频起点的秒数；引擎未给出时为 0。
  final double startTime;

  @override
  String toString() =>
      'WakeWordHit(' + keyword + ' @' + startTime.toStringAsFixed(2) + 's)';
}

/// 唤醒词引擎。**不持有麦克风** —— 采样从外面喂进来。
class WakeWordEngine {
  WakeWordEngine(this.config, {String? dllDir}) {
    preloadNativeLibraries(dllDir: dllDir);
    _spotter = sherpa.KeywordSpotter(config.toSherpa());
  }

  final WakeWordEngineConfig config;
  late final sherpa.KeywordSpotter _spotter;
  bool _freed = false;

  /// 对一段完整音频做检出。
  ///
  /// 结果**必须在解码循环内逐块读取**：KWS 的结果逐块产生，
  /// 循环结束后再读只会拿到空结果（Python 侧踩过同一个坑）。
  List<WakeWordHit> detect(Float32List samples, {int sampleRate = 16000}) {
    if (_freed) {
      throw StateError('引擎已释放');
    }
    final stream = _spotter.createStream();
    final hits = <WakeWordHit>[];
    try {
      stream.acceptWaveform(samples: samples, sampleRate: sampleRate);
      // 尾部补静音：模型需要一点尾音才会对最后一个词下判定
      stream.acceptWaveform(
          samples: Float32List(sampleRate ~/ 2), sampleRate: sampleRate);
      stream.inputFinished();

      while (_spotter.isReady(stream)) {
        _spotter.decode(stream);
        final result = _spotter.getResult(stream);
        if (result.keyword.isNotEmpty) {
          hits.add(WakeWordHit(keyword: result.keyword));
          _spotter.reset(stream);
        }
      }
    } finally {
      stream.free();
    }
    return hits;
  }

  /// 读 wav 文件（16bit PCM）。走 sherpa-onnx 自带的读函数。
  ({Float32List samples, int sampleRate}) readWave(String path) {
    final wave = sherpa.readWave(path);
    return (samples: wave.samples, sampleRate: wave.sampleRate);
  }

  void free() {
    if (!_freed) {
      _spotter.free();
      _freed = true;
    }
  }
}
