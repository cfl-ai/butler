/// 登录入口：登录 / 注册 / 忘记密码（三、P7 步骤3；U67 / U68 / U69）。
///
/// 三条设计上的硬约束：
///
/// 1. **明文密码绝不落本地**：只在本页内存里活到提交那一刻，成功后清空；
///    落盘的只有 token（见 core/session.dart）。
/// 2. **验证码/重置令牌不做本地假成功**：拿不到就是拿不到，界面如实说
///    「开发环境会在下方显示令牌」，而不是画一个「已发送」骗用户。
/// 3. **手机号与邮箱等价**：切换只改提交的 `kind`，不改校验口径 ——
///    校验真值在服务端，端侧只做「非空 + 明显格式错」的即时提示。
library;

import 'package:flutter/material.dart';

/// 账号标识类型。与服务端 `accounts.PHONE / EMAIL` 对齐。
enum IdentKind {
  phone('phone', '手机号', '请输入 11 位手机号'),
  email('email', '邮箱', '请输入邮箱地址');

  const IdentKind(this.wire, this.label, this.hint);

  final String wire;
  final String label;
  final String hint;
}

/// 表单模式。
enum AuthMode { login, register, forgot, reset }

/// 登录页对外回调。**全部由 App 层注入**：页面自己不碰网络，便于测试。
class AuthCallbacks {
  const AuthCallbacks({
    required this.onLogin,
    required this.onRegister,
    required this.onForgot,
    required this.onReset,
    required this.onSuggestNickname,
  });

  final Future<void> Function(IdentKind kind, String identifier, String password)
      onLogin;
  final Future<void> Function(
      IdentKind kind, String identifier, String password, String? nickname)
      onRegister;

  /// 返回开发通道回显的重置令牌（生产通道为 null）。
  final Future<String?> Function(IdentKind kind, String identifier) onForgot;
  final Future<void> Function(String token, String newPassword) onReset;
  final Future<String> Function() onSuggestNickname;
}

class LoginPage extends StatefulWidget {
  const LoginPage({
    super.key,
    required this.callbacks,
    this.errorText,
    this.initialMode = AuthMode.login,
    this.onSkip,
  });

  final AuthCallbacks callbacks;
  final String? errorText;
  final AuthMode initialMode;

  /// 从访客态点进来的登录页必须留一条退路。
  ///
  /// 没有它，「先试试」就变成单向门 —— 用户点了登录就只能注册，
  /// 而他要的本来只是「不打字直接看看」。为 null 时不显示退路
  /// （例如启动时就因服务端关闭访客模式而停在登录页，退回去也没地方去）。
  final VoidCallback? onSkip;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  late AuthMode _mode = widget.initialMode;
  IdentKind _kind = IdentKind.phone;

  final _identifier = TextEditingController();
  final _password = TextEditingController();
  final _nickname = TextEditingController();
  final _resetToken = TextEditingController();
  final _newPassword = TextEditingController();

  bool _busy = false;
  String? _hint;
  String? _error;

  @override
  void dispose() {
    // 页面销毁时把明文口令清掉，别让它留在 controller 里等 GC
    _password.clear();
    _newPassword.clear();
    _identifier.dispose();
    _password.dispose();
    _nickname.dispose();
    _resetToken.dispose();
    _newPassword.dispose();
    super.dispose();
  }

  bool _looksValid(String value) {
    if (value.trim().isEmpty) return false;
    if (_kind == IdentKind.phone) {
      final digits = value.replaceAll(RegExp(r'[^0-9]'), '');
      return digits.length == 11 || (digits.length == 13 && digits.startsWith('86'));
    }
    return value.contains('@') && value.contains('.');
  }

