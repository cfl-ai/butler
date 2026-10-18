/// P7 会话管理：凭证本地加密存储 · 过期本地检测 · 登出清理。
///
/// 计划书依据
/// ----------
/// - 三、P7 步骤3「账号密码登录、**会话凭证本地加密存储**；**明文密码不在本地持久化**」
/// - 三、P7 步骤8「长时间静置**本地检测会话过期**提示重登」
/// - 三、P7 触发时机「唤醒后先校验本地登录会话状态，未登录引导登录」
library;

import 'dart:convert';

import 'models.dart';
import 'ports.dart';

/// 会话存储键。**只存凭证本身**——任何情况下都不写入密码。
const String kSessionStoreKey = 'aibutler.session.v1';

/// 最近活跃时间键（用于本地空闲超时判定）。
const String kLastActiveKey = 'aibutler.lastActive.v1';

class SessionManager {
  SessionManager({required SecureStorePort store, LoggerPort logger = const ConsoleLogger()})
      : _store = store,
        _logger = logger;

  final SecureStorePort _store;
  final LoggerPort _logger;

  SessionCredentials? _current;
  DateTime? _lastActiveAt;

  SessionCredentials? get current => _current;
  bool get isSignedIn => _current != null;
  String? get userId => _current?.userId;

  /// 从本地加密存储恢复会话（App 启动 / 唤醒后先走这一步）。
  ///
  /// 返回是否处于**可用的**登录态；过期或缺失一律视为未登录。
  Future<bool> restore(DateTime now) async {
    final raw = await _store.read(kSessionStoreKey);
    if (raw == null || raw.isEmpty) {
      _current = null;
      return false;
    }
    try {
      _current = SessionCredentials.fromJson(_decode(raw));
    } catch (error) {
      _logger.warn('会话凭证解析失败，按未登录处理');
      await signOut();
      return false;
    }
    final last = await _store.read(kLastActiveKey);
    _lastActiveAt = last == null ? null : DateTime.tryParse(last);

    if (_current!.isExpired(now, lastActiveAt: _lastActiveAt)) {
      _logger.info('本地检测到会话已过期，清除并要求重新登录');
      await signOut();
      return false;
    }
    return true;
  }

  /// 登录成功后**只持久化 token**，绝不落密码。
  Future<void> adopt(SessionCredentials credentials, DateTime now) async {
    _current = credentials;
    _lastActiveAt = now;
    await _store.write(kSessionStoreKey, _encode(credentials.toJson()));
    await _store.write(kLastActiveKey, now.toIso8601String());
  }

  /// 记录一次活跃，避免长时间静置被本地判过期。
  Future<void> touch(DateTime now) async {
    if (_current == null) return;
    _lastActiveAt = now;
    await _store.write(kLastActiveKey, now.toIso8601String());
  }

  /// 主动登出：清空凭证与活跃时间。
  Future<void> signOut() async {
    _current = null;
    _lastActiveAt = null;
    await _store.delete(kSessionStoreKey);
    await _store.delete(kLastActiveKey);
  }

  /// 请求携带的 Token（未登录返回 null，调用方据此拒绝发起私有请求）。
  String? get bearerToken => _current?.token;

  /// 用 JSON 而不是手写键值对：手写格式会把所有值都变成字符串，
  /// 取回时 \`idleTimeoutSeconds\` 这类数字字段就会类型不符
  /// （这个坑由 dart test 实际跑出来，见 P7 交付报告）。
  static String _encode(Map<String, dynamic> json) => jsonEncode(json);

  static Map<String, dynamic> _decode(String raw) =>
      jsonDecode(raw) as Map<String, dynamic>;
}
