/// U64/U66 端侧规则：能力矩阵、访客凭据、语音门控。
///
/// 这些用例钉的是**行为边界**，不是实现细节：
/// 「未登录只能文字」这条规则，如果哪天被顺手放开（例如为了调试方便），
/// 必须有一个用例先红。
library;

import 'dart:typed_data';

import 'package:aibutler_core/src/access_policy.dart';
import 'package:aibutler_core/src/api_client.dart';
import 'package:aibutler_core/src/models.dart';
import 'package:aibutler_core/src/ports.dart';
import 'package:aibutler_core/src/session.dart';
import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime(2026, 10, 24, 10);

ApiClient _guestClient(FakeTransport transport) {
  final session = SessionManager(store: FakeSecureStore());
  final api = ApiClient(transport: transport, session: session);
  api.adoptGuest(GuestSession(
    userId: 'guest:abc',
    token: 'guest-tok',
    // hasGuest 按**真实墙钟**判过期（访客凭据过期就该重领），
    // 所以这里不能用固定的 kNow 造有效期。
    expiresAt: DateTime.now().add(const Duration(hours: 1)),
    textQueryBudget: 30,
  ));
  return api;
}

ApiClient _memberClient(FakeTransport transport) {
  final session = SessionManager(store: FakeSecureStore());
  session.adopt(
      SessionCredentials(
        userId: 'user-A',
        token: 'tok-A',
        issuedAt: kNow,
        expiresAt: kNow.add(const Duration(hours: 12)),
        deviceId: 'phone',
      ),
      kNow);
  return ApiClient(transport: transport, session: session);
}

