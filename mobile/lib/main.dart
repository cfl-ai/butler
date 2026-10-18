/// P7 移动端入口：把平台适配器接到端口上，再交给业务层。
///
/// 计划书依据
/// ----------
/// - 三、P7「搭建独立手机 App 底层框架（**纯客户端、非网页**）」
/// - 一、项目概述「移动端（仅感知 + 交互）… 云端（全部智能运算）」
///
/// 因此本 App **不含任何业务决策**：路由、质检、支付、知识库全在云端，
/// 端上只做登录、收音、渲染、播报、上报与本地缓存。
library;

import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import 'adapters/asset_models.dart';
import 'adapters/platform_adapters.dart';
import 'package:aibutler_core/aibutler_core.dart';
import 'ui/chat_page.dart';
import 'ui/login_page.dart';

/// 云端基址。真实部署时按环境注入（四环境隔离）。
const String kCloudBaseUrl = String.fromEnvironment('AIBUTLER_CLOUD',
    defaultValue: 'http://10.0.2.2:8000');   // Android 模拟器访问宿主

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const AiButlerApp());
}

class AiButlerApp extends StatefulWidget {
  const AiButlerApp({super.key});

  @override
  State<AiButlerApp> createState() => _AiButlerAppState();
}

class _AiButlerAppState extends State<AiButlerApp> {
  late final SessionManager _session;
  late final ApiClient _api;
  late final LocalCache _cache;
  late final DeviceReporter _reporter;
  late final WakeWordService _wake;
  late final VoicePipeline _pipeline;

  final List<ChatMessage> _messages = [];
  bool _signedIn = false;
  bool _listening = false;
  bool _sessionExpired = false;
  String? _loginError;

  @override
  void initState() {
    super.initState();
    final platform = Platform.isAndroid ? 'android' : 'ios';
    final transport = HttpTransport(baseUrl: kCloudBaseUrl);

    _session = SessionManager(store: FlutterSecureStore());
    _api = ApiClient(transport: transport, session: _session);
    _cache = LocalCache(
        store: FileCacheStore(Directory(
            (Platform.environment['AIBUTLER_CACHE_DIR'] ?? Directory.systemTemp.path))));
    _reporter = DeviceReporter(
        location: const GeolocatorLocation(),
        deviceInfo: const BasicDeviceInfo(),
        api: _api,
        session: _session);
    // 登录后要拉起的常驻唤醒前台服务（三、P7「后台常驻监听」）。
    _wakeService = const WakeForegroundService();

    _wake = WakeWordService(
        wakeWord: SherpaWakeWord(
          platform: platform,
          // 随包模型要先用 asset_models.dart 释放到可写目录才拿得到路径，
          // 所以这里给的是「怎么拿到路径」，等真正 start() 时再解析。
          modelResolver: ensureWakeWordModel,
        ),
        session: _session);
    _pipeline = VoicePipeline(
        microphone: RecorderMicrophone(),
        vad: SileroVad(),
        asr: SherpaAsr(),
        tts: SherpaTts(),
        api: _api,
        session: _session);

    // 设备状态上报（三、P7 步骤7）：登录后每 30 分钟一次。
    // 未登录时 reportOnce 自己会跳过，这里不用再判。
    _reporterSubscription = _reporter
        .periodic(interval: const Duration(minutes: 30), clock: DateTime.now)
        .listen((_) {}, onError: (_) {});

    _restore();
  }

  StreamSubscription<bool>? _reporterSubscription;
  late final WakeForegroundService _wakeService;

  Future<void> _restore() async {
    final ok = await _session.restore(DateTime.now());
    if (!ok) return;
    await _cache.bindAccount(_session.userId!);
    setState(() => _signedIn = true);
    await _startResidentWake();
  }

