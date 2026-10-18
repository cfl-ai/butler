import 'package:aibutler_core/src/session.dart';
import 'package:aibutler_core/src/wake_word.dart';
import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime(2026, 10, 16, 12);

void main() {
  group('P7 唤醒（裁定 U23 平台分档）', () {
    test('支持后台常驻的平台（Android）走 background', () async {
      final wake = FakeWakeWord(supportsBackgroundWake: true);
      final logger = SilentLogger();
      final service = WakeWordService(
          wakeWord: wake, session: SessionManager(store: FakeSecureStore()),
          logger: logger);

      final result = await service.start(now: kNow);
      expect(result.mode, WakeWordMode.background);
      expect(wake.startCalls, [true]);
    });

    test('不支持后台常驻的平台（iOS）降级为前台唤醒，并明确记录降级', () async {
      final wake = FakeWakeWord(supportsBackgroundWake: false);
      final logger = SilentLogger();
      final service = WakeWordService(
          wakeWord: wake, session: SessionManager(store: FakeSecureStore()),
          logger: logger);

      final result = await service.start(now: kNow);
      expect(result.mode, WakeWordMode.foreground);
      expect(wake.startCalls, [false], reason: '不得在不支持的平台上假装后台常驻');
      expect(logger.lines.any((l) => l.contains('U23')), isTrue,
          reason: '降级必须留下可追溯的记录');
    });

    test('start 同时校验本地会话状态（计划书触发时机）', () async {
      final store = FakeSecureStore();
      final session = SessionManager(store: store);
      final service = WakeWordService(
          wakeWord: FakeWakeWord(supportsBackgroundWake: true), session: session);

      final anonymous = await service.start(now: kNow);
      expect(anonymous.signedIn, isFalse, reason: '未登录应交由 UI 引导登录');
    });

    test('stop 后不再处于监听态', () async {
      final wake = FakeWakeWord(supportsBackgroundWake: true);
      final service = WakeWordService(
          wakeWord: wake, session: SessionManager(store: FakeSecureStore()));
      await service.start(now: kNow);
      await service.stop();
      expect(service.isListening, isFalse);
      expect(wake.stopCalls, 1);
    });
  });
}
