/// P7 本地缓存：严格绑定当前登录账号，切换账号自动清理。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤8「本地记录归档、隐私加密、离线缓存；**本地缓存严格绑定当前登录账号**；
///   **切换账号自动清理旧账号缓存**」
library;

import 'ports.dart';

class LocalCache {
  LocalCache({required CacheStorePort store, LoggerPort logger = const ConsoleLogger()})
      : _store = store,
        _logger = logger;

  final CacheStorePort _store;
  final LoggerPort _logger;

  String? _accountId;

  String? get accountId => _accountId;

  /// 绑定账号。**切换到不同账号时先清掉旧账号的缓存**（计划书明确要求）。
  Future<void> bindAccount(String accountId) async {
    if (accountId.isEmpty) {
      throw ArgumentError('缓存必须绑定非空账号');
    }
    final previous = _accountId;
    if (previous != null && previous != accountId) {
      _logger.info('切换账号：清理旧账号 $previous 的本地缓存');
      await _store.clearAccount(previous);
    }
    _accountId = accountId;
  }

  /// 登出时调用：清空当前账号缓存并解除绑定。
  Future<void> clearCurrent() async {
    final current = _accountId;
    if (current != null) {
      await _store.clearAccount(current);
    }
    _accountId = null;
  }

  Future<void> put(String key, String value) async {
    final account = _requireAccount();
    await _store.put(account, key, value);
  }

  Future<String?> get(String key) async {
    final account = _requireAccount();
    return _store.get(account, key);
  }

  Future<List<String>> cachedAccounts() => _store.accounts();

  String _requireAccount() {
    final account = _accountId;
    if (account == null) {
      throw StateError('未绑定账号时不得读写本地缓存（避免跨账号串数据）');
    }
    return account;
  }
}
