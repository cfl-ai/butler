import 'package:aibutler_core/src/local_cache.dart';
import 'package:test/test.dart';

import 'fakes.dart';

void main() {
  group('P7 本地缓存', () {
    test('未绑定账号时读写缓存直接拒绝（避免跨账号串数据）', () async {
      final cache = LocalCache(store: FakeCacheStore());
      expect(() => cache.put('k', 'v'), throwsStateError);
      expect(() => cache.get('k'), throwsStateError);
    });

    test('绑定后按账号存取', () async {
      final store = FakeCacheStore();
      final cache = LocalCache(store: store);
      await cache.bindAccount('user-A');
      await cache.put('history', 'hi');
      expect(await cache.get('history'), 'hi');
      expect(store.data.keys, ['user-A']);
    });

    test('切换账号自动清理旧账号缓存（计划书三、P7 步骤8）', () async {
      final store = FakeCacheStore();
      final cache = LocalCache(store: store, logger: SilentLogger());
      await cache.bindAccount('user-A');
      await cache.put('history', 'A 的对话');

      await cache.bindAccount('user-B');
      expect(store.cleared, ['user-A'], reason: '切号必须清掉旧账号缓存');
      expect(await store.get('user-A', 'history'), isNull);
      expect(cache.accountId, 'user-B');
    });

    test('重复绑定同一账号不触发清理', () async {
      final store = FakeCacheStore();
      final cache = LocalCache(store: store);
      await cache.bindAccount('user-A');
      await cache.bindAccount('user-A');
      expect(store.cleared, isEmpty);
    });

    test('clearCurrent 清空并解除绑定', () async {
      final store = FakeCacheStore();
      final cache = LocalCache(store: store);
      await cache.bindAccount('user-A');
      await cache.put('k', 'v');
      await cache.clearCurrent();
      expect(cache.accountId, isNull);
      expect(store.data, isEmpty);
    });

    test('空账号被拒绝', () async {
      final cache = LocalCache(store: FakeCacheStore());
      expect(() => cache.bindAccount(''), throwsArgumentError);
    });
  });
}
