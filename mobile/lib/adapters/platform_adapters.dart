/// P7 平台适配器（真机实现）。
///
/// 业务规则不在这里 —— 这里只把平台能力接到 ~{aibutler_core}~ 的端口上。
///
/// 完成度声明（**逐项如实标注**）
/// ----------------------------
/// | 端口 | 实现 | 状态 |
/// |---|---|---|
/// | 安全存储 | ~{FlutterSecureStorage}~ | 已实现 |
/// | 本地缓存 | 按账号分目录 | 已实现 |
/// | 定位 | ~{geolocator}~ | 已实现 |
/// | 传输 | HTTP + SSE | 已实现 |
/// | **离线唤醒** | ~{aibutler_voice.WakeWordEngine}~ | **已实现且在 Windows 上 4/4 检出验证** |
/// | VAD / 流式 ASR / TTS | 见各自类 | **需下载对应模型后方可用** |
/// | 麦克风收音 | 见 ~{RecorderMicrophone}~ | **需录音插件（未引入）** |
///
/// 后三项**不做「看起来能跑」的假实现**：构造时就要求给出模型路径，
/// 没给就明确报错并指向对应裁定，而不是静默返回空数据。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:aibutler_core/aibutler_core.dart';
import 'package:aibutler_voice/aibutler_voice.dart' as voice;
import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:geolocator/geolocator.dart';
import 'package:record/record.dart' as rec;

// --------------------------------------------------------------------------- //
// 安全存储（U05：flutter_secure_storage）
// --------------------------------------------------------------------------- //
class FlutterSecureStore implements SecureStorePort {
  FlutterSecureStore([FlutterSecureStorage? storage])
      : _storage = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _storage;

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> delete(String key) => _storage.delete(key: key);

  @override
  Future<void> deleteAll() => _storage.deleteAll();
}

// --------------------------------------------------------------------------- //
// 本地缓存：按账号分目录（三、P7 步骤8）
// --------------------------------------------------------------------------- //
class FileCacheStore implements CacheStorePort {
  FileCacheStore(this.rootDir);

  final Directory rootDir;

  Directory _accountDir(String accountId) =>
      Directory(rootDir.path + Platform.pathSeparator + _safe(accountId));

  static String _safe(String accountId) =>
      accountId.replaceAll(RegExp(r'[^A-Za-z0-9_\-]'), '_');

  @override
  Future<void> put(String accountId, String key, String value) async {
    final dir = _accountDir(accountId);
    await dir.create(recursive: true);
    await File(dir.path + Platform.pathSeparator + _safe(key) + '.json')
        .writeAsString(value);
  }

  @override
  Future<String?> get(String accountId, String key) async {
    final file = File(_accountDir(accountId).path +
        Platform.pathSeparator +
        _safe(key) +
        '.json');
    return await file.exists() ? file.readAsString() : null;
  }

  @override
  Future<void> clearAccount(String accountId) async {
    final dir = _accountDir(accountId);
    if (await dir.exists()) {
      await dir.delete(recursive: true);
    }
  }

  @override
  Future<List<String>> accounts() async {
    if (!await rootDir.exists()) return const [];
    return rootDir
        .listSync()
        .whereType<Directory>()
        .map((d) => d.path.split(Platform.pathSeparator).last)
        .toList();
  }
}

// --------------------------------------------------------------------------- //
// 定位（三、P7 步骤7）
// --------------------------------------------------------------------------- //
class GeolocatorLocation implements LocationPort {
  const GeolocatorLocation();

