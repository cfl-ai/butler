/// 测试用假端口。业务逻辑只依赖端口，因此全部规则都能在 Dart VM 上验证。
library;

import 'dart:async';

import 'package:aibutler_core/src/ports.dart';

class FakeSecureStore implements SecureStorePort {
  final Map<String, String> values = {};
  int deleteAllCalls = 0;

  @override
  Future<void> write(String key, String value) async => values[key] = value;

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> delete(String key) async => values.remove(key);

  @override
  Future<void> deleteAll() async {
    deleteAllCalls++;
    values.clear();
  }

  /// 全量内容快照，用于断言「某些东西从未落盘」。
  String dump() => values.entries.map((e) => '${e.key}=${e.value}').join('\n');
}

class FakeCacheStore implements CacheStorePort {
  final Map<String, Map<String, String>> data = {};
  final List<String> cleared = [];

  @override
  Future<void> put(String accountId, String key, String value) async =>
      (data[accountId] ??= {})[key] = value;

  @override
  Future<String?> get(String accountId, String key) async => data[accountId]?[key];

  @override
  Future<void> clearAccount(String accountId) async {
    cleared.add(accountId);
    data.remove(accountId);
  }

  @override
  Future<List<String>> accounts() async => data.keys.toList();
}

class FakeLocation implements LocationPort {
  FakeLocation({this.fix = const (latitude: 30.25, longitude: 120.15)});
  final ({double latitude, double longitude})? fix;
  int calls = 0;

  @override
  Future<({double latitude, double longitude})?> current() async {
    calls++;
    return fix;
  }
}

class FakeDeviceInfo implements DeviceInfoPort {
  @override
  Future<int?> batteryLevel() async => 80;

  @override
  Future<String?> networkType() async => 'wifi';
}

class RecordedRequest {
  RecordedRequest(this.path, this.body, this.token);
  final String path;
  final Map<String, dynamic> body;
  final String? token;
}

class FakeTransport implements TransportPort {
  FakeTransport({this.postResponse, this.streamChunks = const [], this.responses});

  TransportResponse? postResponse;
  List<Map<String, dynamic>> streamChunks;

  /// 按路径返回不同响应。账号/模型/定位这些链路要好几个接口串起来，
  /// 只用一个 `postResponse` 就得反复改写字段，既啰嗦又容易测错东西。
  Map<String, TransportResponse>? responses;

  final List<RecordedRequest> posts = [];
  final List<RecordedRequest> streams = [];

  @override
  Future<TransportResponse> post(String path,
      {required Map<String, dynamic> body, String? token}) async {
    posts.add(RecordedRequest(path, body, token));
    final byPath = responses?[path];
    if (byPath != null) return byPath;
    return postResponse ??
        const TransportResponse(statusCode: 200, body: {'ok': true});
  }

  /// 取某个路径最后一次请求（找不到就抛，免得断言静默通过）。
  RecordedRequest lastPost(String path) =>
      posts.lastWhere((r) => r.path == path,
          orElse: () => throw StateError('没有向 $path 发过请求'));

  /// 设为 true 后，流由测试用 push/close 驱动——
  /// 打断这类**时序敏感**的用例必须能控制「什么时候来下一片」，
  /// 否则假传输一次性吐完，VAD 信号永远追不上。
  bool manual = false;
  StreamController<Map<String, dynamic>>? _controller;

  void push(Map<String, dynamic> chunk) => _controller?.add(chunk);
  Future<void> closeStream() async => _controller?.close();

  @override
  Stream<Map<String, dynamic>> stream(String path,
      {required Map<String, dynamic> body, String? token}) {
    streams.add(RecordedRequest(path, body, token));
    if (manual) {
      _controller ??= StreamController<Map<String, dynamic>>();
      return _controller!.stream;
    }
    return Stream<Map<String, dynamic>>.fromIterable(streamChunks);
  }
}

class FakeWakeWord implements WakeWordPort {
  FakeWakeWord({required this.supportsBackgroundWake});

  @override
  final bool supportsBackgroundWake;
  final List<bool> startCalls = [];
  int stopCalls = 0;
  final StreamController<String> _detections = StreamController<String>.broadcast();

  @override
  Future<void> start({required bool background}) async => startCalls.add(background);

  @override
  Future<void> stop() async => stopCalls++;

  @override
  Stream<String> get detections => _detections.stream;

  void emit(String word) => _detections.add(word);
}

class FakeVad implements VadPort {
  final StreamController<bool> _activity = StreamController<bool>.broadcast();
  int startCalls = 0;
  int stopCalls = 0;

  /// 收到的采样总数：验证「管线真的把麦克风采样喂给了 VAD」。
  int fedSamples = 0;

  @override
  Stream<bool> get speechActivity => _activity.stream;

  @override
  Future<void> start() async => startCalls++;

  @override
  void feed(List<double> samples) => fedSamples += samples.length;

  @override
  Future<void> stop() async => stopCalls++;

  /// 测试驱动：报出「有人声 / 人声结束」。
  void emit(bool active) => _activity.add(active);
}

class FakeAsr implements AsrPort {
  final StreamController<String> _partials = StreamController<String>.broadcast();
  final List<double> fed = [];
  int startCalls = 0;
  int stopCalls = 0;
  int finishCalls = 0;

  /// 终态文本。**过程文本可能还在被改写，终态才是管线该信的那个**（见 AsrPort 契约）。
  String finalText = '';

  @override
  Stream<String> get partialText => _partials.stream;

  @override
  Future<void> start() async => startCalls++;

  @override
  Future<void> feed(List<double> samples) async => fed.addAll(samples);

  @override
  Future<String> finish() async {
    finishCalls++;
    return finalText;
  }

  @override
  Future<void> stop() async => stopCalls++;

  /// 测试驱动：同时更新过程文本与终态文本（真机上由模型在收尾时定稿）。
  void emit(String text) {
    finalText = text;
    _partials.add(text);
  }
}

class FakeTts implements TtsPort {
  final List<String> spoken = [];
  int stopCalls = 0;
  Completer<void>? gate;

  @override
  Future<void> speak(String text) async {
    spoken.add(text);
    if (gate != null) await gate!.future;
  }

  @override
  Future<void> stop() async => stopCalls++;
}

class FakeMicrophone implements MicrophonePort {
  final StreamController<List<double>> _samples =
      StreamController<List<double>>.broadcast();
  int startCalls = 0;
  int stopCalls = 0;

  @override
  Stream<List<double>> get samples => _samples.stream;

  @override
  Future<void> start() async => startCalls++;

  @override
  Future<void> stop() async => stopCalls++;

  void emit(List<double> s) => _samples.add(s);
}

class SilentLogger implements LoggerPort {
  final List<String> lines = [];

  @override
  void info(String message) => lines.add('INFO $message');

  @override
  void warn(String message) => lines.add('WARN $message');

  @override
  void error(String message, [Object? error]) => lines.add('ERROR $message');
}