  /// 拉起后台常驻唤醒的前台服务。
  ///
  /// **登录后才做**：唤醒的下一步是校验会话，未登录时后台常驻只是白耗电。
  /// 失败不阻断登录 —— 前台服务起不来（例如通知被禁用）应该是「后台唤醒
  /// 不可用」而不是「App 用不了」，所以如实提示、照常继续。
  Future<void> _startResidentWake() async {
    try {
      final started = await _wakeService.start();
      if (!started && mounted) {
        _messages.add(ChatMessage(
            role: ChatRole.assistant,
            text: '当前平台不支持后台常驻唤醒，将只在前台监听。'));
        setState(() {});
      }
    } catch (error) {
      if (mounted) {
        _messages.add(ChatMessage(
            role: ChatRole.assistant,
            text: '后台常驻唤醒未能启动（' +
                error.toString() +
                '）。可到系统通知设置里允许通知后重试。'));
        setState(() {});
      }
    }
  }

  Future<void> _login(String account, String password) async {
    try {
      final creds = await _api.login(
          account: account, password: password, deviceId: 'phone', now: DateTime.now());
      await _cache.bindAccount(creds.userId);
      if (mounted) {
        setState(() {
          _signedIn = true;
          _loginError = null;
          _sessionExpired = false;
        });
        await _startResidentWake();
      }
    } catch (error) {
      if (mounted) setState(() => _loginError = '登录失败：' + error.toString());
    }
  }

  Future<void> _signOut() async {
    await _pipeline.interrupt();
    await _wake.stop();
    await _wakeService.stop();         // 登出就别再占着麦克风常驻了
    await _cache.clearCurrent();       // 登出即清当前账号缓存
    await _session.signOut();
    if (mounted) setState(() => _signedIn = false);
  }

  Future<void> _sendText(String text) async {
    _messages.add(ChatMessage(role: ChatRole.user, text: text));
    final reply = ChatMessage(role: ChatRole.assistant, text: '');
    _messages.add(reply);
    setState(() {});

    await for (final event
        in _pipeline.respond(utterance: text, now: DateTime.now())) {
      switch (event.kind) {
        case PipelineEventKind.chunk:
          reply.text += event.text;
          if (event.chunk?.carriesRisk ?? false) reply.riskNoted = true;
          break;
        case PipelineEventKind.sessionExpired:
          reply.sessionExpired = true;
          setState(() => _sessionExpired = true);
          break;
        case PipelineEventKind.failed:
        case PipelineEventKind.interrupted:
          reply.text += '（已中断）';
          break;
        default:
          break;
      }
      if (mounted) setState(() {});
    }
    await _cache.put('lastMessage', reply.text);
  }

  Future<void> _startVoice() async {
    setState(() => _listening = true);
    try {
      await for (final event in _pipeline.runOnce(now: DateTime.now())) {
        if (event.kind == PipelineEventKind.sessionExpired) {
          setState(() => _sessionExpired = true);
        }
      }
    } catch (error) {
      // 语音链路尚未接入（见 adapters 的完成度声明）：如实提示，不假装可用
      _messages.add(ChatMessage(
          role: ChatRole.assistant,
          text: '语音链路尚未接入真机：' + error.toString()));
      setState(() {});
    } finally {
      if (mounted) setState(() => _listening = false);
    }
  }

  @override
  void dispose() {
    // 之前这里没有 dispose：唤醒引擎与语音流水线**从未被释放**。
    // 二者都持有原生资源（sherpa-onnx 会话），不释放就是实打实的泄漏。
    _reporterSubscription?.cancel();
    unawaited(_wakeService.stop());   // 停掉常驻唤醒服务
    unawaited(_pipeline.dispose());   // 停掉麦克风 / VAD / ASR / TTS 各自的资源
    unawaited(_wake.stop());          // 停掉唤醒引擎持有的原生会话
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'AI 管家',
      theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
      home: _signedIn
          ? ChatPage(
              messages: _messages,
              onSendText: _sendText,
              onStartVoice: _startVoice,
              onInterrupt: _pipeline.interrupt,
              listening: _listening,
              sessionExpired: _sessionExpired,
              onRelogin: _signOut,
            )
          : LoginPage(onSubmit: _login, errorText: _loginError),
    );
  }
}
