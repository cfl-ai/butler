import 'package:aibutler_core/src/models.dart';
import 'package:aibutler_core/src/session.dart';
import 'package:test/test.dart';

import 'fakes.dart';

SessionCredentials _creds({DateTime? issued, DateTime? expires, int idle = 0}) {
  final now = issued ?? DateTime(2026, 10, 16, 12);
  return SessionCredentials(
    userId: 'user-A',
    token: 'tok-abc',
    issuedAt: now,
    expiresAt: expires ?? now.add(const Duration(hours: 12)),
    idleTimeoutSeconds: idle,
    deviceId: 'phone-1',
  );
}

void main() {
  group('P7 会话管理', () {
    test('未登录时 restore 返回 false', () async {
      final manager = SessionManager(store: FakeSecureStore());
      expect(await manager.restore(DateTime(2026, 10, 16)), isFalse);
      expect(manager.isSignedIn, isFalse);
    });

    test('adopt 后 restore 可恢复，且凭证写入加密存储', () async {
      final store = FakeSecureStore();
      final manager = SessionManager(store: store);
      final now = DateTime(2026, 10, 16, 12);
      await manager.adopt(_creds(), now);

      final restored = SessionManager(store: store);
      expect(await restored.restore(now), isTrue);
      expect(restored.userId, 'user-A');
      expect(restored.bearerToken, 'tok-abc');
    });

    test('凭证过期后 restore 返回 false 并清除本地痕迹', () async {
      final store = FakeSecureStore();
      final manager = SessionManager(store: store);
      final now = DateTime(2026, 10, 16, 12);
      await manager.adopt(_creds(expires: now.add(const Duration(minutes: 1))), now);

      final later = now.add(const Duration(hours: 2));
      expect(await manager.restore(later), isFalse);
      expect(store.values, isEmpty, reason: '过期凭证必须被清掉');
    });

    test('长时间静置触发本地空闲过期（计划书三、P7 步骤8）', () async {
      final store = FakeSecureStore();
      final manager = SessionManager(store: store);
      final now = DateTime(2026, 10, 16, 12);
      await manager.adopt(_creds(idle: 600), now);   // 空闲 600 秒即过期

      expect(await manager.restore(now.add(const Duration(seconds: 300))), isTrue);
      expect(await manager.restore(now.add(const Duration(seconds: 900))), isFalse);
    });

    test('touch 会推迟空闲过期', () async {
      final store = FakeSecureStore();
      final manager = SessionManager(store: store);
      final now = DateTime(2026, 10, 16, 12);
      await manager.adopt(_creds(idle: 600), now);
      await manager.touch(now.add(const Duration(seconds: 500)));

      expect(await manager.restore(now.add(const Duration(seconds: 900))), isTrue);
    });

    test('signOut 清空凭证', () async {
      final store = FakeSecureStore();
      final manager = SessionManager(store: store);
      await manager.adopt(_creds(), DateTime(2026, 10, 16, 12));
      await manager.signOut();
      expect(manager.bearerToken, isNull);
      expect(store.values, isEmpty);
    });

    test('损坏的凭证按未登录处理，不抛异常', () async {
      final store = FakeSecureStore();
      await store.write(kSessionStoreKey, '这不是合法凭证');
      final manager = SessionManager(store: store, logger: SilentLogger());
      expect(await manager.restore(DateTime(2026, 10, 16)), isFalse);
    });
  });
}
