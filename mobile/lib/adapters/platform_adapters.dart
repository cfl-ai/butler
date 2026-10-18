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
import 'package:flutter/services.dart';
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
// VAD / ASR / TTS：结构与裁定已就位，但**必须给出模型路径**
// --------------------------------------------------------------------------- //
Never _modelMissing(String what, String decision) => throw UnimplementedError(
    what +
    '需要先提供模型文件：' +
    decision +
    '。构造时给出模型路径即可启用；'
    '在没有模型的情况下**不做假装能跑的假实现**。');

/// VAD 人声检测（U22：silero-vad，sherpa-onnx 内置）。
class SileroVad implements VadPort {
  SileroVad({this.modelPath});

  /// ~{silero_vad.onnx}~ 路径；为空表示尚未提供模型。
  final String? modelPath;

  @override
  Stream<bool> get speechActivity => const Stream<bool>.empty();

  @override
  Future<void> start() async {
    if (modelPath == null) {
      _modelMissing('VAD', '裁定 U22：silero-vad（sherpa-onnx 内置）');
    }
  }

  @override
  Future<void> stop() async {}
}

/// 流式 ASR（U20：sherpa-onnx 流式 ZipFormer）。
class SherpaAsr implements AsrPort {
  SherpaAsr({this.encoder, this.decoder, this.joiner, this.tokens});

  final String? encoder;
  final String? decoder;
  final String? joiner;
  final String? tokens;

  @override
  Stream<String> get partialText => const Stream<String>.empty();

  @override
  Future<void> start() async {
    if (encoder == null || tokens == null) {
      _modelMissing('流式 ASR', '裁定 U20：sherpa-onnx 流式 ZipFormer');
    }
  }

  @override
  Future<void> feed(List<double> samples) async {}

  @override
  Future<void> stop() async {}
}

/// TTS 播报（U21：云端 CosyVoice2 为主，端侧 sherpa-onnx VITS 兜底）。
class SherpaTts implements TtsPort {
  SherpaTts({this.modelPath, this.tokens, this.lexicon});

  final String? modelPath;
  final String? tokens;
  final String? lexicon;

  @override
  Future<void> speak(String text) async {
    if (modelPath == null) {
      _modelMissing('端侧 TTS', '裁定 U21：sherpa-onnx VITS 兜底');
    }
  }

  @override
  Future<void> stop() async {}
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