  /// 权限**按需申请**：只在真的要用定位时才问（进定位页 / 点重新定位），
  /// 不在启动或安装时预申请——那样用户在还没用到功能时就被问，一旦拒绝
  /// 就再也弹不出来。
  @override
  Future<({double latitude, double longitude})?> current() async {
    if (!await Geolocator.isLocationServiceEnabled()) return null;
    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
    }
    if (permission == LocationPermission.denied ||
        permission == LocationPermission.deniedForever) {
      return null;
    }
    final position = await Geolocator.getCurrentPosition(
      locationSettings: const LocationSettings(accuracy: LocationAccuracy.high),
    );
    return (latitude: position.latitude, longitude: position.longitude);
  }

  @override
  Future<LocationAccess> probe() async {
    if (!await Geolocator.isLocationServiceEnabled()) {
      return LocationAccess.serviceOff;
    }
    final permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.always ||
        permission == LocationPermission.whileInUse) {
      return LocationAccess.granted;
    }
    if (permission == LocationPermission.deniedForever) {
      return LocationAccess.deniedForever;
    }
    if (permission == LocationPermission.denied) {
      return LocationAccess.denied;
    }
    return LocationAccess.unavailable;
  }

  @override
  Future<bool> openSystemSettings() => Geolocator.openAppSettings();
}

class BasicDeviceInfo implements DeviceInfoPort {
  const BasicDeviceInfo();

  @override
  Future<int?> batteryLevel() async => null;   // 需 battery_plus，暂不引入

  @override
  Future<String?> networkType() async => null;
}

// --------------------------------------------------------------------------- //
// 传输：HTTP + SSE
// --------------------------------------------------------------------------- //
class HttpTransport implements TransportPort {
  HttpTransport({required this.baseUrl, HttpClient? client})
      : _client = client ?? HttpClient();

  final String baseUrl;
  final HttpClient _client;

  Uri _uri(String path) => Uri.parse(baseUrl + path);

  @override
  Future<TransportResponse> post(String path,
      {required Map<String, dynamic> body, String? token}) async {
    final request = await _client.postUrl(_uri(path));
    request.headers.contentType = ContentType.json;
    if (token != null) {
      request.headers.set(HttpHeaders.authorizationHeader, 'Bearer ' + token);
    }
    request.write(jsonEncode(body));
    final response = await request.close();
    final text = await response.transform(utf8.decoder).join();
    final decoded = text.isEmpty ? <String, dynamic>{} : jsonDecode(text);
    return TransportResponse(
      statusCode: response.statusCode,
      body: decoded is Map<String, dynamic> ? decoded : <String, dynamic>{},
    );
  }

  @override
  Stream<Map<String, dynamic>> stream(String path,
      {required Map<String, dynamic> body, String? token}) async* {
    final request = await _client.postUrl(_uri(path));
    request.headers.contentType = ContentType.json;
    request.headers.set(HttpHeaders.acceptHeader, 'text/event-stream');
    if (token != null) {
      request.headers.set(HttpHeaders.authorizationHeader, 'Bearer ' + token);
    }
    request.write(jsonEncode(body));
    final response = await request.close();
    if (response.statusCode == 401) {
      yield {'status_code': 401};
      return;
    }
    await for (final line
        in response.transform(utf8.decoder).transform(const LineSplitter())) {
      if (!line.startsWith('data:')) continue;
      final payload = line.substring(5).trim();
      if (payload.isEmpty) continue;
      final decoded = jsonDecode(payload);
      if (decoded is Map<String, dynamic>) yield decoded;
    }
  }
}

// --------------------------------------------------------------------------- //
// 离线唤醒（U06）—— **已实现且在 Windows 上验证过**
// --------------------------------------------------------------------------- //
/// 唤醒词模型配置（跟随 U06 的 int8 全套）。
class WakeWordModelPaths {
  const WakeWordModelPaths({
    required this.encoder,
    required this.decoder,
    required this.joiner,
    required this.tokens,
    required this.keywordsFile,
    this.dllDir,
  });

  final String encoder;
  final String decoder;
  final String joiner;
  final String tokens;
  final String keywordsFile;

  /// 桌面验证时需要给出原生库目录；**真机上打包进 APK 后无需指定**。
  final String? dllDir;
}

class SherpaWakeWord implements WakeWordPort {
  SherpaWakeWord({
    this.model,
    this.modelResolver,
    required this.platform,
    this.backgroundWakeVerified = false,
  });

