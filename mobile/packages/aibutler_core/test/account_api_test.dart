/// 端侧账号 / 模型 / 定位的契约测试（U67~U71）。
///
/// 重点不在"能调用"，而在几条**不能被客户端放松**的边界：
///
/// * 未登录时，资料/模型/定位一律不发请求（本地就抛 `NotSignedInException`）
/// * 注册成功会丢弃访客身份 —— 两个身份并存会让"用哪个 token"变成猜的
/// * 上报的坐标**先粗化再发送**，精确坐标不出端侧
/// * 登录失败/已注册的错误码能区分，UI 才能给出不同的下一步
library;

import 'package:aibutler_core/src/access_policy.dart';
import 'package:aibutler_core/src/api_client.dart';
import 'package:aibutler_core/src/models.dart';
import 'package:aibutler_core/src/ports.dart';
import 'package:aibutler_core/src/session.dart';
import 'package:test/test.dart';

import 'fakes.dart';

final DateTime kNow = DateTime(2026, 10, 24, 10);

ApiClient _client(FakeTransport transport,
    {bool member = true, bool guest = false}) {
  return _harness(transport, member: member, guest: guest).api;
}

/// 同时给出 client 与 session：有些断言要看"token 到底存没存"，
/// 而 `ApiClient` 故意不暴露 session（少一个能绕过去的入口）。
({ApiClient api, SessionManager session}) _harness(
  FakeTransport transport, {
  bool member = true,
  bool guest = false,
}) {
  final session = SessionManager(store: FakeSecureStore());
  if (member) {
    session.adopt(
        SessionCredentials(
          userId: 'user-A',
          token: 'tok-A',
          issuedAt: kNow,
          expiresAt: kNow.add(const Duration(hours: 12)),
          deviceId: 'phone',
        ),
        kNow);
  }
  final api = ApiClient(transport: transport, session: session);
  if (guest) {
    api.adoptGuest(GuestSession(
      userId: 'guest:abc',
      token: 'guest-tok',
      expiresAt: DateTime.now().add(const Duration(hours: 1)),
      textQueryBudget: 30,
    ));
  }
  return (api: api, session: session);
}

TransportResponse _json(int status, Map<String, dynamic> body) =>
    TransportResponse(statusCode: status, body: body);

