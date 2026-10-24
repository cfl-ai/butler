"""P7 语音链路服务端实现：VAD 分段 → ASR 识别 → TTS 合成 → 声纹校验。

计划书依据
----------
- 一、项目概述「移动端（仅感知 + 交互）… **云端（全部智能运算）**」
- 三、P7 步骤4「开发 **VAD 人声检测、实时收音、随时打断** 机制」
- 三、P7 步骤5「对接云端流式接口：**逐字文字渲染 + 同步 TTS 播报**」
- 三、P7 步骤2「集成后台常驻离线语音唤醒」

裁定
----
| 项 | 裁定 | 说明 |
|---|---|---|
| VAD | **U22** silero-vad | 端侧可跑（1.7MB），云端同样跑一遍做二次切分 |
| ASR | **U20** sherpa-onnx ZipFormer | 端侧缺模型时**由云端识别**，不是「假装没听见」 |
| TTS | **U21** 端侧 VITS 兜底、云端为主 | 云端用 VITS-zh 多说话人；端侧有模型则本地播 |
| 声纹 | **U63（人工授权，本文件新增）** | 计划书未定义；3D-Speaker CAM++ embedding |

为什么识别放云端也算合规
------------------------
计划书写的是「移动端仅感知 + 交互，云端全部智能运算」——**识别属于智能运算**。
U20 让端侧也具备流式识别能力（离线可用），但**不是唯一路径**：
端侧模型缺失时（例如精简安装包），云端这条路必须能接住，否则语音链路整体不可用。
两条路共用同一套 `AsrResult` 契约，上层无需知道语音是在哪一端变成文字的。

三条不变量（本模块的守护点）
---------------------------
1. **fail-closed**：模型没落地 → `SpeechUnavailable`，带「缺哪个文件、跑哪个脚本」；
   绝不返回空字符串冒充「识别结果为空」。
2. **音频边界**：超过 `speech_max_audio_seconds` 直接拒绝，不做「悄悄截断」——
   截断会让用户说的话消失且无从察觉。
3. **声纹分数量化**：任何声纹结论都必须带分数与阈值；取不到分数即判不匹配
   （`core/access.judge_voiceprint` 的同一红线）。
"""

from __future__ import annotations

import array
import io
import json
import math
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

#: sherpa-onnx 的采样率口径：KWS / VAD / ZipFormer / CAM++ 全部按 16k 训练。
TARGET_SAMPLE_RATE = 16000
#: VAD 窗口。silero-vad v4/v5 固定 512 采样（32ms @16k），改这个数会直接报错。
VAD_WINDOW_SIZE = 512


class SpeechError(RuntimeError):
    """语音链路错误基类。"""


class SpeechUnavailable(SpeechError):
    """组件缺少模型或依赖 —— **不降级为「看起来能跑」**。"""

    code = "speech_unavailable"

    def __init__(self, component: str, reason: str) -> None:
        self.component = component
        self.reason = reason
        super().__init__(f"{component} 不可用：{reason}")

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "component": self.component, "message": self.reason}


class AudioRejected(SpeechError):
    """音频本身不合规（格式/时长/静音）。**与「服务不可用」严格区分**。"""

    code = "audio_rejected"


# --------------------------------------------------------------------------- #
# 数据契约
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpeechSegment:
    """一段人声。时间单位是秒，相对本次上传音频的起点。"""

    start: float
    end: float
    samples: tuple[float, ...]

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class AsrResult:
    text: str
    segments: tuple[SpeechSegment, ...]
    audio_seconds: float
    speech_seconds: float
    elapsed_ms: int
    model: str

    def as_dict(self, *, with_samples: bool = False) -> dict[str, Any]:
        return {
            "text": self.text,
            "audio_seconds": round(self.audio_seconds, 3),
            "speech_seconds": round(self.speech_seconds, 3),
            "elapsed_ms": self.elapsed_ms,
            "model": self.model,
            "segments": [
                {"start": round(s.start, 3), "end": round(s.end, 3),
                 **({"samples": list(s.samples)} if with_samples else {})}
                for s in self.segments
            ],
        }


