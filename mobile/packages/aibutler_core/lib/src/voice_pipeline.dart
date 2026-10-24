/// P7 语音管线：收音 → VAD → ASR → 云端流式 → 逐字渲染 + 同步 TTS。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤4「开发 **VAD 人声检测、实时收音、随时打断**机制」
/// - 三、P7 步骤5「对接云端流式接口：**逐字文字渲染 + 同步 TTS 播报**」
/// - 三、P7 步骤2「集成后台常驻离线语音唤醒」
///
/// 编排原则
/// --------
/// 管线只做**编排**：谁先谁后、何时打断、何时停播、采样喂给谁。
/// 识别与合成在云端（端云分层），端上只跑唤醒词与 VAD 的推理。
///
/// 本轮修掉的两个真实缺陷（P7 验收时暴露）
/// ------------------------------------
/// 1. **采样从未被喂给 VAD/ASR**：`captureUtterance()` 启动了麦克风与 VAD，
///    却没有把 `mic.samples` 转发下去。结果 `vad.speechActivity` 永远是空流，
///    只能靠 8 秒超时退出 —— 「检测到人声即打断」成了死代码。
///    现在由管线统一订阅 `mic.samples`，同时喂 VAD 与 ASR（**同一条流，不复制录音**）。
/// 2. **TTS 失败会把整轮对话判成失败**：没有本地 TTS 模型时 `speak()` 抛错，
///    文本已经渲染出来却被标成「交互失败」。播报失败只该影响**声音**，
///    不该影响**屏幕上已经正确的文字**，所以这里降级为记一条警告。
library;

import 'dart:async';

import 'api_client.dart';
import 'models.dart';
import 'ports.dart';
import 'session.dart';

enum PipelineEventKind {
  listening,
  partial,
  chunk,
  speech,
  interrupted,
  sessionExpired,
  failed,
  /// 收音结束但一句人声都没听到 —— **与「识别失败」不是一回事**，
  /// 提示语也不同（「请再说一次」vs「识别失败：模型不可用」）。
  voiceNoSpeech,
}

class PipelineEvent {
  const PipelineEvent(this.kind, {this.text = '', this.chunk, this.error});

  final PipelineEventKind kind;
  final String text;
  final StreamChunk? chunk;
  final Object? error;

  @override
  String toString() => 'PipelineEvent(${kind.name}, $text)';
}

class VoicePipeline {
  VoicePipeline({
    required MicrophonePort microphone,
    required VadPort vad,
    required AsrPort asr,
    required TtsPort tts,
    required ApiClient api,
    required SessionManager session,
    LoggerPort logger = const ConsoleLogger(),
    Duration listenTimeout = const Duration(seconds: 8),
  })  : _listenTimeout = listenTimeout,
        _mic = microphone,
        _vad = vad,
        _asr = asr,
        _tts = tts,
        _api = api,
        _session = session,
        _logger = logger;

  final MicrophonePort _mic;
  final VadPort _vad;
  final AsrPort _asr;
  final TtsPort _tts;
  final ApiClient _api;
  final SessionManager _session;
  final LoggerPort _logger;

  /// 收音超时。**没有它，全程静音会把界面永久挂住**——
  /// 这是本阶段的真实缺陷，由 dart test 跑出来（用例超时才暴露）。
  final Duration _listenTimeout;

  bool _interrupted = false;
  bool get interrupted => _interrupted;

  /// 释放：停掉各端口持有的资源。
  ///
  /// **不调用就是泄漏** —— 麦克风、VAD、ASR、TTS 各自持有原生资源
  /// （sherpa-onnx 的会话、录音句柄等），Flutter 侧 State.dispose 必须转达下来。
  /// 每个端口单独 try/catch：某个没启动过不该拖累其余端口释放。
  Future<void> dispose() async {
    for (final stop in [_mic.stop, _vad.stop, _asr.stop, _tts.stop]) {
      try {
        await stop();
      } catch (_) {
        // 没启动过的端口 stop() 可能抛错 —— 释放路径上不应因此中断
      }
    }
  }

  /// 一次完整交互：先说（收音识别），再说（渲染播报）。
  Stream<PipelineEvent> runOnce({required DateTime now}) async* {
    _interrupted = false;
    yield const PipelineEvent(PipelineEventKind.listening, text: '正在听…');
    var lastPartial = '';
    final partialSub = _asr.partialText.listen((text) => lastPartial = text);

    String utterance;
    try {
      utterance = await captureUtterance();
    } finally {
      await partialSub.cancel();
    }
    if (utterance.trim().isEmpty) {
      // 到这一步才判「没听到」；走到这里说明收音流程本身是通的
      yield PipelineEvent(PipelineEventKind.voiceNoSpeech,
          text: lastPartial.isEmpty ? '没有听到人声' : lastPartial);
      return;
    }
    yield PipelineEvent(PipelineEventKind.partial, text: utterance);
    yield* respond(utterance: utterance, now: now);
  }