void main() {
  group('注册 / 登录（U67）', () {
    test('注册成功会保存成员会话', () async {
      final transport = FakeTransport(responses: {
        ApiClient.registerPath: _json(201, {
          'user_id': 'u_1',
          'token': 'tok-new',
          'expires_in_seconds': 3600,
          'idle_timeout_seconds': 900,
          'profile': {
            'user_id': 'u_1',
            'ident_kind': 'phone',
            'ident_masked': '138****0007',
            'nickname': '松鼠',
          },
        }),
      });
      final h = _harness(transport, member: false);
      final api = h.api;

      final profile = await api.register(
        kind: 'phone',
        identifier: '13800000007',
        password: 'Demo-Passw0rd',
        now: kNow,
      );

      expect(profile.identMasked, '138****0007');
      expect(h.session.bearerToken, 'tok-new');
      final sent = transport.lastPost(ApiClient.registerPath);
      expect(sent.body['kind'], 'phone');
      // 明文密码只上行一次，不该被塞进别的字段
      expect(sent.body['password'], 'Demo-Passw0rd');
    });

    test('已注册的账号抛出可识别的错误码', () async {
      final transport = FakeTransport(responses: {
        ApiClient.registerPath: _json(409, {'code': 'identifier_taken'}),
      });
      final api = _client(transport, member: false);

      await expectLater(
        () => api.register(
          kind: 'phone',
          identifier: '13800000007',
          password: 'Demo-Passw0rd',
          now: kNow,
        ),
        throwsA(isA<AccountException>()
            .having((e) => e.identifierTaken, 'identifierTaken', true)),
      );
    });

    test('登录成功后丢弃访客身份', () async {
      final transport = FakeTransport(responses: {
        ApiClient.loginPath: _json(200, {
          'user_id': 'u_1',
          'token': 'tok-member',
          'expires_in_seconds': 3600,
          'profile': {'user_id': 'u_1', 'ident_kind': 'phone', 'ident_masked': '138****0007'},
        }),
      });
      final api = _client(transport, member: false, guest: true);
      expect(api.hasGuest, isTrue);

      await api.loginWithIdentifier(
        kind: 'phone',
        identifier: '13800000007',
        password: 'Demo-Passw0rd',
        now: kNow,
      );

      expect(api.hasGuest, isFalse, reason: '登录后仍留着访客身份');
      expect(api.hasMemberSession, isTrue);
    });

    test('密码错与账号锁定给出不同错误码', () async {
      for (final code in ['invalid_credentials', 'account_locked']) {
        final transport = FakeTransport(responses: {
          ApiClient.loginPath: _json(401, {'code': code, 'message': '…'}),
        });
        final api = _client(transport, member: false);
        await expectLater(
          () => api.loginWithIdentifier(
            kind: 'phone',
            identifier: '13800000007',
            password: 'x',
            now: kNow,
          ),
          throwsA(isA<AccountException>().having((e) => e.code, 'code', code)),
        );
      }
    });
  });

  group('找回 / 重置（U68）', () {
    test('找回返回开发令牌（仅开发通道）', () async {
      final transport = FakeTransport(responses: {
        ApiClient.forgotPath: _json(200, {
          'status': 'accepted',
          'message': '如果 138****0007 已注册，重置方式已发送',
          'dev_token': 'reset-tok',
        }),
      });
      final api = _client(transport, member: false);

      final result = await api.requestPasswordReset(
          kind: 'phone', identifier: '13800000007');

      expect(result.devToken, 'reset-tok');
      expect(result.message, contains('已注册'));
    });

    test('生产通道没有 dev_token 时也能正常处理', () async {
      final transport = FakeTransport(responses: {
        ApiClient.forgotPath: _json(200, {'status': 'accepted', 'message': '已发送'}),
      });
      final api = _client(transport, member: false);
      final result = await api.requestPasswordReset(
          kind: 'email', identifier: 'a@example.com');
      expect(result.devToken, isNull, reason: '生产通道不该有令牌回显');
    });

    test('重置成功后本地会话被清掉（服务端会吊销全部会话）', () async {
      final transport = FakeTransport(responses: {
        ApiClient.resetPath: _json(200, {'status': 'ok', 'revoked_sessions': 2}),
      });
      final h = _harness(transport);
      final api = h.api;

      final revoked = await api.resetPassword(
          token: 'reset-tok', newPassword: 'Brand-New-Passw0rd');

      expect(revoked, 2);
      expect(h.session.bearerToken, isNull, reason: '改密后旧 token 不能再留着');
    });
  });

  group('资料与昵称（U69）', () {
    test('未登录改昵称不发请求', () async {
      final transport = FakeTransport();
      final api = _client(transport, member: false);

      await expectLater(
        () => api.setNickname('老张'),
        throwsA(isA<NotSignedInException>()),
      );
      expect(transport.posts, isEmpty, reason: '本地就没登录，不该打服务端');
    });

    test('昵称可自定义也可随机刷新', () async {
      final transport = FakeTransport(responses: {
        ApiClient.nicknamePath: _json(200, {'nickname': '老张'}),
        ApiClient.nicknameRandomPath: _json(200, {'nickname': '早起的鲸鱼'}),
      });
      final api = _client(transport);

      expect(await api.setNickname('老张'), '老张');
      expect(await api.refreshNickname(), '早起的鲸鱼');
    });

    test('刷新太频繁的错误码能被识别', () async {
      final transport = FakeTransport(responses: {
        ApiClient.nicknameRandomPath: _json(400, {'code': 'too_many_requests'}),
      });
      final api = _client(transport);
      await expectLater(
        () => api.refreshNickname(),
        // 账号类接口抛的是 `AccountException`，不是语音链路的 `VoiceFailedException`
        throwsA(isA<AccountException>()
            .having((e) => e.code, 'code', 'too_many_requests')
            .having((e) => e.tooManyRequests, 'tooManyRequests', true)),
      );
    });
  });

  group('模型（U70）', () {
    test('未登录看不了模型列表', () async {
      final transport = FakeTransport();
      final api = _client(transport, member: false);
      await expectLater(() => api.models(), throwsA(isA<NotSignedInException>()));
      expect(transport.posts, isEmpty);
    });

    test('自带密钥只上行，客户端拿不到明文回显', () async {
      final transport = FakeTransport(responses: {
        ApiClient.modelsByokPath: _json(201, {
          'binding': {
            'model_key': 'byok-1',
            'tier': 'byok',
            'provider': 'openai-compatible',
            'model': 'my-model',
            'display_name': '我的模型',
            'base_url': 'https://api.example.com/v1',
            'api_key_hint': 'sk-****cdef',
          },
        }),
      });
      final api = _client(transport);

      final binding = await api.bindByokModel(
        baseUrl: 'https://api.example.com/v1',
        model: 'my-model',
        apiKey: 'sk-demo-0123456789abcdef',
      );

      expect(binding['api_key_hint'], 'sk-****cdef');
      expect(binding.containsKey('api_key'), isFalse);
      // 上行时确实带了 key（否则服务端没东西可存）
      expect(transport.lastPost(ApiClient.modelsByokPath).body['api_key'],
          'sk-demo-0123456789abcdef');
    });
  });

  group('定位 / 推荐 / 导航（U71）', () {
    test('上报坐标先粗化，精确坐标不出端侧', () async {
      final transport = FakeTransport(responses: {
        ApiClient.recommendPath: _json(200, {
          'located': true,
          'map_available': false,
          'map_note': '未配置地图服务 key',
          'data_source': 'seed-demo',
          'located_at_grid': [39.908, 116.397],
          'items': [
            {
              'place_id': 'seed-1',
              'name': '胡同里的独立书店',
              'reason': '喜欢安静；距你 120 米',
              'tags': ['书店', '安静'],
              'lat': 39.9142,
              'lon': 116.4040,
              'distance_m': 120,
            }
          ],
        }),
      });
      final api = _client(transport);

      final result = await api.recommend(
          latitude: 39.908722, longitude: 116.397499, prefs: ['安静']);

      final sent = transport.lastPost(ApiClient.recommendPath);
      expect(sent.body['lat'], 39.908, reason: '精确坐标被直接发出去了');
      expect(sent.body['lon'], 116.397);
      expect(result.mapAvailable, isFalse);
      expect(result.isDemoData, isTrue);
      expect(result.items.single.reason, isNotEmpty);
    });

    test('未登录不做定位推荐', () async {
      final transport = FakeTransport();
      final api = _client(transport, member: false);
      await expectLater(
        () => api.recommend(latitude: 39.9, longitude: 116.4),
        throwsA(isA<NotSignedInException>()),
      );
      expect(transport.posts, isEmpty);
    });

    test('导航链接四条齐全且不传起点', () async {
      final transport = FakeTransport(responses: {
        ApiClient.navigationPath: _json(200, {
          'origin_shared': false,
          'links': [
            {'app': 'amap', 'label': '高德地图', 'url': 'https://uri.amap.com/x',
             'coordinate_system': 'gcj02'},
            {'app': 'baidu', 'label': '百度地图', 'url': 'baidumap://x',
             'coordinate_system': 'bd09'},
            {'app': 'tencent', 'label': '腾讯地图', 'url': 'https://apis.map.qq.com/x',
             'coordinate_system': 'gcj02'},
            {'app': 'web', 'label': '网页查看位置', 'url': 'https://uri.amap.com/m',
             'coordinate_system': 'gcj02'},
          ],
        }),
      });
      final api = _client(transport);

      final links = await api.navigationLinks(
          destLat: 39.908722, destLon: 116.397499, destName: '故宫');

      expect(links.map((l) => l.app), ['amap', 'baidu', 'tencent', 'web']);
      expect(links[1].coordinateSystem, 'bd09');
      // 起点默认不传：我方坐标不交给第三方
      expect(transport.lastPost(ApiClient.navigationPath).body.containsKey('origin_lat'),
          isFalse);
    });
  });

  group('偏好与画像（不扣额度）', () {
    test('保存偏好后能拿到服务端归一化的标签与匹配到的模型', () async {
      final transport = FakeTransport(responses: {
        ApiClient.prefsPath: _json(200, {
          'prefs': ['vision', '历史'],
          'matched_model': 'local-vl',
        }),
      });
      final api = _client(transport);

      final result = await api.setPrefs(['vision', '历史', '不存在的标签']);

      expect(result.prefs, ['vision', '历史']);
      expect(result.matchedModel, 'local-vl');
      final body = transport.lastPost(ApiClient.prefsPath).body;
      expect(body['prefs'], ['vision', '历史', '不存在的标签']);
    });

    test('未登录时保存偏好不发请求', () async {
      final transport = FakeTransport();
      final api = _client(transport, member: false);
      await expectLater(
          () => api.setPrefs(['vision']), throwsA(isA<NotSignedInException>()));
      expect(transport.posts, isEmpty);
    });
  });

  group('能力矩阵新增项', () {
    test('访客没有资料编辑与定位能力', () {
      final profile = AccessProfile.guest();
      expect(profile.canEditProfile, isFalse);
      expect(profile.canLocate, isFalse);
      expect(profile.canVoice, isFalse);
      expect(profile.allows(Capability.textQuery), isTrue);
    });

    test('成员拥有资料编辑与定位能力', () {
      final profile = AccessProfile.member();
      expect(profile.canEditProfile, isTrue);
      expect(profile.canLocate, isTrue);
    });
  });
}