@dataclass(frozen=True)
class TtsResult:
    wav: bytes
    sample_rate: int
    duration: float
    speaker_id: int
    elapsed_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {"sample_rate": self.sample_rate, "duration": round(self.duration, 3),
                "speaker_id": self.speaker_id, "elapsed_ms": self.elapsed_ms,
                "bytes": len(self.wav)}


@dataclass(frozen=True)
class VoiceprintResult:
    speaker_id: str
    embedding: tuple[float, ...]
    dim: int
    elapsed_ms: int


@dataclass(frozen=True)
class ModelPresence:
    component: str
    ready: bool
    files: dict[str, bool] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"component": self.component, "ready": self.ready,
                "files": self.files, "reason": self.reason}


# --------------------------------------------------------------------------- #
# 音频解码
# --------------------------------------------------------------------------- #
def decode_wav(data: bytes) -> tuple[list[float], int]:
    """PCM16 WAV → ([-1,1] 浮点采样, 采样率)。

    **只接受 16bit PCM WAV**：手机端由 `RecorderMicrophone` 直接给 PCM16，
    服务端也只认这一种，避免「不同编码悄悄被当成同一种」。
    """
    if not data:
        raise AudioRejected("音频为空")
    if len(data) < 44:
        raise AudioRejected("音频过短，不是合法 WAV（缺文件头）")
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioRejected(f"不是合法 WAV：{exc}") from exc

    if width != 2:
        raise AudioRejected(f"只支持 16bit PCM，实际 {width * 8}bit")
    if channels not in (1, 2):
        raise AudioRejected(f"只支持单声道或双声道，实际 {channels} 声道")
    pcm = array.array("h")
    pcm.frombytes(frames)
    if channels == 2:
        pcm = array.array("h", pcm[::2])      # 只取左声道，不混音（混音会引入相位抵消）
    samples = [v / 32768.0 for v in pcm]
    return samples, rate


def resample(samples: Sequence[float], src_rate: int, dst_rate: int) -> list[float]:
    """线性插值重采样。

    **如实标注局限**：线性插值是保底手段，频响不如 sinc/多相滤波，
    对低质量上采样会有镜像分量。之所以仍采用：手机端固定 16k 直录，
    走到这里的只会是「外部导入的 wav」，而不是主链路。
    """
    if src_rate == dst_rate or not samples:
        return list(samples)
    ratio = dst_rate / src_rate
    out_len = int(round(len(samples) * ratio))
    out: list[float] = []
    for i in range(out_len):
        pos = i / ratio
        left = int(pos)
        frac = pos - left
        a = samples[left]
        b = samples[left + 1] if left + 1 < len(samples) else a
        out.append(a + (b - a) * frac)
    return out