  /// 唤醒词模型；为空表示尚未挂上模型（真机上应随包内置或首次启动时下发）。
  final WakeWordModelPaths? model;

  /// 延迟解析模型路径。
  ///
  /// 随包的模型需要先从 assets 释放到可写目录（见 asset_models.dart），
  /// 那是异步的；而引擎是在 build 期间构造的，等不了。
  /// 所以把「怎么拿到路径」交给它，等真正 start() 时再解析。
  final Future<WakeWordModelPaths?> Function()? modelResolver;

  /// 平台标识（'android' / 'ios'），用于按裁定 U23 分档。
  final String platform;

  /// Android 后台常驻唤醒是否已通过**真机可行性验证**（U23 要求）。
  /// **默认为 false** —— 没验过就不声称能后台常驻。
  final bool backgroundWakeVerified;

  final StreamController<String> _detections =
      StreamController<String>.broadcast();
  voice.WakeWordEngine? _engine;
  bool _running = false;

  @override
  bool get supportsBackgroundWake =>
      platform == 'android' && backgroundWakeVerified;

  @override
  Stream<String> get detections => _detections.stream;

  @override
  Future<void> start({required bool background}) async {
    final paths = model ?? await modelResolver?.call();
    if (paths == null) {
      // 与其他适配器同一条政策：**没有模型就明确报错**，不静默装作在跑。
      throw UnimplementedError(
          '离线唤醒需要先提供唤醒词模型（裁定 U06：sherpa-onnx KWS + '
          'wenetspeech ZipFormer int8）。真机上应随包内置或首次启动时下发；'
          '桌面验证见 packages/aibutler_voice/bin/verify_kws.dart。');
    }
    _engine ??= voice.WakeWordEngine(
      voice.WakeWordEngineConfig(
        encoder: paths.encoder,
        decoder: paths.decoder,
        joiner: paths.joiner,
        tokens: paths.tokens,
        keywordsFile: paths.keywordsFile,
      ),
      dllDir: paths.dllDir,
    );
    _running = true;
  }

  /// 喂入一段音频并派发检出结果。真机上由麦克风回调持续调用。
  void feed(Float32List samples, {int sampleRate = 16000}) {
    if (!_running || _engine == null) return;
    for (final hit in _engine!.detect(samples, sampleRate: sampleRate)) {
      _detections.add(hit.keyword);
    }
  }

  @override
  Future<void> stop() async {
    _running = false;
    _engine?.free();
    _engine = null;
  }

  Future<void> dispose() async {
    await stop();
    await _detections.close();
  }
}

// --------------------------------------------------------------------------- //
// VAD（U22）：**端侧真实运行**，模型随包
// --------------------------------------------------------------------------- //
/// silero-vad 人声检测。
///
/// 状态是「真话」：
/// - 模型没释放出来 → `start()` 明确抛错，不静默返回空流；
/// - 引擎起来了 → `speechActivity` 按**状态变化**发事件（true=开始说、false=说完了），
///   而不是每帧都发（否则下游每 32ms 被叫醒一次，白耗电）。
class SileroVad implements VadPort {
  SileroVad({this.modelPath, this.modelPathResolver});

  /// 模型路径；为空时用 [modelPathResolver] 在 `start()` 时异步解析
  /// （随包模型要先释放到可写目录，构造期等不了异步）。
  final String? modelPath;
  final Future<String> Function()? modelPathResolver;

  final StreamController<bool> _activity = StreamController<bool>.broadcast();
  voice.VadEngine? _engine;
  bool _inSpeech = false;

  /// 本次收音期间累计切出的人声片段数（供自检与排障）。
  int _segments = 0;
  int get segments => _segments;

  @override
  Stream<bool> get speechActivity => _activity.stream;

