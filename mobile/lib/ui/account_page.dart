/// 账号与模型设置页（U67 / U69 / U70）。
///
/// 两件事必须在这一页说清楚，否则用户会以为"设置没生效"：
///
/// * **标识始终是脱敏的**，连本人都看不到完整手机号 —— 这是设计，不是 bug；
/// * **模型分三档**（免费 / 限免 / 自带 key），额度与归属都以账号为单位，
///   BYOK 的 key 存进去就再也读不出来，只能覆盖或删除。
library;

import 'package:flutter/material.dart';

import 'package:aibutler_core/aibutler_core.dart';

class AccountPage extends StatefulWidget {
  const AccountPage({
    super.key,
    required this.profile,
    required this.models,
    required this.onSetNickname,
    required this.onRefreshNickname,
    required this.onChangePassword,
    required this.onSelectModel,
    required this.onBindByok,
    required this.onRemoveModel,
    required this.onSignOut,
  });

  final AccountProfile profile;

  /// `GET /api/models` 的原样返回（models / binding / quota）。
  final Map<String, dynamic> models;

  final Future<String> Function(String nickname) onSetNickname;
  final Future<String> Function() onRefreshNickname;
  final Future<int> Function(String oldPassword, String newPassword) onChangePassword;
  final Future<void> Function(String modelKey) onSelectModel;
  final Future<void> Function(String baseUrl, String model, String apiKey, String name)
      onBindByok;
  final Future<void> Function() onRemoveModel;
  final Future<void> Function() onSignOut;

  @override
  State<AccountPage> createState() => _AccountPageState();
}

class _AccountPageState extends State<AccountPage> {
  late AccountProfile _profile = widget.profile;
  late Map<String, dynamic> _models = widget.models;
  bool _busy = false;
  String? _message;
  String? _error;

  List<Map<String, dynamic>> get _modelItems => ((_models['models'] as List?) ?? const [])
      .whereType<Map>()
      .map((e) => e.cast<String, dynamic>())
      .toList(growable: false);

  Map<String, dynamic>? get _binding =>
      (_models['binding'] as Map?)?.cast<String, dynamic>();

  Map<String, dynamic> get _quota =>
      ((_models['quota'] as Map?) ?? const {}).cast<String, dynamic>();

  Future<void> _run(Future<void> Function() action, {String? done}) async {
    setState(() {
      _busy = true;
      _error = null;
      _message = null;
    });
    try {
      await action();
      if (mounted && done != null) setState(() => _message = done);
    } catch (error) {
      if (mounted) setState(() => _error = _humanize(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  String _humanize(Object error) {
    final text = error.toString();
    if (text.contains('too_many_requests')) return '刷新太频繁，请一分钟后再试';
    if (text.contains('invalid_nickname')) return '昵称不合法：2–16 个字，不能含 @ 或长串数字';
    if (text.contains('password_unchanged')) return '新密码不能与原密码相同';
    if (text.contains('weak_password')) return '密码至少 8 位';
    if (text.contains('model_unavailable') || text.contains('missing_credential')) {
      return '该模型暂不可用（平台未配置对应凭据）';
    }
    if (text.contains('quota_exhausted')) return '今日额度已用完，可改用自带密钥的模型';
    return text;
  }

  Future<void> _editNickname() async {
    final controller = TextEditingController(text: _profile.nickname);
    final value = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('修改昵称'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(
            labelText: '昵称',
            helperText: '2–16 个字；相机、emoji 也算 1 个字',
          ),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context), child: const Text('取消')),
          FilledButton(
              onPressed: () => Navigator.pop(context, controller.text),
              child: const Text('保存')),
        ],
      ),
    );
    if (value == null) return;
    await _run(() async {
      final nickname = await widget.onSetNickname(value);
      if (mounted) setState(() => _profile = _copyProfile(nickname: nickname));
    }, done: '昵称已更新');
  }

  AccountProfile _copyProfile({String? nickname}) => AccountProfile(
        userId: _profile.userId,
        identKind: _profile.identKind,
        identMasked: _profile.identMasked,
        nickname: nickname ?? _profile.nickname,
        prefs: _profile.prefs,
      );