def samples_to_wav(samples: Sequence[float], sample_rate: int) -> bytes:
    """浮点采样 → PCM16 WAV 字节（端侧拿到即可直接播）。"""
    pcm = array.array("h")
    for value in samples:
        clipped = max(-1.0, min(1.0, float(value)))
        pcm.append(int(clipped * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 模型路径解析
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpeechModelPaths:
    root: Path
    vad: Path
    asr_encoder: Path
    asr_decoder: Path
    asr_joiner: Path
    asr_tokens: Path
    tts_model: Path
    tts_tokens: Path
    tts_lexicon: Path
    tts_dict_dir: Path
    speaker: Path

    @classmethod
    def resolve(cls, root: str | Path) -> "SpeechModelPaths":
        base = Path(root)
        return cls(
            root=base,
            vad=base / "vad" / "silero_vad.onnx",
            asr_encoder=base / "asr-streaming-zh" / "encoder-epoch-99-avg-1.int8.onnx",
            asr_decoder=base / "asr-streaming-zh" / "decoder-epoch-99-avg-1.int8.onnx",
            asr_joiner=base / "asr-streaming-zh" / "joiner-epoch-99-avg-1.int8.onnx",
            asr_tokens=base / "asr-streaming-zh" / "tokens.txt",
            tts_model=base / "tts-zh" / "model.onnx",
            tts_tokens=base / "tts-zh" / "tokens.txt",
            tts_lexicon=base / "tts-zh" / "lexicon.txt",
            tts_dict_dir=base / "tts-zh" / "dict",
            speaker=base / "speaker" / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        )

    def presence(self) -> tuple[ModelPresence, ...]:
        """逐组件报告模型文件是否齐备（供 `/api/voice/availability`）。"""
        def check(component: str, files: dict[str, Path]) -> ModelPresence:
            states = {name: path.is_file() and path.stat().st_size > 0
                      for name, path in files.items()}
            missing = [name for name, ok in states.items() if not ok]
            return ModelPresence(
                component=component, ready=not missing, files=states,
                reason="" if not missing else
                "缺少 " + "、".join(missing) + "；运行 "
                "scripts/download_speech_models.py 补齐")

        return (
            check("vad", {"silero_vad.onnx": self.vad}),
            check("asr", {"encoder": self.asr_encoder, "decoder": self.asr_decoder,
                          "joiner": self.asr_joiner, "tokens": self.asr_tokens}),
            check("tts", {"model.onnx": self.tts_model, "tokens": self.tts_tokens,
                          "lexicon": self.tts_lexicon}),
            check("speaker", {"campplus": self.speaker}),
        )


# --------------------------------------------------------------------------- #
# 推理运行时
# --------------------------------------------------------------------------- #
class SpeechRuntime:
    """sherpa-onnx 引擎集合。**懒加载 + 单飞**：同一时刻只跑一次推理。

    为什么单飞而不是并发：sherpa-onnx 的 `OnlineRecognizer` / `OfflineTts`
    内部持有可变流状态，多线程共享会互相踩。并发靠**进程级**水平扩展解决，
    不在这里假装线程安全。
    """

    def __init__(self, paths: SpeechModelPaths, *, max_audio_seconds: float = 60.0,
                 tts_speaker: int = 0, tts_speed: float = 1.0,
                 num_threads: int = 2) -> None:
        if max_audio_seconds <= 0:
            raise SpeechError("音频时长上限必须为正数")
        if not 0.5 <= tts_speed <= 2.0:
            # 超出这个区间的语速合成质量会明显劣化，与其让用户听不清不如拒绝
            raise SpeechError(f"语速 {tts_speed} 超出 0.5~2.0 的可用区间")
        self.paths = paths
        self.max_audio_seconds = max_audio_seconds
        self.tts_speaker = tts_speaker
        self.tts_speed = tts_speed
        self.num_threads = num_threads

        self._lock = threading.RLock()
        self._vad = None
        self._asr = None
        self._tts = None
        self._speaker = None
        self._init_errors: dict[str, str] = {}

    # ---------------- 可用性 ----------------
    def availability(self) -> dict[str, Any]:
        presences = [p.as_dict() for p in self.paths.presence()]
        return {
            "root": str(self.paths.root),
            "sample_rate": TARGET_SAMPLE_RATE,
            "max_audio_seconds": self.max_audio_seconds,
            "components": presences,
            "init_errors": dict(self._init_errors),
            "ready": {p["component"]: p["ready"] for p in presences},
        }

    def _require(self, component: str, files: Sequence[Path], attr: str, factory):
        """统一的三段式：查文件 → 建实例（失败记账）→ 返回。"""
        missing = [str(p) for p in files if not (p.is_file() and p.stat().st_size > 0)]
        if missing:
            raise SpeechUnavailable(
                component,
                "缺少模型文件 " + "、".join(missing)
                + "；运行 scripts/download_speech_models.py 补齐")
        with self._lock:
            cached = getattr(self, attr)
            if cached is not None:
                return cached
            if component in self._init_errors:
                raise SpeechUnavailable(component, self._init_errors[component])
            try:
                engine = factory()
            except Exception as exc:                  # noqa: BLE001 —— 初始化失败要如实记账
                self._init_errors[component] = f"{type(exc).__name__}: {exc}"
                raise SpeechUnavailable(component, self._init_errors[component]) from exc
            setattr(self, attr, engine)
            return engine

    # ---------------- VAD ----------------
    def segment(self, samples: Sequence[float], *, sample_rate: int = TARGET_SAMPLE_RATE
                ) -> tuple[SpeechSegment, ...]:
        """把人声切成段。静音整体丢弃，**并把「一句都没有」如实返回空元组**。"""
        def build():
            import sherpa_onnx

            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = str(self.paths.vad)
            config.silero_vad.threshold = 0.5
            config.silero_vad.min_silence_duration = 0.25
            config.silero_vad.min_speech_duration = 0.25
            config.silero_vad.max_speech_duration = 20.0
            config.silero_vad.window_size = VAD_WINDOW_SIZE
            config.sample_rate = sample_rate
            config.num_threads = 1
            return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)

        vad = self._require("vad", [self.paths.vad], "_vad", build)
        if sample_rate != TARGET_SAMPLE_RATE:
            samples = resample(samples, sample_rate, TARGET_SAMPLE_RATE)

        segments: list[SpeechSegment] = []
        offset = 0.0
        with self._lock:
            vad.reset()
            cursor = 0
            total = len(samples)
            while cursor + VAD_WINDOW_SIZE <= total:
                vad.accept_waveform(list(samples[cursor:cursor + VAD_WINDOW_SIZE]))
                cursor += VAD_WINDOW_SIZE
                while not vad.empty():
                    seg = vad.front
                    data = tuple(seg.samples)
                    start = offset
                    end = offset + len(data) / TARGET_SAMPLE_RATE
                    segments.append(SpeechSegment(start, end, data))
                    offset = end
                    vad.pop()
            # 尾音必须 flush，否则最后半句永远出不来（KWS 同款坑，见 E-13）
            vad.flush()
            while not vad.empty():
                seg = vad.front
                data = tuple(seg.samples)
                start = offset
                end = offset + len(data) / TARGET_SAMPLE_RATE
                segments.append(SpeechSegment(start, end, data))
                offset = end
                vad.pop()
        return tuple(segments)

    # ---------------- ASR ----------------
    def recognize(self, samples: Sequence[float], *, sample_rate: int = TARGET_SAMPLE_RATE
                  ) -> AsrResult:
        """整段音频 → 文字。先 VAD 切段，再逐段识别后拼接。

        为什么要先切：ZipFormer 对流式输入有「最长片段」的限制，
        长静音会让第一段迟迟不出结果；先切段还能给出每句的起止时间，
        供端侧「逐句高亮」。
        """
        started = time.perf_counter()
        duration = len(samples) / float(sample_rate or TARGET_SAMPLE_RATE)
        if duration > self.max_audio_seconds:
            raise AudioRejected(
                f"音频 {duration:.1f}s 超过上限 {self.max_audio_seconds:.0f}s；"
                "请不要悄悄截断，改为分段上传")
        if duration <= 0:
            raise AudioRejected("音频长度为 0")

        segments = self.segment(samples, sample_rate=sample_rate)
        if not segments:
            return AsrResult(text="", segments=(), audio_seconds=duration,
                             speech_seconds=0.0,
                             elapsed_ms=int((time.perf_counter() - started) * 1000),
                             model=self._asr_label())

        recognizer = self._require(
            "asr",
            [self.paths.asr_encoder, self.paths.asr_decoder,
             self.paths.asr_joiner, self.paths.asr_tokens],
            "_asr", self._build_asr)

        pieces: list[str] = []
        with self._lock:
            for segment in segments:
                stream = recognizer.create_stream()
                stream.accept_waveform(TARGET_SAMPLE_RATE, list(segment.samples))
                # 尾部补 0.5s 静音：模型需要一点尾音才对最后一个字下判定
                stream.accept_waveform(TARGET_SAMPLE_RATE,
                                       [0.0] * (TARGET_SAMPLE_RATE // 2))
                stream.input_finished()
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                text = recognizer.get_result(stream)
                text = text.strip() if isinstance(text, str) else str(text).strip()
                if text:
                    pieces.append(text)

        return AsrResult(
            text="".join(pieces), segments=segments, audio_seconds=duration,
            speech_seconds=sum(s.duration for s in segments),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            model=self._asr_label(),
        )

    def _build_asr(self):
        import sherpa_onnx

        return sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(self.paths.asr_encoder),
            decoder=str(self.paths.asr_decoder),
            joiner=str(self.paths.asr_joiner),
            tokens=str(self.paths.asr_tokens),
            num_threads=self.num_threads,
            sample_rate=TARGET_SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
        )

    def _asr_label(self) -> str:
        return "sherpa-onnx/streaming-zipformer-zh-14M-int8"

    # ---------------- TTS ----------------
    def synthesize(self, text: str, *, speaker_id: int | None = None,
                   speed: float | None = None) -> TtsResult:
        """文字 → WAV 字节。空文本与超长文本都拒绝，不做静默裁剪。"""
        cleaned = (text or "").strip()
        if not cleaned:
            raise AudioRejected("待合成文本为空")
        if len(cleaned) > 500:
            raise AudioRejected(f"待合成文本 {len(cleaned)} 字，超过单次上限 500 字；"
                                "长文本请逐句下发（流式播报本就是逐句的）")
        sid = self.tts_speaker if speaker_id is None else speaker_id
        rate = self.tts_speed if speed is None else speed

        started = time.perf_counter()
        tts = self._require("tts",
                            [self.paths.tts_model, self.paths.tts_tokens,
                             self.paths.tts_lexicon],
                            "_tts", self._build_tts)
        with self._lock:
            if not 0 <= sid < tts.num_speakers:
                raise AudioRejected(
                    f"说话人 {sid} 不存在：该模型共 {tts.num_speakers} 个")
            audio = tts.generate(cleaned, sid=sid, speed=rate)
            samples = list(audio.samples)
            sample_rate = int(audio.sample_rate)
        if not samples:
            raise SpeechError("TTS 未产出音频（模型返回空采样）")

        return TtsResult(
            wav=samples_to_wav(samples, sample_rate), sample_rate=sample_rate,
            duration=len(samples) / float(sample_rate), speaker_id=sid,
            elapsed_ms=int((time.perf_counter() - started) * 1000))

    def _build_tts(self):
        import sherpa_onnx

        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(self.paths.tts_model),
                    lexicon=str(self.paths.tts_lexicon),
                    tokens=str(self.paths.tts_tokens),
                    dict_dir=str(self.paths.tts_dict_dir),
                ),
                num_threads=self.num_threads, debug=False, provider="cpu"),
            rule_fsts="", max_num_sentences=2)
        return sherpa_onnx.OfflineTts(config)

    def tts_speakers(self) -> int:
        tts = self._require("tts",
                            [self.paths.tts_model, self.paths.tts_tokens,
                             self.paths.tts_lexicon],
                            "_tts", self._build_tts)
        return int(tts.num_speakers)

    # ---------------- 声纹 ----------------
    def embed_speaker(self, samples: Sequence[float],
                      *, sample_rate: int = TARGET_SAMPLE_RATE
                      ) -> tuple[float, ...]:
        """取说话人 embedding（192 维，CAM++）。

        **不足 0.5 秒的音频直接拒绝**：太短的片段 embedding 不稳定，
        拿去比对只会随机通过或随机失败，两种都不可接受。
        """
        duration = len(samples) / float(sample_rate or TARGET_SAMPLE_RATE)
        if duration < 0.5:
            raise AudioRejected(f"音频 {duration:.2f}s 太短，声纹至少需要 0.5s")
        extractor = self._require("speaker", [self.paths.speaker], "_speaker",
                                   self._build_speaker)
        data = list(samples)
        if sample_rate != TARGET_SAMPLE_RATE:
            data = resample(data, sample_rate, TARGET_SAMPLE_RATE)
        with self._lock:
            stream = extractor.create_stream()
            stream.accept_waveform(TARGET_SAMPLE_RATE, data)
            stream.input_finished()
            return tuple(float(v) for v in extractor.compute(stream))

    def _build_speaker(self):
        import sherpa_onnx

        return sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.paths.speaker), num_threads=1, debug=False,
                provider="cpu"))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度。长度不一致**直接报错**，不做补零对齐（补零会伪造相似度）。"""
    if len(a) != len(b):
        raise SpeechError(f"声纹维度不一致：{len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
# 声纹登记表（落盘）
# --------------------------------------------------------------------------- #
class VoiceprintStore:
    """按账号保存声纹 embedding。

    **存 embedding 而不是存音频**：embedding 无法还原成可听语音，
    是「既能比对、又不留声纹原件」的最小留存。这是 U63 的隐私边界。

    落盘形态是普通 JSON（单账号 192 个 float ≈ 4KB），
    用户量大时应迁到 P1 的加密存储；此处如实标注为**开发形态**。
    """

    def __init__(self, path: str | Path, *, max_samples_per_user: int = 3) -> None:
        self.path = Path(path)
        self.max_samples_per_user = max_samples_per_user
        self._lock = threading.RLock()

    def _load(self) -> dict[str, list[list[float]]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 坏档不静默清空：清空等于「所有人的声纹都没了」，但不影响登录
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): [list(map(float, v)) for v in vs]
                for k, vs in raw.items() if isinstance(vs, list)}

    def _save(self, data: dict[str, list[list[float]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def enroll(self, user_id: str, embedding: Sequence[float]) -> dict[str, Any]:
        uid = (user_id or "").strip()
        if not uid:
            raise SpeechError("声纹登记必须带 user_id")
        if not embedding:
            raise SpeechError("声纹登记不得使用空 embedding")
        with self._lock:
            data = self._load()
            samples = data.get(uid, [])
            samples.append([float(v) for v in embedding])
            # 只留最近 N 条：声纹会随年龄/设备漂移，留最新几条比永久累积更实用
            samples = samples[-self.max_samples_per_user:]
            data[uid] = samples
            self._save(data)
        return {"user_id": uid, "samples": len(samples),
                "dim": len(embedding)}

    def samples_of(self, user_id: str) -> list[list[float]]:
        with self._lock:
            return list(self._load().get((user_id or "").strip(), []))

    def has(self, user_id: str) -> bool:
        return bool(self.samples_of(user_id))

    def forget(self, user_id: str) -> bool:
        with self._lock:
            data = self._load()
            existed = data.pop((user_id or "").strip(), None) is not None
            self._save(data)
        return existed

    def score(self, user_id: str, embedding: Sequence[float]) -> float | None:
        """与已登记样本逐条比余弦，取**最大值**（最像的那一次）。

        取 max 而非均值：声纹样本之间本身有差异，均值会稀释掉「某一次特别像」
        这个最有信息量的信号。
        """
        samples = self.samples_of(user_id)
        if not samples:
            return None
        scored = [cosine(embedding, sample) for sample in samples
                  if len(sample) == len(embedding)]
        return max(scored) if scored else None
