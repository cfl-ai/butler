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
import 'ui/account_page.dart';
import 'ui/chat_page.dart';
import 'ui/location_page.dart';
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

  /// 定位端口（U71）。**与设备上报共用同一个实现**：
  /// 两处各建一个没意义，但权限申请必须走同一个入口，
  /// 否则会出现「一处弹授权、另一处还以为没权限」。
  late final GeolocatorLocation _location = const GeolocatorLocation();

  final List<ChatMessage> _messages = [];

  /// 当前身份的能力档案。**以服务端为准**，本地矩阵只是兜底。
  AccessProfile _profile = AccessProfile.fallback();

  /// 登录后的账号资料（脱敏）。访客态为 null，设置页与后续界面都读它。
  AccountProfile? _account;

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
        location: _location,
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
      // 昵称单独拉一次：拉不到就不显示，别拿「AI 管家」冒充用户的昵称
      try {
        _account = await _api.accountProfile();
      } catch (error) {
        debugPrint('[WARN] 账号资料获取失败，标题不显示昵称：$error');
      }
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

  /// 登录（手机号 / 邮箱，U67）。成功后服务端签发成员会话。
  ///
  /// 与旧的 `_login(account, password)` 的区别：**多带一个 `kind`**。
  /// 服务端的账号表按「标识类型 + 摘要」定位，不带 kind 只能退回旧账号表。
  Future<void> _loginWithKind(
      IdentKind kind, String identifier, String password) async {
    final account = await _api.loginWithIdentifier(
      kind: kind.wire,
      identifier: identifier,
      password: password,
      now: DateTime.now(),
      deviceId: 'phone',
    );
    await _enterMember(account);
  }

  /// 注册（U67）。`nickname` 为 null 时由**服务端**生成随机昵称。
  ///
  /// 端侧不自己拼昵称：词表与校验规则只能有一处（U69）。
  Future<void> _register(
    IdentKind kind,
    String identifier,
    String password,
    String? nickname,
  ) async {
    final account = await _api.register(
      kind: kind.wire,
      identifier: identifier,
      password: password,
      now: DateTime.now(),
      deviceId: 'phone',
      nickname: nickname,
    );
    await _enterMember(account);
  }

  /// 申请重置（U68）。返回值**只在开发通道**才有令牌，生产通道为 null。
  ///
  /// 这里不做「已发送」的假承诺：拿不到令牌就返回 null，
  /// 由登录页显示「如果该账号已注册，重置方式已发送」。
  Future<String?> _forgot(IdentKind kind, String identifier) async {
    final result = await _api.requestPasswordReset(
        kind: kind.wire, identifier: identifier);
    return result.devToken;
  }

  /// 用一次性令牌设置新密码（U68）。
  ///
  /// 服务端改密后会**吊销该账号全部会话**，本地 token 也已被 `resetPassword`
  /// 清掉，所以这里必须同步拆掉成员态 —— 否则界面还显示着成员能力，
  /// 下一次请求才报「未登录」，用户会以为是刚才那步失败了。
  Future<void> _reset(String token, String newPassword) async {
    await _api.resetPassword(token: token, newPassword: newPassword);
    await _teardownMember();
    if (!mounted) return;
    setState(() {
      _showLogin = true;
      _loginError = null;
    });
  }

  /// 会话建立后的共同收尾：绑缓存 → 拉能力 → 进主界面 → 拉起唤醒。
  Future<void> _enterMember(AccountProfile account) async {
    await _cache.bindAccount(account.userId);
    final profile = await _fetchProfile(fallback: AccessProfile.member());
    if (!mounted) return;
    setState(() {
      _signedIn = true;
      _account = account;
      _profile = profile;
      _showLogin = false;
      _loginError = null;
      _sessionExpired = false;
    });
    // 登录成功后清空访客额度账，避免把试用余额带进正式身份
    _api.adoptGuest(null);
    await _startResidentWake();
  }

  /// 登出 = 回到访客态，而不是回到登录页（U64 的同一逻辑）。
  Future<void> _signOut() async {
    await _teardownMember();
    await _enterGuest();
  }

  /// 拆掉成员态：停语音链路 → 松麦克风 → 清缓存 → 清本地会话。
  ///
  /// 顺序不能反：`interrupt` 之后管线才会放开录音器，
  /// 这时再去 `_wakeRunner.dispose()` 才不会和管线抢同一个麦克风。
  ///
  /// **不碰 `_showLogin`**：登出走 `_enterGuest`（回访客），
  /// 改密走登录页（必须重新输入新密码），两者出口不同。
  Future<void> _teardownMember() async {
    await _pipeline.interrupt();
    await _wakeRunner.dispose();       // 先松开麦克风，否则下面的 stop 会互相打架
    await _wake.stop();
    await _wakeService.stop();         // 登出就别再占着麦克风常驻了
    await _cache.clearCurrent();       // 登出即清当前账号缓存
    // 会话标识属于「上一个人的对话」，不能留给下一个账号复用。
    // 服务端本来就会因归属不符重新签发（兜底），但端侧先清掉能少一次无谓的重签，
    // 也更符合直觉：登出之后不该还攥着上一段对话的把手。
    _api.startNewConversation();
    await _session.signOut();
    if (mounted) {
      setState(() {
        _signedIn = false;
        _sessionExpired = false;
        _account = null;
      });
    }
  }

  /// 会话失效对话框里的「重新登录」：拆成员态并**直接停在登录页**。
  ///
  /// 不复用 `_signOut`：登出的语义是「我要变访客」，而这里是「凭据过期了，
  /// 让我重新登」。混用会让点了「重新登录」的人落到访客对话页 ——
  /// 他刚被明确告知要重新登录，结果却是访客模式，只会再点一次。
  Future<void> _relogin() async {
    await _teardownMember();
    if (!mounted) return;
    setState(() {
      _showLogin = true;
      _loginError = '会话已失效，请重新登录';
    });
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

  /// 登录页「先不登录」：能回访客态就回，回不去就由 `_enterGuest` 如实说明。
  ///
  /// 没有这个出口，访客点了「登录」就再也回不到文字查询 ——
  /// 登录页在 login 模式下本来没有返回箭头。
  Future<void> _skipLogin() async {
    if (_profile.isGuest) {
      setState(() => _showLogin = false);
      return;
    }
    await _enterGuest();
  }

  /// 把一句系统提示塞进对话流。**不用 SnackBar**：
  /// 本 State 的 context 在 MaterialApp 之上，拿不到 ScaffoldMessenger；
  /// 而对话流本来就是用户已经在看的地方。
  void _announce(String text) {
    if (!mounted) return;
    setState(() =>
        _messages.add(ChatMessage(role: ChatRole.assistant, text: text)));
  }

  /// 打开「账号与模型」页（U67 / U69 / U70）。
  ///
  /// 资料与模型列表**现拉一次**再进页，不复用登录时那一份：
  /// 用户可能刚在另一台设备改了昵称或换了模型，给他看旧值等于骗他。
  Future<void> _openAccount() async {
    if (!_profile.canEditProfile) {
      _announce(kUpgradeHint[Capability.profileEdit] ?? '当前身份不能修改账号设置');
      return;
    }
    try {
      final account = await _api.accountProfile();
      final models = await _api.models();
      if (!mounted) return;
      setState(() => _account = account);

      await Navigator.of(context).push<void>(MaterialPageRoute(
        builder: (route) => AccountPage(
          profile: account,
          models: models,
          onSetNickname: _api.setNickname,
          onRefreshNickname: _api.refreshNickname,
          onSelectModel: (modelKey) async {
            await _api.selectModel(modelKey);
          },
          onBindByok: (baseUrl, model, apiKey, name) async {
            await _api.bindByokModel(
                baseUrl: baseUrl, model: model, apiKey: apiKey, displayName: name);
          },
          onRemoveModel: () async {
            await _api.removeModelBinding();
          },
          onSetPrefs: _api.setPrefs,
          // 改密成功后服务端已吊销全部会话，本机必须同步降级：
          // 先关掉设置页，再拆成员态并回登录页（登出是回访客，改密是必须重登，
          // 两者出口不同，不能图省事复用 `_signOut`）。
          onChangePassword: (oldPassword, newPassword) async {
            final revoked = await _api.changePassword(
                oldPassword: oldPassword, newPassword: newPassword);
            await _teardownMember();
            if (mounted && route.mounted) Navigator.of(route).pop();
            if (!mounted) return revoked;
            setState(() {
              _showLogin = true;
              _loginError =
                  '密码已更新，请用新密码重新登录（已吊销 $revoked 个会话）';
            });
            return revoked;
          },
          onSignOut: () async {
            if (mounted && route.mounted) Navigator.of(route).pop();
            await _signOut();
          },
        ),
      ));
      // 回到对话页后再拉一次资料：用户可能在设置页里改了昵称，
      // 标题还挂着旧名字会让人以为「改了没生效」。
      // 改密/登出后 `_signedIn` 已为 false，这条路径自然跳过。
      if (!mounted || !_signedIn) return;
      try {
        final latest = await _api.accountProfile();
        if (mounted) setState(() => _account = latest);
      } catch (error) {
        debugPrint('[WARN] 账号资料刷新失败：$error');
      }
    } on CapabilityRequiredException catch (error) {
      _announce(error.message);
    } on SessionExpiredException {
      if (mounted) setState(() => _sessionExpired = true);
    } catch (error) {
      _announce('账号设置打不开：$error');
    }
  }

  /// 打开「附近与推荐」页（U71）。
  ///
  /// 坐标**由 `GeolocatorLocation` 现场取**，上报前经过 `ApiClient.recommend`
  /// 与服务端两道粗化 —— 精确坐标不出设备。
  Future<void> _openLocation() async {
    if (!_profile.canLocate) {
      _announce(kUpgradeHint[Capability.locationShare] ?? '当前身份不能使用定位与推荐');
      return;
    }
    await Navigator.of(context).push<void>(MaterialPageRoute(
      builder: (_) => LocationPage(
        onLocate: _location.current,
        onProbe: _location.probe,
        onOpenSettings: _location.openSystemSettings,
        onRecommend: (lat, lon, prefs) =>
            _api.recommend(latitude: lat, longitude: lon, prefs: prefs),
        onNavigation: (item) => _api.navigationLinks(
          destLat: item.lat!,
          destLon: item.lon!,
          destName: item.name,
        ),
      ),
    ));
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
      home = LoginPage(
        callbacks: AuthCallbacks(
          onLogin: _loginWithKind,
          onRegister: _register,
          onForgot: _forgot,
          onReset: _reset,
          // 昵称建议走服务端公开接口：词表与校验规则只能有一处（U69）
          onSuggestNickname: _api.suggestNickname,
        ),
        errorText: _loginError,
        // 访客点进来的登录页必须能退回去，否则「试用」变成单向门
        onSkip: _profile.isGuest && !_signedIn ? _skipLogin : null,
      );
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
        onRelogin: _relogin,
        tier: _profile.tier,
        guestRemaining: _profile.textQueryRemaining,
        onLogin: _profile.isGuest ? () => setState(() => _showLogin = true) : null,
        onSignOut: _profile.isMember ? _signOut : null,
        // 账号/定位两个入口同样按能力渲染（U67 / U71）
        onOpenAccount: _profile.canEditProfile ? _openAccount : null,
        onOpenLocation: _profile.canLocate ? _openLocation : null,
        nickname: _account?.nickname,
      );
    }
    return MaterialApp(
      title: 'AI 管家',
      theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
      home: home,
    );
  }
}