  Future<void> _changePassword() async {
    final oldController = TextEditingController();
    final newController = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('修改密码'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: oldController,
              obscureText: true,
              decoration: const InputDecoration(labelText: '原密码'),
            ),
            TextField(
              controller: newController,
              obscureText: true,
              decoration: const InputDecoration(labelText: '新密码（至少 8 位）'),
            ),
            const SizedBox(height: 8),
            const Text('改密成功后，所有设备都需要重新登录。',
                style: TextStyle(fontSize: 12)),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('确认')),
        ],
      ),
    );
    if (ok != true) return;
    await _run(() async {
      await widget.onChangePassword(oldController.text, newController.text);
    }, done: '密码已更新，请用新密码重新登录');
  }

  Future<void> _addByok() async {
    final baseUrl = TextEditingController();
    final model = TextEditingController();
    final apiKey = TextEditingController();
    final name = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('接入自带模型'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: baseUrl,
                decoration: const InputDecoration(
                  labelText: '接入地址',
                  hintText: 'https://api.example.com/v1',
                ),
              ),
              TextField(
                controller: model,
                decoration: const InputDecoration(
                  labelText: '模型名称',
                  hintText: '例如 deepseek-chat',
                ),
              ),
              TextField(
                controller: apiKey,
                obscureText: true,
                decoration: const InputDecoration(labelText: 'API Key'),
              ),
              TextField(
                controller: name,
                decoration: const InputDecoration(labelText: '显示名称（可选）'),
              ),
              const SizedBox(height: 8),
              const Text(
                'Key 会加密存储，保存后**无法再查看**，只能覆盖或删除绑定。',
                style: TextStyle(fontSize: 12),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('保存')),
        ],
      ),
    );
    if (ok != true) return;
    await _run(() async {
      await widget.onBindByok(
          baseUrl.text.trim(), model.text.trim(), apiKey.text, name.text.trim());
      // 保存完立刻清掉明文 key 的内存副本
      apiKey.clear();
    }, done: '已接入，Key 只保存了密文');
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final binding = _binding;

    return Scaffold(
      appBar: AppBar(title: const Text('账号与模型')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: ListTile(
              leading: const CircleAvatar(child: Icon(Icons.person)),
              title: Text(_profile.nickname),
              subtitle: Text(
                '${_profile.isPhone ? "手机号" : "邮箱"}：${_profile.identMasked}\n'
                '完整标识加密存储，界面上不显示原文',
              ),
              isThreeLine: true,
              trailing: IconButton(
                icon: const Icon(Icons.edit),
                tooltip: '修改昵称',
                onPressed: _busy ? null : _editNickname,
              ),
            ),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: _busy
                      ? null
                      : () => _run(() async {
                            final nickname = await widget.onRefreshNickname();
                            if (mounted) {
                              setState(() =>
                                  _profile = _copyProfile(nickname: nickname));
                            }
                          }, done: '昵称已随机刷新'),
                  icon: const Icon(Icons.casino_outlined),
                  label: const Text('随机刷新昵称'),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: _busy ? null : _changePassword,
                  icon: const Icon(Icons.lock_reset),
                  label: const Text('修改密码'),
                ),
              ),
            ],
          ),
          if (_message != null) ...[
            const SizedBox(height: 8),
            Text(_message!, style: TextStyle(color: theme.colorScheme.primary)),
          ],
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
          ],
          const Divider(height: 32),
          Text('大模型', style: theme.textTheme.titleMedium),
          const SizedBox(height: 4),
          Text(
            '免费额度：${_quota["free_remaining"] ?? "-"} 次 · '
            '限免额度：${_quota["trial_remaining"] ?? "-"} 次',
            style: theme.textTheme.bodySmall,
          ),
          const SizedBox(height: 8),
          ..._modelItems.map((item) {
            final key = (item['model_key'] as String?) ?? '';
            final selected = item['selected'] == true;
            final usable = item['usable'] == true;
            final hint = (item['unusable_reason'] as String?) ?? '';
            return RadioListTile<String>(
              value: key,
              groupValue: binding?['model_key'] as String?,
              onChanged: !usable || _busy
                  ? null
                  : (value) => _run(() async {
                        await widget.onSelectModel(value!);
                        if (mounted) {
                          setState(() => _models = {
                                ..._models,
                                'binding': {
                                  'model_key': value,
                                  'label': item['label'],
                                },
                              });
                        }
                      }, done: '已切换模型'),
              title: Text((item['label'] as String?) ?? key),
              subtitle: Text(
                [
                  (item['tier'] as String?) ?? '',
                  if (item['vision'] == true) '支持看图',
                  if (item['tools'] == true) '支持工具调用',
                  if (hint.isNotEmpty) hint,
                  if (item['description'] != null &&
                      (item['description'] as String).isNotEmpty)
                    item['description'] as String,
                ].join(' · '),
              ),
              secondary: selected ? const Icon(Icons.check_circle) : null,
            );
          }),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: _busy ? null : _addByok,
                  icon: const Icon(Icons.key),
                  label: const Text('接入自带模型'),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: binding == null || _busy
                      ? null
                      : () => _run(() async {
                            await widget.onRemoveModel();
                            if (mounted) {
                              setState(() =>
                                  _models = {..._models, 'binding': null});
                            }
                          }, done: '已解除绑定（密钥密文一并删除）'),
                  icon: const Icon(Icons.link_off),
                  label: const Text('解除绑定'),
                ),
              ),
            ],
          ),
          if (binding != null && binding['tier'] == 'byok') ...[
            const SizedBox(height: 8),
            Text(
              '自带密钥：${binding["api_key_hint"] ?? "—"}'
              '（只显示尾号，原文不入库回传）',
              style: theme.textTheme.bodySmall,
            ),
          ],
          const Divider(height: 32),
          TextButton.icon(
            onPressed: _busy ? null : () => widget.onSignOut(),
            icon: const Icon(Icons.logout),
            label: const Text('退出登录（回到访客，只能文字查询）'),
          ),
        ],
      ),
    );
  }
}
