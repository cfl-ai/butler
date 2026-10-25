/// U73：会话标识的端侧契约。
///
/// 这一层只守两句话：
/// * 标识**只由服务端签发**，端侧只负责保存与回传，绝不自己造；
/// * 服务端说「本次不建会话」时，端侧必须把旧标识**清掉**，
///   而不是继续拿着上一段对话的把手。
library;

import 'package:aibutler_core/aibutler_core.dart';
import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime.utc(2026, 10, 25, 12);

/// 服务端签发的形态：32 位小写十六进制。
const String kConversation = '0123456789abcdef0123456789abcdef';

Future<ApiClient> _member(FakeTransport transport) async {
  transport.responses = {
    ApiClient.loginPath: const TransportResponse(statusCode: 200, body: {
      'token': 'member-token',
      'user_id': 'u_1',
      'expires_in_seconds': 3600,
      'idle_timeout_seconds': 1800,
    }),
  };
  final api = ApiClient(
      transport: transport, session: SessionManager(store: FakeSecureStore()));
  await api.login(
      account: '13800000101', password: 'Demo-Passw0rd',
      deviceId: 'phone', now: kNow);
  return api;
}

void main() {
  test('首轮不带会话标识，由服务端签发', () async {
    final transport = FakeTransport(streamChunks: [
      {'type': 'accepted', 'data': {'conversation_id': kConversation, 'memory': true}},
      {'type': 'chunk', 'text': '好'},
      {'type': 'done', 'data': {}},
    ]);
    final api = await _member(transport);
    expect(api.conversationId, isNull);

    await api.chatEvents(utterance: '年假几天', now: kNow).toList();

    expect(transport.streams.single.body.containsKey('conversation_id'), isFalse,
        reason: '端侧不该在服务端签发之前就带上一个标识');
    expect(api.conversationId, kConversation);
  });

  test('第二轮带上服务端签发的标识', () async {
    final transport = FakeTransport(streamChunks: [
      {'type': 'accepted', 'data': {'conversation_id': kConversation}},
      {'type': 'done', 'data': {}},
    ]);
    final api = await _member(transport);

    await api.chatEvents(utterance: '公司年假有几天', now: kNow).toList();
    await api.chatEvents(utterance: '那第二个呢', now: kNow).toList();

    expect(transport.streams, hasLength(2));
    expect(transport.streams[0].body.containsKey('conversation_id'), isFalse);
    expect(transport.streams[1].body['conversation_id'], kConversation);
  });

  test('服务端给空值时清空旧标识，下一次不再携带', () async {
    final transport = FakeTransport(streamChunks: [
      {'type': 'accepted', 'data': {'conversation_id': kConversation}},
      {'type': 'done', 'data': {}},
    ]);
    final api = await _member(transport);
    await api.chatEvents(utterance: '第一句', now: kNow).toList();
    expect(api.conversationId, kConversation);

    // 服务端改口：本次不建会话（访客 / 功能关闭 / 归属不符被重签失败）
    transport.streamChunks = [
      {'type': 'accepted', 'data': {'conversation_id': '', 'note': '访客不建立会话'}},
      {'type': 'done', 'data': {}},
    ];
    await api.chatEvents(utterance: '第二句', now: kNow).toList();
    expect(api.conversationId, isNull);

    // 第三次彻底不带 —— 这才是「清空」与「记着旧值」的分界
    await api.chatEvents(utterance: '第三句', now: kNow).toList();
    expect(transport.streams[2].body.containsKey('conversation_id'), isFalse);
  });

  test('startNewConversation 之后重新由服务端签发', () async {
    final transport = FakeTransport(streamChunks: [
      {'type': 'accepted', 'data': {'conversation_id': kConversation}},
      {'type': 'done', 'data': {}},
    ]);
    final api = await _member(transport);
    await api.chatEvents(utterance: '第一段对话', now: kNow).toList();

    api.startNewConversation();
    expect(api.conversationId, isNull);

    await api.chatEvents(utterance: '新的话题', now: kNow).toList();
    expect(transport.streams[1].body.containsKey('conversation_id'), isFalse);
  });

  test('设备号与会话标识是两个字段，不再互相冒充', () async {
    final transport = FakeTransport(streamChunks: [
      {'type': 'accepted', 'data': {'conversation_id': kConversation}},
      {'type': 'done', 'data': {}},
    ]);
    final api = await _member(transport);
    await api.chatEvents(utterance: '年假几天', now: kNow).toList();
    await api.chatEvents(utterance: '那第二个呢', now: kNow).toList();

    final body = transport.streams[1].body;
    // 设备维度上报仍然在（服务端要用），但会话维度由 conversation_id 承载
    expect(body['session_id'], 'phone');
    expect(body['conversation_id'], kConversation);
    expect(body['session_id'], isNot(equals(body['conversation_id'])),
        reason: '把设备号当会话标识，等于一台设备上所有对话共用一个分片');
  });
}
