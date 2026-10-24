/// 图文识别的移动端对接测试（台账 U61/U62）。
///
/// 关注点不是「有没有发请求」，而是三条容易出错、后果严重的边界：
/// 1. **识别结果里的图片必须真能解出字节**——base64 解不开要抛错，
///    不能悄悄返回空图让界面显示成「识别成功」。
/// 2. **错误必须可分辨**：503/vision_unavailable（去配模型）与
///    400/image_rejected（换张图）给出的用户指引完全不同。
/// 3. **未登录不发请求**：图片是隐私数据，不能因为漏判把图发出去。
library;

import 'dart:convert';

import 'package:aibutler_core/src/api_client.dart';
import 'package:aibutler_core/src/models.dart';
import 'package:aibutler_core/src/ports.dart';
import 'package:aibutler_core/src/session.dart';
import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime(2026, 10, 24, 12);

/// 1x1 的 PNG，够用来当「合法图片字节」。
final List<int> kTinyPng = base64Decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF/'
    'oQAAAABJRU5ErkJggg==');

({ApiClient api, FakeTransport transport, SessionManager session}) _build({
  TransportResponse? postResponse,
}) {
  final store = FakeSecureStore();
  final session = SessionManager(store: store);
  final transport = FakeTransport(
    postResponse: postResponse ??
        TransportResponse(statusCode: 200, body: const {
          'user_id': 'user-A',
          'token': 'tok-123',
          'expires_in_seconds': 43200,
          'idle_timeout_seconds': 7200,
        }),
  );
  return (api: ApiClient(transport: transport, session: session),
      transport: transport, session: session);
}

Future<void> _signIn(ApiClient api) async {
  await api.login(
      account: 'user-A', password: 'pw', deviceId: 'dev-1', now: kNow);
}

Map<String, dynamic> _successBody() => {
      'user_id': 'user-A',
      'text': '订单编号 AB-2026-1024\n合计金额：￥1280.00',
      'provider': 'ollama',
      'model': 'qwen2.5vl:3b',
      'elapsed_ms': 38913,
      'facts': {'width': 720, 'height': 260, 'brightness': 244},
      'blocks': [
        {'type': 'text', 'text': '订单编号 AB-2026-1024'},
        {
          'type': 'image',
          'mime': 'image/png',
          'purpose': 'card',
          'data_base64': base64Encode(kTinyPng),
        },
      ],
      'notes': <String>[],
    };

void main() {
  group('图文识别（台账 U61/U62）', () {
    test('上传图片后返回文字与结果图，且图片字节真能解出', () async {
      final rig = _build();
      await _signIn(rig.api);
      rig.transport.postResponse =
          TransportResponse(statusCode: 200, body: _successBody());

      final result = await rig.api.recognizeImage(bytes: kTinyPng, now: kNow);

      expect(result.text, contains('AB-2026-1024'));
      expect(result.provider, 'ollama');
      expect(result.model, 'qwen2.5vl:3b');
      expect(result.elapsedMs, 38913);
      expect(result.blocks.map((b) => b.type).toList(), ['text', 'image'],
          reason: '顺序有意义：正文在前、图在后');
      expect(result.card, isNotNull, reason: '必须能定位到结果卡片');
      expect(result.card!.bytes, kTinyPng, reason: '解出的字节要与服务端一致');
      expect(result.card!.mime, 'image/png');
    });

    test('请求体带 image_base64 与会话 token，且没有明文多余字段', () async {
      final rig = _build();
      await _signIn(rig.api);
      rig.transport.posts.clear();
      rig.transport.postResponse =
          TransportResponse(statusCode: 200, body: _successBody());

      await rig.api.recognizeImage(bytes: kTinyPng, now: kNow, wantCard: false);

      expect(rig.transport.posts, hasLength(1));
      final sent = rig.transport.posts.single;
      expect(sent.path, ApiClient.visionPath);
      expect(sent.token, 'tok-123', reason: '必须带会话身份');
      expect(sent.body['image_base64'], base64Encode(kTinyPng));
      expect(sent.body['want_card'], false);
    });

    test('服务端说没有可用视觉模型 → 可识别为「去配模型」而不是图片问题', () async {
      final rig = _build();
      await _signIn(rig.api);
      rig.transport.postResponse = TransportResponse(statusCode: 503, body: const {
        'code': 'vision_unavailable',
        'message': '没有可用的视觉供应商：ollama: 未拉取 qwen2.5vl:3b',
      });

      await expectLater(
        () => rig.api.recognizeImage(bytes: kTinyPng, now: kNow),
        throwsA(isA<VisionFailedException>()
            .having((e) => e.isUnavailable, 'isUnavailable', isTrue)
            .having((e) => e.statusCode, 'statusCode', 503)
            .having((e) => e.message, 'message', contains('qwen2.5vl'))),
      );
    });

    test('图片被拒绝 → 不是配置问题，指引不同', () async {
      final rig = _build();
      await _signIn(rig.api);
      rig.transport.postResponse = TransportResponse(statusCode: 400, body: const {
        'code': 'image_rejected',
        'message': '无法解析图片：cannot identify image file',
      });

      await expectLater(
        () => rig.api.recognizeImage(bytes: const <int>[1, 2, 3], now: kNow),
        throwsA(isA<VisionFailedException>()
            .having((e) => e.isUnavailable, 'isUnavailable', isFalse)
            .having((e) => e.code, 'code', 'image_rejected')),
      );
    });

    test('会话失效 → 抛 SessionExpired（由界面弹重登）', () async {
      final rig = _build();
      await _signIn(rig.api);
      rig.transport.postResponse =
          TransportResponse(statusCode: 401, body: const {'code': 'unauthorized'});

      await expectLater(
        () => rig.api.recognizeImage(bytes: kTinyPng, now: kNow),
        throwsA(isA<SessionExpiredException>()),
      );
    });

    test('未登录时**不发请求**（图片是隐私，不能漏判发出去）', () async {
      final rig = _build();
      rig.transport.posts.clear();

      await expectLater(
        () => rig.api.recognizeImage(bytes: kTinyPng, now: kNow),
        throwsA(isA<NotSignedInException>()),
      );
      expect(rig.transport.posts, isEmpty);
    });

    test('服务端漏给 blocks 时用 text 兜底，不返回空内容', () async {
      final rig = _build();
      await _signIn(rig.api);
      rig.transport.postResponse = TransportResponse(statusCode: 200, body: const {
        'text': '只有文字没有块',
        'provider': 'p',
        'model': 'm',
        'elapsed_ms': 1,
      });

      final result = await rig.api.recognizeImage(bytes: kTinyPng, now: kNow);
      expect(result.blocks, hasLength(1));
      expect(result.blocks.single.isText, isTrue);
      expect(result.text, '只有文字没有块');
      expect(result.card, isNull);
    });

    test('图片块 base64 损坏时抛错，而不是给出一张空图', () {
      expect(
        () => VisionBlock.fromJson(const {
          'type': 'image',
          'purpose': 'card',
          'data_base64': '!!!not-base64!!!',
        }),
        throwsA(isA<FormatException>()),
      );
    });
  });
}
