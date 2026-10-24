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

/// 全空实现：页面本身不碰网络，回调只用来验证「点下去有没有被调用」。
AuthCallbacks _noopCallbacks() => AuthCallbacks(
      onLogin: (kind, identifier, password) async {},
      onRegister: (kind, identifier, password, nickname) async {},
      onForgot: (kind, identifier) async => null,
      onReset: (token, newPassword) async {},
      onSuggestNickname: () async => '示例昵称',
    );

void main() {
  testWidgets('登录页有手机号/邮箱切换与密码框（U67）', (tester) async {
    await tester.pumpWidget(
        MaterialApp(home: LoginPage(callbacks: _noopCallbacks())));
    expect(find.text('登录 AI 管家'), findsOneWidget);
    // 「手机号」既是切换按钮也是输入框标签，所以不钉死出现次数
    expect(find.text('手机号'), findsWidgets);
    expect(find.text('邮箱'), findsOneWidget);
    expect(find.text('密码（至少 8 位）'), findsOneWidget);
    // 隐私说明四条是给用户看的承诺，掉了就是掉了
    expect(find.text('隐私说明'), findsOneWidget);
  });

  testWidgets('登录失败时显示错误文案', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: LoginPage(
          callbacks: _noopCallbacks(), errorText: '登录失败：凭据无效'),
    ));
    expect(find.text('登录失败：凭据无效'), findsOneWidget);
  });

  testWidgets('忘记密码切到找回模式（U68）', (tester) async {
    await tester.pumpWidget(
        MaterialApp(home: LoginPage(callbacks: _noopCallbacks())));
    await tester.tap(find.text('忘记密码'));
    await tester.pumpAndSettle();
    expect(find.text('找回密码'), findsOneWidget);        // AppBar 标题变了
    expect(find.text('获取重置令牌'), findsOneWidget);    // 主按钮文案变了
  });

  testWidgets('访客点进来的登录页必须留退路（onSkip）', (tester) async {
    var skipped = 0;
    await tester.pumpWidget(MaterialApp(
      home: LoginPage(
          callbacks: _noopCallbacks(), onSkip: () => skipped++),
    ));
    await tester.tap(find.text('先不登录，以访客身份继续（只能文字查询）'));
    await tester.pump();
    expect(skipped, 1);
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

  testWidgets('账号与定位入口按能力渲染，为 null 就不出现（U67 / U71）', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: ChatPage(
        messages: const [],
        onSendText: (t) async {},
        onInterrupt: () async {},
      ),
    ));
    expect(find.byIcon(Icons.manage_accounts_outlined), findsNothing);
    expect(find.byIcon(Icons.explore_outlined), findsNothing);

    await tester.pumpWidget(MaterialApp(
      home: ChatPage(
        messages: const [],
        onSendText: (t) async {},
        onInterrupt: () async {},
        onOpenAccount: () async {},
        onOpenLocation: () async {},
      ),
    ));
    expect(find.byIcon(Icons.manage_accounts_outlined), findsOneWidget);
    expect(find.byIcon(Icons.explore_outlined), findsOneWidget);
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
