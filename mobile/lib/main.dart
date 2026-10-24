/// P7 移动端入口：把平台适配器接到端口上，再交给业务层。
///
/// 计划书依据
/// ----------
/// - 三、P7「搭建独立手机 App 底层框架（**纯客户端、非网页**）」
/// - 一、项目概述「移动端（仅感知 + 交互）… 云端（全部智能运算）」
/// - 七、验收标准 11「未登录状态下所有业务接口拒绝执行」
///
/// 登录模式（裁定 U64，人工授权 2026-10-24）
/// ----------------------------------------
/// 「未登录时两端都只能文字查询，登录后可以语音唤起」—— 落在本文件的是：
///
/// | 状态 | 进入的界面 | 能做什么 |
/// |---|---|---|
/// | 未登录 | 直接进对话页（**不是登录页**） | **只能文字**查询；语音/图片/唤醒按能力禁用 |
/// | 已登录 | 对话页 | 文字 + 语音唤起 + 声纹 + 图文识别 |
///
/// 三个实现要点：
/// 1. **访客有凭据**（`/api/session/guest`），不是免鉴权——服务端照样校验，
///    只是能力表里只有 text_query（服务端是权限真值源）。
/// 2. 访客凭据**不进 SessionManager**：`isSignedIn` 必须是「已登录成员」，
///    否则设备上报、唤醒、语音管线这些「登录后才做」的规则会被静默放开。
/// 3. 按钮禁用不是权限——真正的拦截在服务端。端侧禁用只为「别让用户点了才知道不行」。
///
/// 端云分层
/// --------
/// 本 App **不含任何业务决策**：路由、质检、支付、知识库全在云端，
/// 端上只做登录、收音、渲染、播报、上报与本地缓存。
library;

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

import 'adapters/asset_models.dart';
import 'adapters/platform_adapters.dart';
import 'adapters/wake_runner.dart';
import 'package:aibutler_core/aibutler_core.dart';
import 'ui/chat_page.dart';
import 'ui/login_page.dart';

/// 云端基址。真实部署时按环境注入（四环境隔离）。
const String kCloudBaseUrl = String.fromEnvironment('AIBUTLER_CLOUD',
    defaultValue: 'http://10.0.2.2:8000');   // Android 模拟器访问宿主