void main() {
  group('能力矩阵（U64）', () {
    test('访客只有文字查询', () {
      expect(capabilitiesOf(AccessTier.guest), {Capability.textQuery});
      final profile = AccessProfile.guest();
      expect(profile.canVoice, isFalse);
      expect(profile.canWake, isFalse);
      expect(profile.canImage, isFalse);
      expect(profile.canSpeakerprint, isFalse);
      expect(profile.allows(Capability.textQuery), isTrue);
    });

    test('成员拥有全部能力（语音 / 声纹 / 图文）', () {
      expect(capabilitiesOf(AccessTier.member).length, Capability.values.length);
      final profile = AccessProfile.member();
      expect(profile.canVoice, isTrue);
      expect(profile.canWake, isTrue);
      expect(profile.canImage, isTrue);
      expect(profile.canSpeakerprint, isTrue);
    });

    test('缺能力时抛的异常必须标出「需要去登录」', () {
      final profile = AccessProfile.guest();
      expect(
        () => profile.require(Capability.voiceInput),
        throwsA(isA<AccessDeniedException>()
            .having((e) => e.capability, 'capability', Capability.voiceInput)
            .having((e) => e.needsLogin, 'needsLogin', isTrue)
            .having((e) => e.message, 'message', contains('登录'))),
      );
    });

    test('成员缺能力时不引导去登录（登录也解决不了）', () {
      const profile = AccessProfile(tier: AccessTier.member, capabilities: {Capability.textQuery});
      expect(
        () => profile.require(Capability.sensitiveAction),
        throwsA(isA<AccessDeniedException>()
            .having((e) => e.needsLogin, 'needsLogin', isFalse)),
      );
    });

    test('解析服务端档案：以服务端下发的能力为准', () {
      final profile = AccessProfile.fromJson({
        'tier': 'member',
        'capabilities': ['text_query', 'voice_input', 'speakerprint'],
      });
      expect(profile.tier, AccessTier.member);
      expect(profile.canVoice, isTrue);
      expect(profile.canImage, isFalse);   // 服务端没给，就是没有
    });

    test('服务端只给 tier 时退回本地矩阵', () {
      final profile = AccessProfile.fromJson({'tier': 'member'});
      expect(profile.capabilities, capabilitiesOf(AccessTier.member));
    });

    test('parse 失败退回访客而不是成员（宁严不宽）', () {
      expect(AccessProfile.fromJson(const {}).tier, AccessTier.guest);
      expect(AccessProfile.fromJson({'tier': '超级管理员'}).tier, AccessTier.guest);
      expect(AccessProfile.fallback().canVoice, isFalse);
    });

    test('访客额度为 0 时标记为「已用完」', () {
      expect(AccessProfile.guest(textQueryRemaining: 0).quotaExhausted, isTrue);
      expect(AccessProfile.guest(textQueryRemaining: 3).quotaExhausted, isFalse);
      expect(AccessProfile.member().quotaExhausted, isFalse);   // 成员不限额
    });
  });

  group('访客凭据（U64）', () {
    test('startGuest 落凭据，文本查询带访客 token', () async {
      final transport = FakeTransport(
        postResponse: TransportResponse(statusCode: 200, body: {
          'user_id': 'guest:xyz',
          'token': 'guest-token',
          'tier': 'guest',
          'expires_in_seconds': 3600,
          'text_query_budget': 30,
        }),
        streamChunks: [
          {'type': 'chunk', 'text': '你好'},
          {'type': 'done', 'data': {'tools': ['model.direct']}},
        ],
      );
      final api = ApiClient(
          transport: transport, session: SessionManager(store: FakeSecureStore()));

      final guest = await api.startGuest(now: kNow);
      expect(guest.token, 'guest-token');
      expect(api.hasMemberSession, isFalse);   // 有访客凭据 ≠ 已登录

      final events = await api
          .chatEvents(utterance: '年假几天', now: kNow)
          .toList();
      expect(events.map((e) => e.type),
          [ChatEventType.chunk, ChatEventType.done]);
      expect(transport.streams.single.token, 'guest-token');
      expect(events.last.tools, ['model.direct']);
    });

    test('访客拿不到语音接口：本地就拒，不白发一次请求', () async {
      final transport = FakeTransport();
      final api = _guestClient(transport);
      expect(
        () => api.transcribe(_fakeWav()),
        throwsA(isA<NotSignedInException>()),
      );
      expect(transport.posts, isEmpty);
    });

    test('完全没有凭据时文本查询也拒绝', () async {
      final api = ApiClient(
          transport: FakeTransport(), session: SessionManager(store: FakeSecureStore()));
      expect(
        () => api.chatEvents(utterance: 'hi', now: kNow).toList(),
        throwsA(isA<NotSignedInException>()),
      );
    });

    test('登录成功后丢弃访客身份（两个身份并存会让 token 选择变成猜的）', () async {
      final transport = FakeTransport(
        postResponse: TransportResponse(statusCode: 200, body: {
          'user_id': 'user-A',
          'token': 'member-token',
          'expires_in_seconds': 43200,
        }),
      );
      final api = _guestClient(transport);
      expect(api.hasGuest, isTrue);
      await api.login(account: 'user-A', password: 'p', deviceId: 'phone', now: kNow);
      expect(api.hasGuest, isFalse);
      expect(api.hasMemberSession, isTrue);
    });
  });

  group('语音接口（成员专属）', () {
    test('成员可以调用识别与合成', () async {
      final transport = FakeTransport(
        postResponse: TransportResponse(statusCode: 200, body: {
          'text': '帮我订明天的会议室',
          'audio_seconds': 2.5,
          'speech_seconds': 2.1,
          'elapsed_ms': 320,
          'model': 'sherpa-onnx/streaming-zipformer-zh-14M-int8',
        }),
      );
      final api = _memberClient(transport);
      final result = await api.transcribe(_fakeWav());
      expect(result.text, '帮我订明天的会议室');
      expect(result.isEmpty, isFalse);
      expect(transport.posts.single.path, '/api/voice/asr');
    });

    test('服务端 503 → 失败可分辨为「模型不可用」', () async {
      final transport = FakeTransport(
        postResponse: TransportResponse(statusCode: 503, body: {
          'code': 'speech_unavailable',
          'message': 'asr 不可用：缺少模型文件',
        }),
      );
      final api = _memberClient(transport);
      expect(
        () => api.transcribe(_fakeWav()),
        throwsA(isA<VoiceFailedException>()
            .having((e) => e.isUnavailable, 'isUnavailable', isTrue)),
      );
    });

    test('声纹不匹配是正常结论，不是异常', () async {
      final transport = FakeTransport(
        postResponse: TransportResponse(statusCode: 403, body: {
          'matched': false,
          'score': 0.11,
          'threshold': 0.35,
          'code': 'voiceprint_rejected',
        }),
      );
      final api = _memberClient(transport);
      final verdict = await api.verifyVoiceprint(_fakeWav());
      expect(verdict.matched, isFalse);
      expect(verdict.score, closeTo(0.11, 1e-9));
      expect(verdict.threshold, 0.35);
    });
  });
}

/// 一段假 WAV 字节：这些用例只验证「发给谁、带什么 token」，
/// 不做真实解码 —— 解码归服务端（`infra/speech.decode_wav`）负责。
Uint8List _fakeWav() => Uint8List(44);
