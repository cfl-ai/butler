/// 把随包的端侧模型释放到可写目录。
///
/// 为什么需要这一步
/// ----------------
/// sherpa-onnx 要的是**文件路径**，而 assets 在 APK 里是压缩资源，
/// 不能直接当路径用。所以首次启动时把模型写进应用私有目录，
/// 之后按「文件存在且大小一致」判断，不重复写。
///
/// 为什么随包而不是首次启动时下载
/// ------------------------------
/// 裁定 U06 要的是**离线**唤醒、U22 要的是**毫秒级**打断判断。
/// 如果模型靠下载，那第一次断网就唤不醒、也判断不了有没有人说话，
/// 「离线」二字不成立。KWS 4.82MB + VAD 1.7MB 的随包代价可以接受。
///
/// 为什么不把 ASR / TTS 也随包（U65）
/// --------------------------------
/// 中文流式 ASR 全套 24MB、VITS 中文 127MB。后者会把安装包直接推到 175MB，
/// 而计划书 P7 的端云分层本来就写明「云端全部智能运算」。
/// 因此 **ASR / TTS 走云端**（服务端已跑通真实推理），端侧只保留唤醒与 VAD。
/// 这属于 U65 的裁定：端侧 ASR/TTS 为**可选能力**，模型不在包里时不得假装可用。
library;

import 'dart:io';

import 'package:flutter/services.dart' show rootBundle;
import 'package:path_provider/path_provider.dart';

import 'platform_adapters.dart' show WakeWordModelPaths;

/// 唤醒词模型在 pubspec 里声明的目录
const String kModelAssetDir = 'assets/models/kws';

/// VAD 模型目录（U22）
const String kVadAssetDir = 'assets/models/vad';

/// VAD 模型文件名
const String kVadModelFile = 'silero_vad.onnx';

/// 唤醒词模型需要的文件（与 assets/models/kws 下的内容一一对应）
const List<String> kWakeModelFiles = [
  'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
  'decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
  'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
  'tokens.txt',
  'keywords.txt',
];

/// 把一组 asset 释放到 [subdir] 并返回目录。
///
/// 幂等：已存在且大小一致就跳过，避免每次启动重写几十 MB。
/// 失败时**抛出** —— 不返回半套路径让引擎拿到错的东西。
Future<Directory> _materialize({
  required String assetDir,
  required List<String> files,
  required String subdir,
}) async {
  final base = await getApplicationSupportDirectory();
  final dir = Directory(base.path + Platform.pathSeparator + subdir);
  await dir.create(recursive: true);

  for (final name in files) {
    final asset = await rootBundle.load('$assetDir/$name');
    final bytes = asset.buffer.asUint8List(asset.offsetInBytes, asset.lengthInBytes);
    final file = File(dir.path + Platform.pathSeparator + name);
    if (await file.exists() && await file.length() == bytes.length) {
      continue;
    }
    await file.writeAsBytes(bytes, flush: true);
  }
  return dir;
}

/// 释放唤醒词模型并返回可用路径。
Future<WakeWordModelPaths> ensureWakeModel() async {
  final dir = await _materialize(
    assetDir: kModelAssetDir, files: kWakeModelFiles, subdir: 'wake_model');

  String p(String name) => dir.path + Platform.pathSeparator + name;
  return WakeWordModelPaths(
    encoder: p(kWakeModelFiles[0]),
    decoder: p(kWakeModelFiles[1]),
    joiner: p(kWakeModelFiles[2]),
    tokens: p(kWakeModelFiles[3]),
    keywordsFile: p(kWakeModelFiles[4]),
  );
}

/// 释放 VAD 模型并返回其路径（U22）。
///
/// 与唤醒模型同一个释放机制，但**单独一个目录**：两者生命周期不同
/// （VAD 每次收音都要用，唤醒是一旦启动就常驻），混在一起会让
/// 「只缺 VAD」的情况变得看不出来。
Future<String> ensureVadModel() async {
  final dir = await _materialize(
    assetDir: kVadAssetDir, files: const [kVadModelFile], subdir: 'vad_model');
  return dir.path + Platform.pathSeparator + kVadModelFile;
}

/// 旧名字保留（既有调用点与文档引用它）。
@Deprecated('改用 ensureWakeModel()：它现在同时负责唤醒与 VAD 的释放')
Future<WakeWordModelPaths> ensureWakeWordModel() => ensureWakeModel();
