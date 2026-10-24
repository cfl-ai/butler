/// 类 Siri / 小爱对话界面（三、P7 步骤3、步骤6）。
///
/// 双向交互：语音（唤醒 → 收音 → 识别 → 播报）与**文字手动输入**并存。
/// 会话失效时弹窗引导重新登录（三、P7 步骤5）。
library;

import 'package:flutter/material.dart';

import 'package:aibutler_core/aibutler_core.dart';
import 'widgets/streaming_text.dart';

class ChatPage extends StatefulWidget {
  const ChatPage({
    super.key,
    required this.messages,
    required this.onSendText,
    required this.onInterrupt,
    this.onStartVoice,
    this.onPickImage,
    this.listening = false,
    this.sessionExpired = false,
    this.onRelogin,
    this.tier = AccessTier.guest,
    this.guestRemaining,
    this.onLogin,
    this.onSignOut,
  });

  final List<ChatMessage> messages;
  final Future<void> Function(String text) onSendText;

  /// 语音输入。为 null 表示**当前层级没有语音能力**（U64：未登录只能文字），
  /// 此时不显示麦克风 —— 显示一个点了没反应的按钮比不显示更糟。
  final Future<void> Function()? onStartVoice;
  final Future<void> Function() onInterrupt;

  /// 图片输入（台账 U61）：相机或相册取图。为 null 时不显示入口，
  /// 而不是显示一个点了没反应的按钮。
  final Future<void> Function()? onPickImage;
  final bool listening;

  /// 会话失效标记：为 true 时由本页弹窗（三、P7 步骤5）。
  final bool sessionExpired;
  final Future<void> Function()? onRelogin;

  /// 当前身份层级。**只用于渲染**，权限真值源在服务端（U64）。
  final AccessTier tier;

  /// 访客剩余提问次数；成员为 null。
  final int? guestRemaining;

  /// 访客点「登录」；成员为 null。
  final VoidCallback? onLogin;

  /// 成员点「登出」；访客为 null。
  final Future<void> Function()? onSignOut;

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final _input = TextEditingController();
  bool _dialogShown = false;

  @override
  void initState() {
    super.initState();
    // 首次构建也要判一次：只在 didUpdateWidget 里判会漏掉
    // 「App 启动时就发现会话已失效」这条路径 —— 那样用户看到的是一个
    // 沉默的对话页，而不是重新登录的引导。（组件测试抓到的真 bug）
    _maybeShowExpiredDialog();
  }

  @override
  void didUpdateWidget(ChatPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    _maybeShowExpiredDialog();
  }

  void _maybeShowExpiredDialog() {
    if (widget.sessionExpired && !_dialogShown) {
      _dialogShown = true;
      WidgetsBinding.instance.addPostFrameCallback((_) => _showExpiredDialog());
    }
  }

  Future<void> _showExpiredDialog() async {
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('会话已失效'),
        content: const Text('长时间未操作或凭证已过期，请重新登录。'),
        actions: [
          TextButton(
            onPressed: () {
              Navigator.of(context).pop();
              _dialogShown = false;
            },
            child: const Text('知道了'),
          ),
          FilledButton(
            onPressed: () {
              Navigator.of(context).pop();
              _dialogShown = false;
              widget.onRelogin?.call();
            },
            child: const Text('重新登录'),
          ),
        ],
      ),
    );
  }

  /// 访客态提示条：说清「现在能做什么、登录多什么、还剩几次」。
  ///
  /// 三条都说，是因为少任何一条都会引出误解：
  /// 不说「只能文字」→ 用户找不到麦克风；不说「登录多什么」→ 用户不知道值不值；
  /// 不说剩余次数 → 用到第 30 次被拒绝时像是坏了。
  Widget _guestBanner() {
    final remaining = widget.guestRemaining;
    return Container(
      width: double.infinity,
      color: Theme.of(context).colorScheme.secondaryContainer,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        children: [
          const Icon(Icons.info_outline, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '当前为访客模式：仅支持文字提问。'
              '登录后可语音唤起、声纹识别与图文识别。'
              '${remaining == null ? '' : '（剩余 $remaining 次）'}',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final guest = widget.tier == AccessTier.guest;
    return Scaffold(
      appBar: AppBar(
        title: Text(guest ? 'AI 管家（访客）' : 'AI 管家'),
        actions: [
          if (widget.onLogin != null)
            TextButton.icon(
              onPressed: widget.onLogin,
              icon: const Icon(Icons.login, size: 18),
              label: const Text('登录'),
            ),
          if (widget.onSignOut != null)
            IconButton(
              onPressed: widget.onSignOut,
              icon: const Icon(Icons.logout),
              tooltip: '登出（回到访客模式）',
            ),
        ],
      ),
      body: Column(
        children: [
          if (guest) _guestBanner(),
          Expanded(
            child: ListView.builder(
              padding: const EdgeInsets.all(12),
              itemCount: widget.messages.length,
              itemBuilder: (context, index) {
                final message = widget.messages[index];
                final mine = message.role == ChatRole.user;
                return Align(
                  alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
                  child: Card(
                    child: Padding(
                      padding: const EdgeInsets.all(10),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          // 图片输入的原图回显（台账 U61）：让用户确认自己发的是哪张
                          if (message.imageBytes != null) ...[
                            ClipRRect(
                              borderRadius: BorderRadius.circular(8),
                              child: Image.memory(
                                message.imageBytes!,
                                width: 220,
                                fit: BoxFit.cover,
                                errorBuilder: (_, __, ___) =>
                                    const Text('（图片无法显示）'),
                              ),
                            ),
                            const SizedBox(height: 8),
                          ],
                          StreamingText(
                            text: message.text,
                            riskNoted: message.riskNoted,
                          ),
                          // 图文输出：识别结果卡片（台账 U62）
                          if (message.outputImage != null) ...[
                            const SizedBox(height: 8),
                            ClipRRect(
                              borderRadius: BorderRadius.circular(8),
                              child: Image.memory(
                                message.outputImage!,
                                width: 280,
                                fit: BoxFit.fitWidth,
                                errorBuilder: (_, __, ___) =>
                                    const Text('（结果图无法显示）'),
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: Row(
                children: [
                  // 语音入口按能力渲染（U64）：访客看不到麦克风，只看到文字输入
                  if (widget.onStartVoice != null)
                    IconButton(
                      onPressed: widget.listening
                          ? widget.onInterrupt
                          : widget.onStartVoice,
                      icon: Icon(widget.listening ? Icons.stop : Icons.mic),
                      tooltip: widget.listening ? '打断' : '语音输入',
                    ),
                  if (widget.onPickImage != null)
                    IconButton(
                      onPressed: widget.onPickImage,
                      icon: const Icon(Icons.image_outlined),
                      tooltip: '图片识别',
                    ),
                  Expanded(
                    child: TextField(
                      controller: _input,
                      decoration: const InputDecoration(hintText: '也可以直接打字'),
                      onSubmitted: (text) {
                        if (text.trim().isEmpty) return;
                        widget.onSendText(text.trim());
                        _input.clear();
                      },
                    ),
                  ),
                  IconButton(
                    onPressed: () {
                      final text = _input.text.trim();
                      if (text.isEmpty) return;
                      widget.onSendText(text);
                      _input.clear();
                    },
                    icon: const Icon(Icons.send),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