  /// 采集一轮语音并返回识别文本。
  ///
  /// **采样只采集一次，同时喂给 VAD 与 ASR**：再开一路录音既浪费，
  /// 安卓上也拿不到第二路（同一时刻只有一个 AudioRecord 能拿到数据）。
  Future<String> captureUtterance() async {
    await _mic.start();
    await _vad.start();
    await _asr.start();

    final pump = _mic.samples.listen((samples) {
      try {
        _vad.feed(samples);
      } catch (error) {
        _logger.warn('VAD 喂采样失败：$error');
      }
      // ASR 的 feed 是异步的，这里不 await：它是「尽力接收」，
      // 真正的终态以 finish() 为准（见 AsrPort 的契约）
      unawaited(_asr.feed(samples).catchError((Object error) {
        _logger.warn('ASR 喂采样失败：$error');
      }));
    }, onError: (Object error) => _logger.warn('麦克风流出错：$error'));

    try {
      var heardSpeech = false;
      try {
        await for (final active in _vad.speechActivity.timeout(_listenTimeout)) {
          if (active) {
            heardSpeech = true;
            continue;
          }
          if (heardSpeech) break;      // 人声结束
        }
      } on TimeoutException {
        _logger.info('收音超时（未检测到有效语音），返回空文本而不是一直等下去');
      }
      // 终态由 ASR 给：过程文本可能还在被改写
      final text = await _asr.finish();
      return text.trim().isEmpty ? '' : text.trim();
    } finally {
      await pump.cancel();
      await _vad.stop();
      await _asr.stop();
      await _mic.stop();
    }
  }

  /// 把一句话送上云，边收边渲染 + 播报；期间检测到人声即**随时打断**。
  ///
  /// 打断由 **VAD 驱动**（三、P7 步骤4「随时打断」）：播报过程中只要
  /// `vad.speechActivity` 报出人声，就立刻停 TTS 并作废本轮，不需要用户按按钮。
  Stream<PipelineEvent> respond({required String utterance, required DateTime now}) async* {
    // 发送前先看本地登录态：未登录不必等网络往返才知道，也避免白发一次请求
    if (!_session.isSignedIn) {
      yield const PipelineEvent(PipelineEventKind.sessionExpired, text: '未登录，请先登录');
      return;
    }
    final vadSub = _vad.speechActivity.listen((active) {
      if (active) {
        _interrupted = true;
        // 不 await：这里在流的回调里，停播是「尽力立刻」的动作
        _tts.stop();
      }
    });
    try {
      await for (final chunk in _api.chatStream(utterance: utterance, now: now)) {
        if (_interrupted) {
          yield const PipelineEvent(PipelineEventKind.interrupted, text: '已被用户打断');
          return;
        }
        yield PipelineEvent(PipelineEventKind.chunk, text: chunk.text, chunk: chunk);
        if (chunk.speech.isNotEmpty) {
          yield PipelineEvent(PipelineEventKind.speech, text: chunk.speech);
          // 播报失败只影响声音，不影响已经正确的文字（见文件头缺陷 2）
          try {
            await _tts.speak(chunk.speech);
          } catch (error) {
            _logger.warn('播报失败（文字仍保留）：$error');
          }
          if (_interrupted) {
            yield const PipelineEvent(PipelineEventKind.interrupted, text: '已被用户打断');
            return;
          }
        }
      }
    } on SessionExpiredException catch (error) {
      _logger.warn('会话失效，停止播报并交由 UI 弹窗重登');
      await _tts.stop();
      yield PipelineEvent(PipelineEventKind.sessionExpired, text: error.message,
          error: error);
    } on NotSignedInException catch (error) {
      yield PipelineEvent(PipelineEventKind.sessionExpired, text: error.message,
          error: error);
    } catch (error) {
      yield PipelineEvent(PipelineEventKind.failed, text: '交互失败', error: error);
    } finally {
      await vadSub.cancel();
    }
  }

  /// 随时打断：立即停播，并标记本轮作废（三、P7 步骤4）。
  Future<void> interrupt() async {
    _interrupted = true;
    await _tts.stop();
    _logger.info('检测到人声，已打断播报');
  }
}
