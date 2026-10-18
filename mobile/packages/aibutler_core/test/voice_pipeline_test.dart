import 'package:aibutler_core/src/api_client.dart';
import 'package:aibutler_core/src/models.dart';
import 'package:aibutler_core/src/ports.dart';   // _ThrowingVad 要实现 VadPort
import 'package:aibutler_core/src/session.dart';
import 'package:aibutler_core/src/voice_pipeline.dart';
import 'dart:async';

import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime(2026, 10, 16, 12);

({VoicePipeline pipeline, FakeVad vad, FakeAsr asr, FakeTts tts, FakeTransport transport})
    _build({List<Map<String, dynamic>> chunks = const []}) {
  final session = SessionManager(store: FakeSecureStore());
  // 先写入会话：chatStream 未登录会直接抛 NotSignedInException
  session.adopt(
      SessionCredentials(
        userId: 'user-A',
        token: 'tok-123',
        issuedAt: kNow,
        expiresAt: kNow.add(const Duration(hours: 12)),
        deviceId: 'phone-1',
      ),
      kNow);
  final transport = FakeTransport(streamChunks: chunks);
  final api = ApiClient(transport: transport, session: session);
  final vad = FakeVad();
  final asr = FakeAsr();
  final tts = FakeTts();
  final mic = FakeMicrophone();
  final pipeline = VoicePipeline(
      microphone: mic, vad: vad, asr: asr, tts: tts, api: api, session: session,
      logger: SilentLogger(),
      // 用例里把收音超时压到很短：静音路径必须**快速返回**而不是挂住
      listenTimeout: const Duration(milliseconds: 80));
  return (pipeline: pipeline, vad: vad, asr: asr, tts: tts, transport: transport);
}

/// 释放时会抛错的 VAD：用来验证「单个端口失败不拖累其余端口释放」。
class _ThrowingVad implements VadPort {
  int stopCalls = 0;

  @override
  Stream<bool> get speechActivity => const Stream<bool>.empty();

  @override
  Future<void> start() async {}

  @override
  Future<void> stop() async {
    stopCalls++;
    throw StateError('没启动过就停 —— 释放路径不应因此中断');
  }
}

void main() {
  group('P7 语音管线', () {
    test('收音 → VAD → ASR 拿到识别文本', () async {
      final rig = _build();
      final future = rig.pipeline.captureUtterance();
      await Future<void>.delayed(Duration.zero);
      rig.asr.emit('帮我订酒店');
      rig.vad.emit(true);
      rig.vad.emit(false);
      expect(await future, '帮我订酒店');
    });

    test('流式分片被逐片渲染，并同步交给 TTS（计划书三、P7 步骤5）', () async {
      final rig = _build(chunks: [
        {'index': 0, 'text': '第一段', 'speech': '第一段', 'is_last': false},
        {'index': 1, 'text': '第二段', 'speech': '第二段', 'is_last': true},
      ]);
      // 直接驱动 respond，避免依赖 capture 的时序
      final events = await rig.pipeline
          .respond(utterance: '说点什么', now: kNow)
          .toList();
      final rendered = events
          .where((e) => e.kind == PipelineEventKind.chunk)
          .map((e) => e.text)
          .toList();
      expect(rendered, ['第一段', '第二段']);
      expect(rig.tts.spoken, ['第一段', '第二段'],
          reason: '逐字渲染与 TTS 必须同序同源才能同步播报');
    });

    test('播报中检测到人声 → 立即打断并停播（计划书三、P7 步骤4）', () async {
      final rig = _build(chunks: const []);
      rig.transport.manual = true;              // 由本用例控制分片时序

      final events = <PipelineEvent>[];
      final done = rig.pipeline
          .respond(utterance: 'x', now: kNow)
          .listen(events.add)
          .asFuture<void>();

      await Future<void>.delayed(const Duration(milliseconds: 20));
      rig.transport.push({'index': 0, 'text': '一', 'speech': '一'});
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(events.any((e) => e.kind == PipelineEventKind.chunk), isTrue);

      rig.vad.emit(true);                       // 用户插话
      await Future<void>.delayed(const Duration(milliseconds: 20));
      rig.transport.push({'index': 1, 'text': '二', 'speech': '二'});
      await Future<void>.delayed(const Duration(milliseconds: 20));
      await rig.transport.closeStream();
      await done;

      expect(events.last.kind, PipelineEventKind.interrupted);
      expect(rig.pipeline.interrupted, isTrue);
      expect(rig.tts.stopCalls, greaterThan(0), reason: '打断必须立即停播');
    });

    test('会话失效 → 停播并给出可弹窗的事件', () async {
      final rig = _build(chunks: [{'status_code': 401}]);
      final events = await rig.pipeline
          .respond(utterance: 'x', now: kNow)
          .toList();
      expect(events.single.kind, PipelineEventKind.sessionExpired);
      expect(rig.tts.stopCalls, greaterThan(0));
    });

    test('识别不到有效语音时不发起云端请求', () async {
      final rig = _build();
      final future = rig.pipeline.runOnce(now: kNow).toList();
      await Future<void>.delayed(Duration.zero);
      rig.vad.emit(false);                      // 全程无人声 → 由超时兜底返回
      final events = await future;
      expect(events.single.kind, PipelineEventKind.failed);
      expect(rig.transport.streams, isEmpty);
    });

    test('手动 interrupt 同样停播', () async {
      final rig = _build();
      await rig.pipeline.interrupt();
      expect(rig.pipeline.interrupted, isTrue);
      expect(rig.tts.stopCalls, 1);
    });

    test('dispose 逐个停掉各端口，且单个端口抛错不中断其余释放', () async {
      final session = SessionManager(store: FakeSecureStore());
      final vad = _ThrowingVad();
      final asr = FakeAsr();
      final tts = FakeTts();
      final mic = FakeMicrophone();
      final pipeline = VoicePipeline(
          microphone: mic,
          vad: vad,
          asr: asr,
          tts: tts,
          api: ApiClient(transport: FakeTransport(), session: session),
          session: session,
          logger: SilentLogger());

      await pipeline.dispose();

      expect(vad.stopCalls, 1);
      // VAD 的 stop 抛了错，后面三个仍必须被停 —— 否则就是
      // 「一个插件坏掉，其余原生资源全泄漏」。
      expect(asr.stopCalls, 1, reason: 'VAD 抛错不应中断后续端口释放');
      expect(tts.stopCalls, 1);
      expect(mic.stopCalls, 1);
    });
  });
}
