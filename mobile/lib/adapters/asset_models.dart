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
/// 裁定 U06 要的是**离线**唤醒。如果模型靠下载，那第一次断网就唤不醒，
/// 「离线」二字不成立。KWS 全套 int8 只有 4.82 MB，随包的代价可以接受。
library;

import 'dart:io';

import 'package:flutter/services.dart' show rootBundle;
import 'package:path_provider/path_provider.dart';

import 'platform_adapters.dart' show WakeWordModelPaths;

/// 随包模型在 pubspec 里声明的目录
const String kModelAssetDir = 'assets/models/kws';

/// 唤醒词模型需要的文件（与 assets/models/kws 下的内容一一对应）
const List<String> kWakeModelFiles = [
  'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
  'decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
  'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
  'tokens.txt',
  'keywords.txt',
];

/// 释放模型并返回可用路径。失败时**抛出**，由上层决定怎么提示 ——
/// 不返回半套路径让引擎拿到错的东西。
Future<WakeWordModelPaths> ensureWakeWordModel() async {
  final base = await getApplicationSupportDirectory();
  final dir = Directory(base.path + Platform.pathSeparator + 'wake_model');
  await dir.create(recursive: true);

  for (final name in kWakeModelFiles) {
    final asset = await rootBundle.load(kModelAssetDir + '/' + name);
    final bytes = asset.buffer.asUint8List(asset.offsetInBytes, asset.lengthInBytes);
    final file = File(dir.path + Platform.pathSeparator + name);
    // 已存在且大小一致就跳过：避免每次启动都重写 4.8MB
    if (await file.exists() && await file.length() == bytes.length) {
      continue;
    }
    await file.writeAsBytes(bytes, flush: true);
  }

  String p(String name) => dir.path + Platform.pathSeparator + name;
  return WakeWordModelPaths(
    encoder: p(kWakeModelFiles[0]),
    decoder: p(kWakeModelFiles[1]),
    joiner: p(kWakeModelFiles[2]),
    tokens: p(kWakeModelFiles[3]),
    keywordsFile: p(kWakeModelFiles[4]),
  );
}