/// Android 后台常驻唤醒是否已通过**真机可行性验证**（裁定 U23）。
///
/// 默认 false：没在真机上验过「锁屏后仍能唤醒」，就不许在日志与界面里
/// 声称自己处于后台常驻模式。验证通过后由构建参数置为 true。
const bool kBackgroundWakeVerified =
    bool.fromEnvironment('AIBUTLER_BG_WAKE_VERIFIED');

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

  /// 麦克风全 App 只有一个：唤醒监听与语音管线**共用同一个录音器**。
  /// 各建一个的结果是两个 AudioRecord 抢同一路输入，安卓上必然有一个拿不到数据。
  late final RecorderMicrophone _mic;
  late final SherpaWakeWord _kws;
  late final WakeRunner _wakeRunner;
  StreamSubscription<String>? _wakeDetections;

  final List<ChatMessage> _messages = [];

  /// 当前身份的能力档案。**以服务端为准**，本地矩阵只是兜底。
  AccessProfile _profile = AccessProfile.fallback();

  bool _signedIn = false;
  bool _ready = false;          // 是否已决定进入哪个界面
  bool _showLogin = false;      // 访客主动点「登录」时切到登录页
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

    _mic = RecorderMicrophone();
    _kws = SherpaWakeWord(
      platform: platform,
      // 随包模型要先用 asset_models.dart 释放到可写目录才拿得到路径，
      // 所以这里给的是「怎么拿到路径」，等真正 start() 时再解析。
      modelResolver: ensureWakeModel,
      backgroundWakeVerified: kBackgroundWakeVerified,
    );
    _wake = WakeWordService(wakeWord: _kws, session: _session);
    // 唤醒链路：麦克风采样 → KWS 推理。没有这一步，引擎只是被构造出来而已。
    _wakeRunner = WakeRunner(microphone: _mic, engine: _kws);
    _pipeline = VoicePipeline(
        microphone: _mic,
        vad: SileroVad(modelPathResolver: ensureVadModel),
        asr: CloudAsr(api: _api, session: _session),
        tts: CloudTts(api: _api),
        api: _api,
        session: _session);

    // 设备状态上报（三、P7 步骤7）：登录后每 30 分钟一次。
    // 未登录时 reportOnce 自己会跳过，这里不用再判。
    _reporterSubscription = _reporter
        .periodic(interval: const Duration(minutes: 30), clock: DateTime.now)
        .listen((_) {}, onError: (_) {});

    _bootstrap();
  }

  StreamSubscription<bool>? _reporterSubscription;
  late final WakeForegroundService _wakeService;

  /// 启动决策：**能恢复成员会话就是成员，否则直接进访客文字模式**。
  ///
  /// 先登录页的时代结束了（U64）：「未登录」不再等于「不能用」，
  /// 但仍**不等于**「什么都能用」——能力表决定边界。
  Future<void> _bootstrap() async {
    final restored = await _session.restore(DateTime.now());
    if (restored) {
      await _cache.bindAccount(_session.userId!);
      _profile = await _fetchProfile(fallback: AccessProfile.member());
      if (!mounted) return;
      setState(() {
        _signedIn = true;
        _ready = true;
      });
      await _startResidentWake();
      return;
    }

    // 未登录 → 领访客凭据。领不到（服务端关闭访客模式）就停在登录页。
    try {
      final guest = await _api.startGuest(now: DateTime.now(), deviceId: 'phone');
      _profile = AccessProfile.guest(textQueryRemaining: guest.textQueryBudget);
      if (!mounted) return;
      setState(() {
        _signedIn = false;
        _ready = true;
        _showLogin = false;
      });
      _messages.add(ChatMessage(
          role: ChatRole.assistant,
          text: '当前是访客模式：可以直接打字提问。'
              '登录后可使用语音唤起、声纹识别与图文识别。'));
      setState(() {});
    } catch (error) {
      // 如实说明：不是「网络不好」，而是这套部署不给访客用
      if (!mounted) return;
      setState(() {
        _ready = true;
        _showLogin = true;
        _loginError = '访客模式不可用：$error';
      });
    }
  }

  /// 拉取能力档案。**拉不到就用传进来的兜底**（成员/访客各自的本地矩阵），
  /// 绝不因为一次失败就把成员降级成访客（那会让用户莫名其妙丢功能）。
  Future<AccessProfile> _fetchProfile({required AccessProfile fallback}) async {
    try {
      return await _api.accessProfile();
    } catch (error) {
      debugPrint('[WARN] 能力档案获取失败，按本地矩阵兜底：$error');
      return fallback;
    }
  }

  /// 拉起后台常驻唤醒的前台服务。**仅成员**（U64：唤醒需要登录）。
  ///
  /// 失败不阻断登录 —— 前台服务起不来（例如通知被禁用）应该是「后台唤醒
  /// 不可用」而不是「App 用不了」，所以如实提示、照常继续。
  Future<void> _startResidentWake() async {
    if (!_profile.canWake) {
      debugPrint('[INFO] 当前层级无唤醒能力，跳过常驻监听');
      return;
    }
    // 通知权限先要：Android 13+ 没它，前台服务的常驻通知不可见，
    // 用户既看不到「正在待命」，也没法一键停掉麦克风。
    final notified = await _wakeService.requestNotificationPermission();
    if (!notified) {
      debugPrint('[WARN] 通知权限未授予：常驻通知将不可见');
    }
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

    // 前台服务只解决「进程活着 + 握着麦克风使用权」，它自己不听声音。
    // 真正开始听还差两步：启动 KWS 引擎、把麦克风采样持续喂进去。
    // 少这两步，「离线唤醒」就只是一个常驻通知。
    try {
      final wake = await _wake.start(now: DateTime.now());
      _wakeDetections ??= _wake.detections.listen(_onWakeDetected);
      if (!wake.signedIn) {
        debugPrint('[WARN] 会话不可用，唤醒监听未接麦克风');
        return;
      }
      await _wakeRunner.start();
    } catch (error) {
      // 缺模型、缺录音权限都在这里如实暴露 —— 不静默装作在听
      if (mounted) {
        _messages.add(ChatMessage(
            role: ChatRole.assistant, text: '离线唤醒未能启动：' + error.toString()));
        setState(() {});
      }
    }
  }

  /// 唤醒词检出后启动业务链路（三、P7 触发时机「唤醒即启动全业务链路」）。
  ///
  /// 顺序不能反：先校验本地会话状态，未登录不起链路。
  Future<void> _onWakeDetected(String keyword) async {
    debugPrint('[INFO] 检出唤醒词：$keyword');
    if (!_signedIn || _listening || !_profile.canVoice) return;
    await _wakeRunner.pause();   // 让出麦克风
    try {
      await _startVoice();
    } finally {
      // 管线必然在 finally 里停掉录音器，所以这里必须重新 start，不能恢复原状态
      await _wakeRunner.resume();
    }
  }

  Future<void> _login(String account, String password) async {
    try {
      final creds = await _api.login(
          account: account, password: password, deviceId: 'phone', now: DateTime.now());
      await _cache.bindAccount(creds.userId);
      final profile = await _fetchProfile(fallback: AccessProfile.member());
      if (!mounted) return;
      setState(() {
        _signedIn = true;
        _profile = profile;
        _showLogin = false;
        _loginError = null;
        _sessionExpired = false;
      });
      // 登录成功后清空访客额度账，避免把试用余额带进正式身份
      _api.adoptGuest(null);
      await _startResidentWake();
    } catch (error) {
      if (mounted) setState(() => _loginError = '登录失败：' + error.toString());
    }
  }

  /// 登出 = 回到访客态，而不是回到登录页（U64 的同一逻辑）。
  Future<void> _signOut() async {
    await _pipeline.interrupt();
    await _wakeRunner.dispose();       // 先松开麦克风，否则下面的 stop 会互相打架
    await _wake.stop();
    await _wakeService.stop();         // 登出就别再占着麦克风常驻了
    await _cache.clearCurrent();       // 登出即清当前账号缓存
    await _session.signOut();
    if (mounted) {
      setState(() {
        _signedIn = false;
        _sessionExpired = false;
      });
    }
    await _enterGuest();
  }

  /// 转成访客模式（登出后 / 成员会话失效后）。
  Future<void> _enterGuest() async {
    _api.adoptGuest(null);
    try {
      final guest = await _api.startGuest(now: DateTime.now(), deviceId: 'phone');
      if (!mounted) return;
      setState(() {
        _profile = AccessProfile.guest(textQueryRemaining: guest.textQueryBudget);
        _showLogin = false;
      });
      _messages.add(ChatMessage(
          role: ChatRole.assistant,
          text: '已切换为访客模式：现在只能文字提问。'));
      setState(() {});
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _profile = AccessProfile.fallback();
        _showLogin = true;
        _loginError = '已登出，但访客模式不可用：$error';
      });
    }
  }

  /// 图片输入（台账 U61）。**成员能力**（U64 的同一张能力表）。
  Future<void> _pickImage() async {
    if (!_profile.canImage) {
      _messages.add(ChatMessage(
          role: ChatRole.assistant, text: kUpgradeHint[Capability.visionInput]!));
      setState(() {});
      return;
    }
    final source = await showModalBottomSheet<ImageSource>(
      context: context,
      builder: (sheet) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(
            leading: const Icon(Icons.photo_camera_outlined),
            title: const Text('拍照'),
            onTap: () => Navigator.pop(sheet, ImageSource.camera),
          ),
          ListTile(
            leading: const Icon(Icons.photo_library_outlined),
            title: const Text('从相册选择'),
            onTap: () => Navigator.pop(sheet, ImageSource.gallery),
          ),
        ]),
      ),
    );
    if (source == null) return;
    try {
      // maxWidth + imageQuality：端上先压一轮，别把手机直出的大图整张推上云
      final picked = await ImagePicker()
          .pickImage(source: source, maxWidth: 1600, imageQuality: 88);
      if (picked == null) return;
      final bytes = await picked.readAsBytes();
      await _sendImage(bytes, source == ImageSource.camera ? '拍照' : '相册');
    } catch (error) {
      if (!mounted) return;
      setState(() => _messages.add(
          ChatMessage(role: ChatRole.assistant, text: '取图失败：$error')));
    }
  }

  /// 上传图片做识别，并把「文字 + 结果图」一起渲染（台账 U61/U62）。
  Future<void> _sendImage(Uint8List bytes, String source) async {
    _messages.add(ChatMessage(
        role: ChatRole.user, text: '（$source）请识别这张图', imageBytes: bytes));
    final reply = ChatMessage(role: ChatRole.assistant, text: '正在识别…');
    _messages.add(reply);
    setState(() {});

    try {
      final result = await _api.recognizeImage(bytes: bytes, now: DateTime.now());
      reply.text = result.text.trim().isEmpty ? '（未识别到文字）' : result.text;
      reply.outputImage = result.card?.bytes;
      // 来源与耗时必须如实标注：用户有权知道这段文字是谁认出来的
      final size = result.facts['width'] == null
          ? ''
          : ' · ${result.facts['width']}x${result.facts['height']}';
      reply.text = '${reply.text}\n\n—— ${result.provider}/${result.model}'
          ' · ${result.elapsedMs}ms$size';
    } on SessionExpiredException {
      reply.sessionExpired = true;
      reply.text = '会话已失效，请重新登录后再传图。';
      if (mounted) setState(() => _sessionExpired = true);
    } on CapabilityRequiredException catch (error) {
      reply.text = error.message;
    } on VisionFailedException catch (error) {
      // 两种失败给完全不同的指引：去配模型 vs 换一张图
      reply.text = error.isUnavailable
          ? '云端暂无可用视觉模型：${error.message}'
          : '这张图没能识别：${error.message}';
    } catch (error) {
      reply.text = '图文识别失败：$error';
    }
    if (mounted) setState(() {});
  }

  /// 文字查询。两条链路按层级分开：
  /// - 成员：走 `VoicePipeline.respond`（含逐句 TTS 播报）
  /// - 访客：直接走 `chatEvents`（服务端本来也不下发 speech）
  ///
  /// 为什么不合并：访客连 TTS 能力都没有，走管线会先撞 `isSignedIn` 的检查，
  /// 拿到的错误会是「未登录」而不是「仅支持文字」—— 提示语是错的。
  Future<void> _sendText(String text) async {
    _messages.add(ChatMessage(role: ChatRole.user, text: text));
    final reply = ChatMessage(role: ChatRole.assistant, text: '');
    _messages.add(reply);
    setState(() {});

    if (_signedIn) {
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
    } else {
      await _streamGuest(text, reply);
    }
    await _cache.put('lastMessage', reply.text);
  }

  /// 访客文字查询：消费 SSE 事件（`plan` / `chunk` / `revise` / `clarify` / `error`）。
  Future<void> _streamGuest(String text, ChatMessage reply) async {
    try {
      await for (final event in _api.chatEvents(utterance: text, now: DateTime.now())) {
        switch (event.type) {
          case ChatEventType.chunk:
            reply.text += event.text;
            break;
          case ChatEventType.revise:
            // 服务端复检后改判：**覆盖**而不是追加，否则会拼出半真半假的答案
            reply.text = event.text;
            break;
          case ChatEventType.clarify:
            reply.text = event.text;
            break;
          case ChatEventType.error:
            reply.text = _describeChatError(event);
            break;
          case ChatEventType.done:
            break;
          default:
            break;
        }
        if (mounted) setState(() {});
      }
    } catch (error) {
      reply.text = '查询失败：$error';
      if (mounted) setState(() {});
    }
  }

  /// 把服务端错误码翻成「用户该做什么」，而不是把 code 直接丢给用户看。
  String _describeChatError(ChatStreamEvent event) {
    switch (event.code) {
      case 'guest_budget_exhausted':
        setState(() => _profile = _profile.copyWith(textQueryRemaining: 0));
        return '访客提问次数已用完。${event.text}';
      case 'capability_required':
        return event.text;
      case 'llm_unavailable':
        return '云端回答模型暂时不可用，请稍后再试。';
      case 'plan_failed':
        return '这句话没能理解：${event.text}';
      default:
        return event.text.isEmpty ? '（无回复）' : event.text;
    }
  }

  /// 语音输入（唤醒后或手动点麦克风）。**必须登录**（U64）。
  Future<void> _startVoice() async {
    if (!_profile.canVoice) {
      _messages.add(ChatMessage(
          role: ChatRole.assistant, text: kUpgradeHint[Capability.voiceInput]!));
      setState(() {});
      return;
    }
    setState(() => _listening = true);
    try {
      await for (final event in _pipeline.runOnce(now: DateTime.now())) {
        switch (event.kind) {
          case PipelineEventKind.partial:
            // 边说边显示：让用户知道「听到了」，而不是对着黑屏说话
            _messages.add(ChatMessage(role: ChatRole.user, text: event.text));
            setState(() {});
            break;
          case PipelineEventKind.voiceNoSpeech:
            _messages.add(ChatMessage(
                role: ChatRole.assistant, text: '没有听到人声，请再说一次。'));
            setState(() {});
            break;
          case PipelineEventKind.sessionExpired:
            setState(() => _sessionExpired = true);
            break;
          case PipelineEventKind.failed:
            _messages.add(ChatMessage(
                role: ChatRole.assistant, text: '语音识别失败：${event.text}'));
            setState(() {});
            break;
          default:
            break;
        }
      }
    } catch (error) {
      // 语音链路哪里断了就如实说哪里断了（缺 VAD 模型 / 缺权限 / 云端不可达）
      _messages.add(
          ChatMessage(role: ChatRole.assistant, text: '语音链路不可用：$error'));
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
    unawaited(_wakeDetections?.cancel() ?? Future<void>.value());
    unawaited(_wakeRunner.dispose()); // 先松开麦克风，再放引擎
    unawaited(_wakeService.stop());   // 停掉常驻唤醒服务
    unawaited(_pipeline.dispose());   // 停掉麦克风 / VAD / ASR / TTS 各自的资源
    unawaited(_wake.stop());          // 停掉唤醒引擎持有的原生会话
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    Widget home;
    if (!_ready) {
      home = const Scaffold(body: Center(child: CircularProgressIndicator()));
    } else if (_showLogin) {
      home = LoginPage(onSubmit: _login, errorText: _loginError);
    } else {
      home = ChatPage(
        messages: _messages,
        onSendText: _sendText,
        // 没有语音能力就不给入口：显示一个点了没反应的麦克风比不显示更糟
        onStartVoice: _profile.canVoice ? _startVoice : null,
        onInterrupt: _pipeline.interrupt,
        onPickImage: _profile.canImage ? _pickImage : null,
        listening: _listening,
        sessionExpired: _sessionExpired,
        onRelogin: _signOut,
        tier: _profile.tier,
        guestRemaining: _profile.textQueryRemaining,
        onLogin: _profile.isGuest ? () => setState(() => _showLogin = true) : null,
        onSignOut: _profile.isMember ? _signOut : null,
      );
    }
    return MaterialApp(
      title: 'AI 管家',
      theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
      home: home,
    );
  }
}
