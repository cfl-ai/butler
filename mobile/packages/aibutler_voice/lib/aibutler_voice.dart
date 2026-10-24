/// P7 端侧语音引擎绑定（sherpa-onnx）。
///
/// 裁定 U06（离线唤醒）/ U20（流式 ASR）/ U21（端侧 TTS 兜底）/ U22（VAD）。
/// 与 ~{aibutler_core}~ 的分工：core 放**业务规则**（不依赖任何原生库），
/// 本包放**引擎绑定**（依赖 sherpa-onnx）。
library;

export 'src/keyword_spotter_engine.dart';
export 'src/vad_engine.dart';
