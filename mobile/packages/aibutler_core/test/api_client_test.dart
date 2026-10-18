import 'package:aibutler_core/src/api_client.dart';
import 'package:aibutler_core/src/models.dart';
import 'package:aibutler_core/src/session.dart';
import 'package:aibutler_core/src/ports.dart';
import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime(2026, 10, 16, 12);

({ApiClient api, FakeTransport transport, FakeSecureStore store, SessionManager session})
    _build({Map<String, dynamic>? loginBody, List<Map<String, dynamic>> chunks = const []}) {
  final store = FakeSecureStore();
  final session = SessionManager(store: store);
  // 默认给一个**带 token 的成功返回**：否则登录后 bearerToken 为空，
  // 所有后续调用都会以 NotSignedInException 失败，掩盖真正要测的东西。
  final transport = FakeTransport(
    streamChunks: chunks,
    postResponse: TransportResponse(
        statusCode: 200,
        body: loginBody ??
            const {
              'user_id': 'user-A',
              'token': 'tok-123',
              'expires_in_seconds': 43200,
              'idle_timeout_seconds': 7200,
            }),
  );
  return (api: ApiClient(transport: transport, session: session),
          transport: transport, store: store, session: session);
}

void main() {
  group('P7 云端对接', () {
    test('登录后只持久化 token，**明文密码绝不落盘**（计划书三、P7 步骤3）', () async {
      const password = 'SuperSecret#2026';
      final rig = _build(loginBody: {
        'user_id': 'user-A',
        'token': 'tok-123',
        'expires_in_seconds': 43200,
        'idle_timeout_seconds': 7200,
      });

      final creds = await rig.api.login(
          account: 'user-A', password: password, deviceId: 'phone-1', now: kNow);

      expect(creds.token, 'tok-123');
      expect(rig.store.dump().contains(password), isFalse, reason: '密码不得出现在本地存储里');
      expect(rig.store.dump().contains('password'), isFalse);
      expect(rig.store.dump().contains('tok-123'), isTrue);

      // 请求体里出现密码是正常的（一次性），但落盘的不行
      expect(rig.transport.posts.single.body['password'], password);
    });

    test('登录失败抛错且不写入任何凭证', () async {
      final store = FakeSecureStore();
      final session = SessionManager(store: store);
      final transport = FakeTransport(
          postResponse: const TransportResponse(statusCode: 401, body: {}));
      final api = ApiClient(transport: transport, session: session);
      await expectLater(
          api.login(account: 'a', password: 'p', deviceId: 'd', now: kNow),
          throwsA(isA<StateError>()));
      expect(store.values, isEmpty);
    });

    test('未登录发流式请求 → NotSignedInException，且不发出网络请求', () async {
      final rig = _build(chunks: []);
      await expectLater(rig.api.chatStream(utterance: '你好', now: kNow).toList(),
          throwsA(isA<NotSignedInException>()));
      expect(rig.transport.streams, isEmpty, reason: '未登录不得发起私有请求');
    });

    test('流式请求带上会话 Token', () async {
      final rig = _build(chunks: [{'index': 0, 'text': '你好', 'is_last': true}]);
      await rig.api.login(account: 'user-A', password: 'p', deviceId: 'd', now: kNow);
      final chunks = await rig.api
          .chatStream(utterance: '安排行程', now: kNow)
          .toList();
      expect(chunks.single.text, '你好');
      expect(rig.transport.streams.single.token, isNotNull);
      expect(rig.transport.streams.single.body['input_mode'], 'voice');
    });

    test('手输模式标记为 text（计划书三、P7 步骤6 双向交互）', () async {
      final rig = _build(chunks: const []);
      await rig.api.login(account: 'user-A', password: 'p', deviceId: 'd', now: kNow);
      await rig.api
          .chatStream(utterance: '打字', now: kNow, manualInput: true)
          .toList();
      expect(rig.transport.streams.single.body['input_mode'], 'text');
    });

    test('云端返回 401 → SessionExpiredException（UI 据此弹窗重登）', () async {
      final rig = _build(chunks: [{'status_code': 401}]);
      await rig.api.login(account: 'user-A', password: 'p', deviceId: 'd', now: kNow);
      await expectLater(rig.api.chatStream(utterance: 'x', now: kNow).toList(),
          throwsA(isA<SessionExpiredException>()));
    });

    test('设备上报未登录直接拒绝', () async {
      final rig = _build();
      final report = DeviceReport(userId: 'user-A', sessionId: 's', reportedAt: kNow);
      await expectLater(rig.api.reportDevice(report),
          throwsA(isA<NotSignedInException>()));
      expect(rig.transport.posts, isEmpty);
    });

    test('设备上报携带会话身份并可成功', () async {
      final rig = _build();
      await rig.api.login(account: 'user-A', password: 'p', deviceId: 'd', now: kNow);
      await rig.api.reportDevice(DeviceReport(
          userId: 'user-A', sessionId: 'phone-1', reportedAt: kNow, latitude: 30.2));
      final body = rig.transport.posts.last.body;
      expect(body['user_id'], 'user-A');
      expect(body['session_id'], 'phone-1');
    });
  });
}
