/// P7 移动端纯 Dart 核心：会话、缓存、云端对接、语音编排、唤醒策略、设备上报。
///
/// 这里承载全部 P7 业务规则，**不依赖 Flutter**，因此可以用 dart test 直接验证。
library;

export 'src/access_policy.dart';
export 'src/api_client.dart';
export 'src/local_cache.dart';
export 'src/models.dart';
export 'src/ports.dart';
export 'src/reporter.dart';
export 'src/session.dart';
export 'src/voice_pipeline.dart';
export 'src/wake_word.dart';