  @override
  Future<void> start() async {
    final path = modelPath ?? await modelPathResolver?.call();
    if (path == null || path.isEmpty) {
      throw UnimplementedError(
          'VAD 需要 silero_vad.onnx：裁定 U22。随包模型应经 asset_models.dart 释放；'
          '缺模型时不做「假装在听」的实现。');
    }
    _engine?.free();
    _engine = voice.VadEngine(voice.VadEngineConfig(modelPath: path));
    _inSpeech = false;
    _segments = 0;
  }

  @override
  void feed(List<double> samples) {
    final engine = _engine;
    if (engine == null || samples.isEmpty) return;
    final segments = engine.feed(Float32List.fromList(samples));
    _segments += segments.length;
    final speaking = engine.isSpeechDetected;
    if (speaking != _inSpeech) {
      _inSpeech = speaking;
      if (!_activity.isClosed) _activity.add(speaking);
    }
  }

  @override
  Future<void> stop() async {
    final engine = _engine;
    _engine = null;
    _inSpeech = false;
    engine?.free();
  }
}

// --------------------------------------------------------------------------- //
// ASR / TTS（U20 / U21 → U65 裁定：**云端执行**）
// --------------------------------------------------------------------------- //
/// 云端语音识别：录音 → 上传 WAV → 拿文字。
///
/// 为什么不像 VAD 那样端侧推理（U65）
/// --------------------------------
/// 中文流式 ASR 全套 24MB。放进安装包会让 APK 从 49MB 涨到 73MB，
/// 而计划书 P7 的端云分层本来就写明「云端全部智能运算」——
/// 识别属于智能运算，放云端完全合规；真正必须留在端侧的只有「毫秒级打断判断」（VAD）。
///
/// 三个如实标注：
/// 1. **整段上传**：不做端侧切音，静音由云端 VAD 裁掉。代价是多传几十 KB，
///    换来的是端侧不必再维护一套切音逻辑（少一处可能出错的地方）。
/// 2. **有长度上限**：超过 [maxSeconds] 直接拒绝，而不是悄悄截断 ——
///    截断会让用户说的话消失且无从察觉。
/// 3. **失败要能分辨**：模型不可用（503）与音频不合规（400）给不同指引。
class CloudAsr implements AsrPort {
  CloudAsr({
    required ApiClient api,
    required SessionManager session,
    this.sampleRate = 16000,
    this.maxSeconds = 30,
  })  : _api = api,
        _session = session;

  final ApiClient _api;
  final SessionManager _session;
  final int sampleRate;
  final int maxSeconds;

  final StreamController<String> _partial = StreamController<String>.broadcast();
  final List<double> _buffer = [];
  bool _running = false;

  @override
  Stream<String> get partialText => _partial.stream;

  @override
  Future<void> start() async {
    // 语音识别属成员能力（U64）：本地先挡，别让用户对着手机说完一整句才收到 401
    if (!_session.isSignedIn) {
      throw StateError('未登录，无法使用语音识别（U64：语音能力需登录）');
    }
    _buffer.clear();
    _running = true;
  }

  @override
  Future<void> feed(List<double> samples) async {
    if (!_running) return;
    // 到上限就停止累积，而不是无限吃内存（一次对话不该录满 30 秒以上）
    final capacity = maxSeconds * sampleRate;
    if (_buffer.length >= capacity) return;
    _buffer.addAll(samples.take(capacity - _buffer.length));
  }

  @override
  Future<String> finish() async {
    _running = false;
    if (_buffer.isEmpty) return '';
    if (_buffer.length >= maxSeconds * sampleRate) {
      throw StateError('录音超过 $maxSeconds 秒上限，已停止；请分段说话。');
    }
    final wav = encodeWavPcm16(_buffer, sampleRate);
    final result = await _api.transcribe(wav);
    if (!result.isEmpty) {
      if (!_partial.isClosed) _partial.add(result.text);
    }
    return result.text;
  }

  @override
  Future<void> stop() async {
    _running = false;
    _buffer.clear();
  }

