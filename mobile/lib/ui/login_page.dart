/// 账号登录页（三、P7 步骤3）。
///
/// **明文密码不在本地持久化**：密码只存在于本页的内存中，登录成功后立即丢弃，
/// 落盘的只有 token（见 core/session.dart）。
library;

import 'package:flutter/material.dart';

class LoginPage extends StatefulWidget {
  const LoginPage({super.key, required this.onSubmit, this.errorText});

  final Future<void> Function(String account, String password) onSubmit;
  final String? errorText;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _account = TextEditingController();
  final _password = TextEditingController();
  bool _busy = false;

  @override
  void dispose() {
    _account.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() => _busy = true);
    try {
      await widget.onSubmit(_account.text, _password.text);
      _password.clear();               // 用完立即清掉内存中的明文
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('登录 AI 管家')),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            TextField(
              controller: _account,
              decoration: const InputDecoration(labelText: '账号'),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _password,
              obscureText: true,
              decoration: const InputDecoration(labelText: '密码'),
            ),
            if (widget.errorText != null) ...[
              const SizedBox(height: 12),
              Text(widget.errorText!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ],
            const SizedBox(height: 24),
            FilledButton(
              onPressed: _busy ? null : _submit,
              child: _busy ? const CircularProgressIndicator() : const Text('登录'),
            ),
          ],
        ),
      ),
    );
  }
}