  Future<void> _run(Future<void> Function() action) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await action();
    } catch (error) {
      if (mounted) setState(() => _error = _humanize(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// 把错误码翻成人话。**不同 code 给不同下一步**，而不是统一「操作失败」。
  String _humanize(Object error) {
    final message = error.toString();
    if (message.contains('identifier_taken')) {
      return '该手机号/邮箱已注册，请直接登录，或点「忘记密码」找回';
    }
    if (message.contains('invalid_credentials')) return '账号或密码不正确';
    if (message.contains('account_locked')) return '连续失败次数过多，请 15 分钟后再试';
    if (message.contains('weak_password')) return '密码至少 8 位';
    if (message.contains('password_unchanged')) return '新密码不能与原密码相同';
    if (message.contains('invalid_token') || message.contains('expired_token')) {
      return '重置令牌无效或已过期，请重新获取';
    }
    if (message.contains('invalid_phone')) return '手机号格式不正确（需 11 位大陆号码）';
    if (message.contains('invalid_email')) return '邮箱格式不正确';
    if (message.contains('invalid_nickname')) return '昵称不合法：2–16 个字，不能含 @ 或长串数字';
    if (message.contains('too_many_requests')) return '操作太频繁，请稍后再试';
    return message;
  }

  Future<void> _submit() async {
    final identifier = _identifier.text.trim();
    final password = _password.text;
    if (!_looksValid(identifier)) {
      setState(() => _error = _kind.hint);
      return;
    }
    if (password.length < 8 && _mode != AuthMode.forgot) {
      setState(() => _error = '密码至少 8 位');
      return;
    }
    await _run(() async {
      if (_mode == AuthMode.login) {
        await widget.callbacks.onLogin(_kind, identifier, password);
        _password.clear();
      } else if (_mode == AuthMode.register) {
        final nickname = _nickname.text.trim();
        await widget.callbacks.onRegister(
            _kind, identifier, password, nickname.isEmpty ? null : nickname);
        _password.clear();
      } else if (_mode == AuthMode.forgot) {
        final token =
            await widget.callbacks.onForgot(_kind, identifier);
        if (!mounted) return;
        setState(() {
          _hint = token == null
              ? '如果该账号已注册，重置方式已发送。请查收后回来粘贴令牌。'
              : '开发环境：重置令牌已显示在下方，请粘贴到下一栏。';
          // 开发通道才有的回显：直接填好，省一次复制粘贴
          if (token != null) {
            _resetToken.text = token;
            _mode = AuthMode.reset;
          }
        });
      } else {
        await widget.callbacks.onReset(_resetToken.text.trim(), _newPassword.text);
        _newPassword.clear();
        _resetToken.clear();
        if (!mounted) return;
        setState(() {
          _hint = '密码已更新，请用新密码登录。';
          _mode = AuthMode.login;
        });
      }
    });
  }

  Future<void> _suggestNickname() async {
    await _run(() async {
      final suggestion = await widget.callbacks.onSuggestNickname();
      if (mounted) setState(() => _nickname.text = suggestion);
    });
  }

  String get _primaryLabel => switch (_mode) {
        AuthMode.login => '登录',
        AuthMode.register => '注册并登录',
        AuthMode.forgot => '获取重置令牌',
        AuthMode.reset => '设置新密码',
      };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final error = _error ?? widget.errorText;

    return Scaffold(
      appBar: AppBar(
        title: Text(switch (_mode) {
          AuthMode.login => '登录 AI 管家',
          AuthMode.register => '注册账号',
          AuthMode.forgot => '找回密码',
          AuthMode.reset => '设置新密码',
        }),
        leading: _mode != AuthMode.login
            ? IconButton(
                icon: const Icon(Icons.arrow_back),
                onPressed: _busy
                    ? null
                    : () => setState(() {
                          _mode = AuthMode.login;
                          _error = null;
                          _hint = null;
                        }),
              )
            : (widget.onSkip == null
                ? null
                : IconButton(
                    icon: const Icon(Icons.close),
                    tooltip: '先不登录',
                    onPressed: _busy ? null : widget.onSkip,
                  )),
      ),
      body: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          if (_mode == AuthMode.login || _mode == AuthMode.register) ...[
            SegmentedButton<IdentKind>(
              segments: [
                for (final kind in IdentKind.values)
                  ButtonSegment(value: kind, label: Text(kind.label)),
              ],
              selected: {_kind},
              onSelectionChanged: _busy
                  ? null
                  : (selection) => setState(() => _kind = selection.first),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _identifier,
              keyboardType: _kind == IdentKind.phone
                  ? TextInputType.phone
                  : TextInputType.emailAddress,
              decoration: InputDecoration(
                labelText: _kind.label,
                hintText: _kind.hint,
                prefixIcon: Icon(_kind == IdentKind.phone
                    ? Icons.smartphone
                    : Icons.alternate_email),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _password,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: '密码（至少 8 位）',
                prefixIcon: Icon(Icons.lock_outline),
              ),
            ),
          ],
          if (_mode == AuthMode.register) ...[
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _nickname,
                    decoration: const InputDecoration(
                      labelText: '昵称（可留空，会自动生成）',
                      prefixIcon: Icon(Icons.badge_outlined),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                OutlinedButton(
                  onPressed: _busy ? null : _suggestNickname,
                  child: const Text('随机一个'),
                ),
              ],
            ),
          ],
          if (_mode == AuthMode.forgot) ...[
            const SizedBox(height: 8),
            Text(
              '我们会向该账号发送一次性重置令牌，15 分钟内有效、只能用一次。',
              style: theme.textTheme.bodySmall,
            ),
          ],
          if (_mode == AuthMode.reset) ...[
            const SizedBox(height: 12),
            TextField(
              controller: _resetToken,
              decoration: const InputDecoration(
                labelText: '重置令牌',
                prefixIcon: Icon(Icons.vpn_key_outlined),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _newPassword,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: '新密码（至少 8 位）',
                prefixIcon: Icon(Icons.lock_reset),
              ),
            ),
          ],
          if (_hint != null) ...[
            const SizedBox(height: 12),
            Text(_hint!, style: TextStyle(color: theme.colorScheme.primary)),
          ],
          if (error != null) ...[
            const SizedBox(height: 12),
            Text(error, style: TextStyle(color: theme.colorScheme.error)),
          ],
          const SizedBox(height: 24),
          FilledButton(
            onPressed: _busy ? null : _submit,
            child: _busy
                ? const SizedBox(
                    height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2))
                : Text(_primaryLabel),
          ),
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              if (_mode == AuthMode.login)
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => setState(() {
                            _mode = AuthMode.forgot;
                            _error = null;
                            _hint = null;
                          }),
                  child: const Text('忘记密码'),
                )
              else
                const SizedBox.shrink(),
              if (_mode == AuthMode.login || _mode == AuthMode.forgot)
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => setState(() {
                            _mode = AuthMode.register;
                            _error = null;
                            _hint = null;
                          }),
                  child: const Text('还没有账号？去注册'),
                )
              else
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => setState(() {
                            _mode = AuthMode.login;
                            _error = null;
                            _hint = null;
                          }),
                  child: const Text('已有账号？去登录'),
                ),
            ],
          ),
          if (_mode == AuthMode.login && widget.onSkip != null) ...[
            const SizedBox(height: 4),
            Center(
              child: TextButton(
                onPressed: _busy ? null : widget.onSkip,
                child: const Text('先不登录，以访客身份继续（只能文字查询）'),
              ),
            ),
          ],
          const Divider(height: 32),
          Text(
            '隐私说明',
            style: theme.textTheme.titleSmall,
          ),
          const SizedBox(height: 4),
          Text(
            '· 手机号/邮箱加密存储，界面只显示脱敏值（如 138****0007）\n'
            '· 密码只保存加盐哈希，任何接口都不会回传原文\n'
            '· 登录状态只在本机保存 token，不保存密码\n'
            '· 定位坐标在上报前会先粗化（约百米级），精确位置不出设备',
            style: theme.textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}
