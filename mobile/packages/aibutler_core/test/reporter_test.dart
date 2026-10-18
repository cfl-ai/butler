import 'package:aibutler_core/src/api_client.dart';
import 'package:aibutler_core/src/models.dart';
import 'package:aibutler_core/src/session.dart';
import 'package:aibutler_core/src/reporter.dart';
import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime(2026, 10, 16, 12);

({DeviceReporter reporter, FakeTransport transport, SessionManager session, FakeLocation loc})
    _build() {
  final session = SessionManager(store: FakeSecureStore());
  final transport = FakeTransport();
  final api = ApiClient(transport: transport, session: session);
  final loc = FakeLocation();
  final reporter = DeviceReporter(
      location: loc,
      deviceInfo: FakeDeviceInfo(),
      api: api,
      session: session,
      logger: SilentLogger());
  return (reporter: reporter, transport: transport, session: session, loc: loc);
}

/// 直接写入会话凭证——本用例检的是**上报**行为，不必绕道登录接口。
Future<void> _signIn(SessionManager session) => session.adopt(
      SessionCredentials(
        userId: 'user-A',
        token: 'tok-123',
        issuedAt: kNow,
        expiresAt: kNow.add(const Duration(hours: 12)),
        idleTimeoutSeconds: 7200,
        deviceId: 'phone-1',
      ),
      kNow,
    );

void main() {
  group('P7 设备上报', () {
    test('未登录时**不发起任何请求**，也不读取位置（计划书三、P7 步骤7）', () async {
      final rig = _build();
      final sent = await rig.reporter.reportOnce(now: kNow);
      expect(sent, isFalse);
      expect(rig.transport.posts, isEmpty, reason: '未登录不得上报用户私有设备信息');
      expect(rig.loc.calls, 0, reason: '连位置都不该去取');
      expect(rig.reporter.skippedCount, 1);
    });

    test('已登录时上报并带上会话身份', () async {
      final rig = _build();
      await _signIn(rig.session);

      expect(await rig.reporter.reportOnce(now: kNow), isTrue);
      final body = rig.transport.posts.single.body;
      expect(body['user_id'], 'user-A');
      expect(body['session_id'], 'phone-1');
      expect(body['latitude'], 30.25);
      expect(body['battery_level'], 80);
      expect(body['network_type'], 'wifi');
      expect(rig.reporter.reportedCount, 1);
    });

    test('登出后立即恢复为「不上报」', () async {
      final rig = _build();
      await _signIn(rig.session);
      expect(await rig.reporter.reportOnce(now: kNow), isTrue);

      await rig.session.signOut();
      expect(await rig.reporter.reportOnce(now: kNow), isFalse);
      expect(rig.transport.posts.length, 1, reason: '登出后不得再发请求');
      expect(rig.reporter.skippedCount, 1);
    });

    test('没有定位权限时仍可上报其余设备信息', () async {
      final session = SessionManager(store: FakeSecureStore());
      final transport = FakeTransport();
      final api = ApiClient(transport: transport, session: session);
      await _signIn(session);
      final reporter = DeviceReporter(
          location: FakeLocation(fix: null),
          deviceInfo: FakeDeviceInfo(),
          api: api,
          session: session,
          logger: SilentLogger());

      expect(await reporter.reportOnce(now: kNow), isTrue);
      expect(transport.posts.single.body.containsKey('latitude'), isFalse);
      expect(transport.posts.single.body['battery_level'], 80);
    });
  });
}