  /// 把 [-1,1] 浮点采样编码成 16bit 单声道 WAV。
  ///
  /// 服务端只认这一种：格式一旦不统一，就会变成「同一段音频在两端理解不同」。
  static Uint8List encodeWavPcm16(List<double> samples, int sampleRate) {
    final dataLength = samples.length * 2;
    final bytes = Uint8List(44 + dataLength);
    final view = ByteData.sublistView(bytes);

    void ascii(int offset, String text) {
      for (var i = 0; i < text.length; i++) {
        bytes[offset + i] = text.codeUnitAt(i);
      }
    }

    ascii(0, 'RIFF');
    view.setUint32(4, 36 + dataLength, Endian.little);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    view.setUint32(16, 16, Endian.little);      // fmt 块大小
    view.setUint16(20, 1, Endian.little);       // PCM
    view.setUint16(22, 1, Endian.little);       // 单声道
    view.setUint32(24, sampleRate, Endian.little);
    view.setUint32(28, sampleRate * 2, Endian.little);  // 字节率
    view.setUint16(32, 2, Endian.little);       // 块对齐
    view.setUint16(34, 16, Endian.little);      // 位深
    ascii(36, 'data');
    view.setUint32(40, dataLength, Endian.little);

    for (var i = 0; i < samples.length; i++) {
      final clamped = samples[i].clamp(-1.0, 1.0);
      view.setInt16(44 + i * 2, (clamped * 32767).round(), Endian.little);
    }
    return bytes;
  }
}

/// 云端语音合成 + 端侧播放（U21「云端为主」的落地形态）。
///
/// 为什么不端侧合成：VITS 中文模型 127MB，随包不现实（见 U65）。
/// 为什么不端侧播 raw 采样：Flutter 没有内置的「播 PCM」能力，
/// `audioplayers` 播文件最稳（音频焦点、蓝牙路由都交给它）。
class CloudTts implements TtsPort {
  CloudTts({required ApiClient api, AudioPlayer? player, int? speakerId})
      : _api = api,
        _player = player ?? AudioPlayer(),
        _speakerId = speakerId;

  final ApiClient _api;
  final AudioPlayer _player;
  final int? _speakerId;

  @override
  Future<void> speak(String text) async {
    if (text.trim().isEmpty) return;
    // 语音合成属成员能力（U64）：未登录时明确拒绝，不静默不出声
    if (_speakerId == null && !_api.hasMemberSession) {
      throw StateError('未登录，无法使用语音播报（U64：语音能力需登录）');
    }
    final wav = await _api.synthesize(text, speakerId: _speakerId);
    final dir = await getTemporaryDirectory();
    final file = File(
        dir.path + Platform.pathSeparator + 'tts_${DateTime.now().millisecondsSinceEpoch}.wav');
    await file.writeAsBytes(wav, flush: true);
    await _player.stop();
    await _player.play(DeviceFileSource(file.path));
  }

  @override
  Future<void> stop() async {
    try {
      await _player.stop();
    } catch (_) {
      // 没在播时 stop 也可能抛错 —— 停播是尽力动作，不该影响主流程
    }
  }
}

/// 麦克风收音：16kHz 单声道 PCM16 → [-1, 1] 的浮点采样。
///
/// 采样格式是**跟着唤醒词模型定的**（裁定 U06：wenetspeech ZipFormer 要 16kHz），
/// 不是随手选的 —— 采样率不对，唤醒词引擎拿到的就是错的东西。
class RecorderMicrophone implements MicrophonePort {
  RecorderMicrophone({this.sampleRate = 16000});

  final int sampleRate;
  final rec.AudioRecorder _recorder = rec.AudioRecorder();
  final StreamController<List<double>> _samples =
      StreamController<List<double>>.broadcast();
  StreamSubscription<Uint8List>? _subscription;
  bool _running = false;

  @override
  Stream<List<double>> get samples => _samples.stream;

  /// 是否有麦克风权限。**没有就明确返回 false**，由上层去要权限，
  /// 而不是在这里静默地录不到东西。
  Future<bool> hasPermission() => _recorder.hasPermission();

