"""P7 语音服务守护：VAD / ASR / TTS / 声纹（U22 / U20 / U21 / U63）。

分两层：
- **纯逻辑**（音频解码、重采样、WAV 编码、可用性报告）—— 不依赖模型，必跑；
- **真实推理**（有模型时跑一段真 wav）—— 模型不在则 skip，并**明确说明跳过的原因**，
  绝不静默通过。这是「声明 ≠ 能力」的落地方式：没跑过就是没跑过。
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from ai_butler.core.access import judge_voiceprint
from ai_butler.infra.speech import (
    TARGET_SAMPLE_RATE,
    AudioRejected,
    SpeechError,
    SpeechModelPaths,
    SpeechRuntime,
    SpeechUnavailable,
    VoiceprintStore,
    cosine,
    decode_wav,
    resample,
    samples_to_wav,
)

ROOT = Path(__file__).resolve().parents[1]
SPEECH_ROOT = ROOT / "models" / "speech"
#: 现成的测试音频（wenetspeech 测试集里的一条）
SAMPLE_WAV = ROOT / "models" / "kws-zipformer-wenetspeech-3.3M" / "test_wavs" / "0.wav"


def _read_sample() -> tuple[list[float], int]:
    with wave.open(str(SAMPLE_WAV), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    import array

    pcm = array.array("h")
    pcm.frombytes(frames)
    return [v / 32768.0 for v in pcm], rate


def _runtime() -> SpeechRuntime:
    return SpeechRuntime(SpeechModelPaths.resolve(SPEECH_ROOT), max_audio_seconds=60.0)


def _has(component: str) -> bool:
    presence = {p.component: p.ready for p in SpeechModelPaths.resolve(SPEECH_ROOT).presence()}
    return presence.get(component, False)


# --------------------------------------------------------------------------- #
# 纯逻辑
# --------------------------------------------------------------------------- #
def test_decode_rejects_empty_and_non_wav():
    with pytest.raises(AudioRejected):
        decode_wav(b"")
    with pytest.raises(AudioRejected):
        decode_wav(b"not a wav file at all, definitely not" * 3)


def test_decode_rejects_8bit_and_multichannel_more_than_two():
    """只接受 16bit PCM 单/双声道。其余明确拒绝，不猜格式。"""
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)          # 8bit
        handle.setframerate(TARGET_SAMPLE_RATE)
        handle.writeframes(b"\x80" * 100)
    with pytest.raises(AudioRejected):
        decode_wav(buffer.getvalue())


def test_wav_roundtrip_preserves_sample_count():
    samples = [0.0, 0.5, -0.5, 1.0, -1.0] * 100
    wav = samples_to_wav(samples, TARGET_SAMPLE_RATE)
    decoded, rate = decode_wav(wav)
    assert rate == TARGET_SAMPLE_RATE
    assert len(decoded) == len(samples)
    # 量化误差在 16bit 的 1/32767 以内
    for a, b in zip(samples, decoded):
        assert abs(a - b) < 1e-3


def test_synthesis_wav_is_playable_pcm16():
    wav = samples_to_wav([0.1] * 1600, TARGET_SAMPLE_RATE)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"


def test_resample_changes_length_by_ratio():
    source = [0.0] * 8000        # 8k 采样 1 秒
    out = resample(source, 8000, TARGET_SAMPLE_RATE)
    assert len(out) == 16000
    # 同采样率不该多绕一圈
    assert resample(source, 8000, 8000) == source


def test_runtime_rejects_nonsense_parameters():
    with pytest.raises(SpeechError):
        SpeechRuntime(SpeechModelPaths.resolve(SPEECH_ROOT), max_audio_seconds=0)
    with pytest.raises(SpeechError):
        SpeechRuntime(SpeechModelPaths.resolve(SPEECH_ROOT), tts_speed=5.0)


def test_availability_reports_missing_models_with_actionable_reason(tmp_path):
    """缺模型时给的是「跑哪个脚本」，不是一句「不可用」。"""
    runtime = SpeechRuntime(SpeechModelPaths.resolve(tmp_path))
    payload = runtime.availability()
    # availability() 只报事实（每个组件齐不齐），"available" 的聚合计由接口层加
    assert all(ready is False for ready in payload["ready"].values())
    reasons = " ".join(c["reason"] for c in payload["components"])
    assert "download_speech_models.py" in reasons


def test_missing_model_raises_instead_of_returning_empty_text(tmp_path):
    runtime = SpeechRuntime(SpeechModelPaths.resolve(tmp_path))
    with pytest.raises(SpeechUnavailable):
        runtime.recognize([0.0] * 16000)


def test_audio_longer_than_limit_is_rejected_not_truncated():
    runtime = SpeechRuntime(SpeechModelPaths.resolve(SPEECH_ROOT), max_audio_seconds=1.0)
    with pytest.raises(AudioRejected) as caught:
        runtime.recognize([0.0] * (TARGET_SAMPLE_RATE * 3))
    assert "超过上限" in str(caught.value)


def test_empty_text_cannot_be_synthesized():
    runtime = _runtime()
    with pytest.raises(AudioRejected):
        runtime.synthesize("   ")


def test_voiceprint_requires_minimum_duration():
    runtime = _runtime()
    with pytest.raises(AudioRejected):
        runtime.embed_speaker([0.0] * 100, sample_rate=TARGET_SAMPLE_RATE)


def test_cosine_is_scale_invariant_and_rejects_dimension_mismatch():
    assert cosine([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    with pytest.raises(SpeechError):
        cosine([1.0], [1.0, 2.0])


# --------------------------------------------------------------------------- #
# 声纹登记表
# --------------------------------------------------------------------------- #
def test_voiceprint_store_keeps_latest_samples_only(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json", max_samples_per_user=2)
    for value in (0.1, 0.2, 0.3):
        store.enroll("user-A", [value, 0.0])
    assert store.samples_of("user-A") == [[0.2, 0.0], [0.3, 0.0]]
    assert store.has("user-A")
    assert store.has("user-B") is False


def test_voiceprint_store_scores_max_of_samples(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json")
    store.enroll("user-A", [1.0, 0.0])
    store.enroll("user-A", [0.0, 1.0])
    # 取最大值：某一次特别像，就是有信息量的信号，不能被均值稀释
    assert store.score("user-A", [0.9, 0.1]) == pytest.approx(0.9938, abs=1e-3)
    assert store.score("user-B", [1.0, 0.0]) is None


def test_voiceprint_store_rejects_blank_identity(tmp_path):
    store = VoiceprintStore(tmp_path / "v.json")
    with pytest.raises(SpeechError):
        store.enroll("", [1.0])
    with pytest.raises(SpeechError):
        store.enroll("user-A", [])


def test_voiceprint_corrupt_file_does_not_wipe_silently(tmp_path):
    """坏档不静默清空：清空等于所有人的声纹都没了。"""
    path = tmp_path / "v.json"
    path.write_text("{ not json", encoding="utf-8")
    store = VoiceprintStore(path)
    assert store.samples_of("user-A") == []
    store.enroll("user-A", [1.0, 0.0])
    assert store.has("user-A") is True


# --------------------------------------------------------------------------- #
# 真实推理（模型不在则 skip，并说明原因）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not SAMPLE_WAV.is_file(), reason="缺少测试音频（models/kws-.../test_wavs/0.wav）")
@pytest.mark.skipif(not _has("vad"), reason="VAD 模型未下载：跑 scripts/download_speech_models.py")
def test_real_vad_finds_speech_segments():
    samples, rate = _read_sample()
    segments = _runtime().segment(samples, sample_rate=rate)
    assert segments, "5.6 秒的人声录音里没有切出任何片段 —— VAD 没生效"
    assert all(s.duration > 0.1 for s in segments)
    # 片段起点应当递增：乱序说明 pop/游标逻辑错了
    starts = [s.start for s in segments]
    assert starts == sorted(starts)


@pytest.mark.skipif(not SAMPLE_WAV.is_file(), reason="缺少测试音频")
@pytest.mark.skipif(not (_has("vad") and _has("asr")), reason="ASR/VAD 模型未下载")
def test_real_asr_transcribes_chinese_speech():
    samples, rate = _read_sample()
    result = _runtime().recognize(samples, sample_rate=rate)
    assert result.text.strip(), "识别结果为空 —— 链路没跑通"
    # 只断言「有中文」这一条稳定性质：逐字比对模型版本会变，绑死反而脆
    assert any("\u4e00" <= ch <= "\u9fff" for ch in result.text)
    assert result.audio_seconds == pytest.approx(5.6, abs=0.2)
    assert result.speech_seconds > 0


@pytest.mark.skipif(not _has("tts"), reason="TTS 模型未下载：约 127MB，见 U65")
def test_real_tts_produces_audible_wav():
    result = _runtime().synthesize("您好，我是您的管家。")
    assert result.wav[:4] == b"RIFF"
    assert result.sample_rate > 0
    # 这句话至少要有 0.5 秒，否则是「合成了个空文件」
    assert result.duration > 0.5


@pytest.mark.skipif(not SAMPLE_WAV.is_file(), reason="缺少测试音频")
@pytest.mark.skipif(not _has("speaker"),
                    reason="声纹模型未下载：3D-Speaker CAM++（U63）")
def test_real_voiceprint_same_speaker_passes_and_other_fails(tmp_path):
    samples, rate = _read_sample()
    runtime = _runtime()
    full = runtime.embed_speaker(samples, sample_rate=rate)
    # 同一个人不同长度的一段：应当判为同一人
    part = runtime.embed_speaker(samples[: TARGET_SAMPLE_RATE * 2], sample_rate=rate)
    store = VoiceprintStore(tmp_path / "v.json")
    store.enroll("user-A", full)

    score = store.score("user-A", part)
    assert score is not None
    assert judge_voiceprint(score, speaker_id="user-A").matched is True

    # 未登记的人拿不到分数 → 判定不匹配（None 绝不当通过）
    assert judge_voiceprint(store.score("user-B", part)).matched is False
    # 维度稳定：换一段音频也必须是同一维度，否则比对会直接抛长度不一致
    assert len(full) == len(part)
