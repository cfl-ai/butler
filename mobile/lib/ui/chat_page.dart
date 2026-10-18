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
    required this.onStartVoice,
    required this.onInterrupt,
    this.listening = false,
    this.sessionExpired = false,
    this.onRelogin,
  });

  final List<ChatMessage> messages;
  final Future<void> Function(String text) onSendText;
  final Future<void> Function() onStartVoice;
  final Future<void> Function() onInterrupt;
  final bool listening;

  /// 会话失效标记：为 true 时由本页弹窗（三、P7 步骤5）。
  final bool sessionExpired;
  final Future<void> Function()? onRelogin;

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

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('AI 管家')),
      body: Column(
        children: [
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
                      child: StreamingText(
                        text: message.text,
                        riskNoted: message.riskNoted,
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
                  IconButton(
                    onPressed: widget.listening ? widget.onInterrupt : widget.onStartVoice,
                    icon: Icon(widget.listening ? Icons.stop : Icons.mic),
                    tooltip: widget.listening ? '打断' : '语音输入',
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