  @override
  Future<void> start() async {
    if (_running) return;
    if (!await _recorder.hasPermission()) {
      throw StateError('没有麦克风权限：请先在系统设置里授予录音权限（清单已声明 RECORD_AUDIO）。');
    }
    final stream = await _recorder.startStream(rec.RecordConfig(
      encoder: rec.AudioEncoder.pcm16bits,
      sampleRate: sampleRate,
      numChannels: 1,
    ));
    _subscription = stream.listen(_onBytes, onError: _samples.addError);
    _running = true;
  }

  /// PCM16 小端字节 → [-1, 1] 浮点。按**两字节一采样**切，
  /// 落单的半个采样留到下一块（否则波形会整体错位）。
  Uint8List _carry = Uint8List(0);

  void _onBytes(Uint8List chunk) {
    final buffer = Uint8List(_carry.length + chunk.length)
      ..setRange(0, _carry.length, _carry)
      ..setRange(_carry.length, _carry.length + chunk.length, chunk);
    final usable = buffer.length - (buffer.length % 2);
    _carry = Uint8List.fromList(buffer.sublist(usable));

    final view = ByteData.sublistView(buffer, 0, usable);
    final out = Float32List(usable ~/ 2);
    for (var i = 0; i < out.length; i++) {
      out[i] = view.getInt16(i * 2, Endian.little) / 32768.0;
    }
    if (!_samples.isClosed) _samples.add(out);
  }

  @override
  Future<void> stop() async {
    _running = false;
    final sub = _subscription;
    _subscription = null;
    await sub?.cancel();
    await _recorder.stop();
  }

  Future<void> dispose() async {
    await stop();
    await _recorder.dispose();
    await _samples.close();
  }
}

// --------------------------------------------------------------------------- //
// 后台常驻唤醒的前台服务（Android）
// --------------------------------------------------------------------------- //
/// 桥到原生的 [WakeWordForegroundService]（见 android/app/src/main/kotlin/...）。
///
/// **为什么它必须是前台服务**：Android 8+ 限制后台访问麦克风，
/// Android 14+ 还要求前台服务声明类型（microphone）并持有对应权限，
/// 否则 startForeground 直接抛异常。也就是说「揣在兜里也能喊醒」
/// 在 Android 14+ 上只能靠它成立。
///
/// 说明：真机上能否稳定后台唤醒**只有真机可以回答**（裁定 U23）；
/// 这里提供的是必要条件，不是充分条件的证明。
class WakeForegroundService {
  const WakeForegroundService();

  static const MethodChannel _channel =
      MethodChannel('com.aibutler.aibutler_mobile/wake_service');

  /// 仅 Android 有；其它平台直接返回 false。
  bool get supported => Platform.isAndroid;

  Future<bool> start() async {
    if (!supported) return false;
    return await _channel.invokeMethod<bool>('start') ?? false;
  }

  Future<bool> stop() async {
    if (!supported) return false;
    return await _channel.invokeMethod<bool>('stop') ?? false;
  }

  /// 请求通知权限（Android 13+ 必需）。
  ///
  /// 不申请会怎样：前台服务照样能起，但**常驻通知不可见** ——
  /// 用户既看不到"管家在待命"，也没法一键停掉麦克风，属于隐蔽占用。
  /// 返回 true 表示此刻已授予；false 表示尚未授予（系统弹窗是异步的）。
  Future<bool> requestNotificationPermission() async {
    if (!supported) return false;
    return await _channel.invokeMethod<bool>('requestNotification') ?? false;
  }

  /// 常驻通知被用户关掉时，前台服务会被系统降级 —— 引导去开。
  Future<void> openNotificationSettings() async {
    if (!supported) return;
    await _channel.invokeMethod<void>('openNotificationSettings');
  }

  Future<void> openAppSettings() async {
    if (!supported) return;
    await _channel.invokeMethod<void>('openAppSettings');
  }
}
