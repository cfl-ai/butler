/// 唤醒词引擎的真实验证（在 Windows 上跑 sherpa-onnx 的 Dart 绑定）。
///
/// 为什么要有它
/// ------------
/// 手机上的适配器不能只靠「看起来对」。这份入口让**同一份引擎代码**
/// 在桌面上跑真实推理，把真机要回答的问题缩小到「权限、保活、功耗」。
///
/// 关键前提：**必须先按绝对路径预加载 onnxruntime.dll**
/// ------------------------------------------------
/// Windows 解析 DLL 依赖按「exe 目录 → **System32** → PATH」找，而 System32 里
/// 躺着系统自带的 onnxruntime 1.10；它抢先生效会让库拿到 ORT API 10 而需要 28，
/// 于是报 ~{version [28] is not supported~}~ 并崩溃。见 MEMORY E-13。
/// 引擎内部已处理（~{preloadNativeLibraries~}~），这里只需给出 dll 目录。
///
/// 关键词从哪来
/// ------------
/// 模型仓（modelscope）里的 ~{test_wavs/test_keywords.txt~}~ **与同目录音频并不对应** ——
/// 用同一套模型转写 0.wav 得到的是「对我做了介绍么我想说的是大家如果对我点酒感兴趣」。
/// 因此这里的关键词是**从转写结果里取的真实短语**（Python 侧已验证 4/4 检出）。
///
/// 用法：
///   set SHERPA_DLL_DIR=<sherpa_onnx_windows 的 windows 目录>
///   dart run bin/verify_kws.dart <模型目录>
library;

import 'dart:io';

import 'package:aibutler_voice/aibutler_voice.dart';

/// 从测试音频转写结果里取出的、确实存在的短语。
const List<(String, String)> verifiedKeywords = [
  ('介绍', 'j iè sh ào'),
  ('感兴趣', 'g ǎn x ìng q ù'),
  ('我想说的是', 'w ǒ x iǎng sh uō d e sh ì'),
  ('大家', 'd à j iā'),
];

/// U06 的自定义唤醒词：每个音节拆成「声母 + 韵母」两个 token。
const String wakeWordLine = ' n ǐ h ǎo g u ǎn j i ā @你好管家';

/// 四条里至少命中这么多才算通过（留一条余量：边界切分本就可能漏）。
const int minHits = 3;

int _passed = 0;
int _failed = 0;

void check(String label, bool ok, String detail) {
  final tag = ok ? 'PASS' : 'FAIL';
  if (ok) {
    _passed++;
  } else {
    _failed++;
  }
  print('   [$tag] $label：$detail');
}

void main(List<String> args) {
  final modelDir = args.isNotEmpty
      ? args.first
      : '../../../../models/kws-zipformer-wenetspeech-3.3M';
  final dir = Directory(modelDir);
  if (!dir.existsSync()) {
    stderr.writeln('模型目录不存在：${dir.absolute.path}');
    exit(2);
  }
  final base = dir.path;
  final dllDir = Platform.environment['SHERPA_DLL_DIR'];

  print('=' * 68);
  print('P7 Dart 侧唤醒词引擎验证（真机之前的最后一层证据）');
  print('=' * 68);

  WakeWordEngineConfig configFor(String keywordsFile) => WakeWordEngineConfig(
        encoder: '$base/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
        decoder: '$base/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
        joiner: '$base/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
        tokens: '$base/tokens.txt',
        keywordsFile: keywordsFile,
      );

  // ---- 1) 原生绑定 ----
  print('[1/3] 原生绑定');
  try {
    final probe = WakeWordEngine(configFor('$base/keywords.txt'), dllDir: dllDir);
    probe.free();
    check('绑定初始化', true, '预加载 onnxruntime 后，引擎构建成功');
  } catch (error) {
    check('绑定初始化', false, error.toString());
    exit(1);
  }

  // ---- 2) 自定义唤醒词的 token 合法性 ----
  print('[2/3] 自定义唤醒词 token 校验');
  final tokens = File('$base/tokens.txt')
      .readAsLinesSync()
      .where((l) => l.trim().isNotEmpty)
      .map((l) => l.trim().split(RegExp(r'\s+')).first)
      .toSet();
  final wakeTokens =
      wakeWordLine.split('@').first.trim().split(RegExp(r'\s+'));
  final missing = wakeTokens.where((t) => !tokens.contains(t)).toList();
  check(
      '你好管家 token',
      missing.isEmpty,
      '${wakeTokens.length} 个 token（声母+韵母），'
      '缺失=${missing.isEmpty ? '无' : missing.join(',')}');

  // ---- 3) 真实检出 ----
  print('[3/3] 真实推理');
  final wavs = dir
      .listSync()
      .whereType<Directory>()
      .where((d) => d.path.endsWith('test_wavs'))
      .expand((d) => d.listSync().whereType<File>())
      .where((f) => f.path.endsWith('.wav'))
      .toList()
    ..sort((a, b) => a.path.compareTo(b.path));
  if (wavs.isEmpty) {
    check('测试音频', false, 'test_wavs 下没有 wav');
    exit(1);
  }
  final wav = wavs.first;

  final reader = WakeWordEngine(configFor('$base/keywords.txt'), dllDir: dllDir);
  final wave = reader.readWave(wav.path);
  reader.free();
  check('读取音频', wave.samples.isNotEmpty,
      '${wav.uri.pathSegments.last}：${wave.samples.length} 采样 @${wave.sampleRate}Hz');

  var hits = 0;
  for (final (label, pinyin) in verifiedKeywords) {
    final keywordFile = File('$base/_dart_verify_keywords.txt');
    keywordFile.writeAsStringSync(' $pinyin @$label\n');
    final engine =
        WakeWordEngine(configFor(keywordFile.path), dllDir: dllDir);
    try {
      final found = engine.detect(wave.samples, sampleRate: wave.sampleRate);
      if (found.isNotEmpty) {
        hits++;
        print('   检出「$label」（$pinyin）');
      } else {
        print('   未检出「$label」（$pinyin）');
      }
    } finally {
      engine.free();
    }
  }
  check(
      '唤醒检出',
      hits >= minHits,
      '${verifiedKeywords.length} 条关键词检出 ${hits} 条'
      '（阈值 ${minHits}）');

  print('=' * 68);
  print('结论：PASS ${_passed} / FAIL ${_failed}');
  print('=' * 68);
  exit(_failed == 0 ? 0 : 1);
}
