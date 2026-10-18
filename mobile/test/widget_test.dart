/// Flutter 外壳的组件测试。
///
/// 取代 ~{flutter create}~ 生成的默认计数器测试 —— 那个测的是模板 App，
/// 与本项目无关，留着只会永远失败（还容易被当成「测试通过」的噪音）。
library;

import 'package:aibutler_core/aibutler_core.dart';
import 'package:aibutler_mobile/ui/chat_page.dart';
import 'package:aibutler_mobile/ui/login_page.dart';
import 'package:aibutler_mobile/ui/widgets/streaming_text.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('登录页显示账号与密码输入框', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: LoginPage(onSubmit: (a, p) async {}),
    ));
    expect(find.text('登录 AI 管家'), findsOneWidget);
    expect(find.text('账号'), findsOneWidget);
    expect(find.text('密码'), findsOneWidget);
  });

  testWidgets('登录失败时显示错误文案', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: LoginPage(onSubmit: (a, p) async {}, errorText: '登录失败：凭据无效'),
    ));
    expect(find.text('登录失败：凭据无效'), findsOneWidget);
  });

  testWidgets('对话页渲染消息并支持文字输入（双向交互）', (tester) async {
    final messages = [
      ChatMessage(role: ChatRole.user, text: '帮我安排行程'),
      ChatMessage(role: ChatRole.assistant, text: '好的，正在为你规划'),
    ];
    await tester.pumpWidget(MaterialApp(
      home: ChatPage(
        messages: messages,
        onSendText: (t) async {},
        onStartVoice: () async {},
        onInterrupt: () async {},
      ),
    ));
    expect(find.text('帮我安排行程'), findsOneWidget);
    expect(find.text('好的，正在为你规划'), findsOneWidget);
    expect(find.text('也可以直接打字'), findsOneWidget);
  });

  testWidgets('会话失效时弹出重新登录对话框（三、P7 步骤5）', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: ChatPage(
        messages: const [],
        onSendText: (t) async {},
        onStartVoice: () async {},
        onInterrupt: () async {},
        sessionExpired: true,
        onRelogin: () async {},
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.text('会话已失效'), findsOneWidget);
    expect(find.text('重新登录'), findsOneWidget);
  });

  testWidgets('带风险标注的消息用醒目样式（P5/P6 风险标注透传）', (tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(body: StreamingText(text: '这条有风险', riskNoted: true)),
    ));
    expect(find.text('这条有风险'), findsOneWidget);
    expect(find.byIcon(Icons.warning_amber_rounded), findsOneWidget);
  });
}
