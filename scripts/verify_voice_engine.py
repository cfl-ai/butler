"""P7 端侧语音引擎验证（真机之前的最后一层证据）。

为什么要有这个脚本
------------------
P7 的端侧适配器要跑在手机上，本机没有 Android 设备。但**引擎能否装上、模型是否完整、
唤醒词能否真的被检出**这几件事可以在本机验证，不必等到真机才发现选型不成立。

覆盖的裁定
----------
- U06 离线唤醒：sherpa-onnx KWS + wenetspeech ZipFormer（int8 约 4.9MB）
- U20 流式 ASR：sherpa-onnx OnlineRecognizer（同栈）
- U21 端侧 TTS 兜底：sherpa-onnx OfflineTts
- U22 VAD：silero-vad（sherpa-onnx 内置）

分层结论
--------
1. ~{引擎层}~：引擎类可用、模型完整、唤醒词 token 合法 —— 本机可验证；
2. ~{推理层}~：**真实音频能否触发检出** —— 本机可验证；
3. ~{真机层}~：麦克风权限、后台保活、功耗、Android 14 前台服务限制（U23）——
   **只有真机能回答**，本脚本不假装覆盖。

推理层是怎么验通的（这段很重要，别再走一遍弯路）
------------------------------------------------
模型仓（modelscope）里的 ~{test_wavs/test_keywords.txt}~ **与同目录的音频并不对应** ——
用同一套模型当普通 ASR 转写 0.wav，得到的是
「对我做了介绍么我想说的是大家如果对我点酒感兴趣」，
里面**根本没有** test_keywords.txt 列的那些词（文森特卡索、周望军…）。
拿它做关键词表自然一个都检不出，很容易被误判成「引擎不兼容」。

正确做法是**先用 ASR 转写音频，再从转写结果里取确实存在的短语构造关键词**：
~{j iè sh ào @介绍}~、~{g ǎn x ìng q ù @感兴趣}~、~{w ǒ x iǎng sh uō d e sh ì @我想说的是}~、
~{d à j iā @大家}~ —— 这四条**全部被正确检出**，引擎与模型链路由此坐实。

另有一个坑：关键词的**每个音节要拆成「声母 + 韵母」两个 token**
（介绍 = ~{j~} + ~{iè~} + ~{sh~} + ~{ào~}），不是把整音节写成一个 token；
token 不在 ~{tokens.txt}~ 里时引擎会打印 "Cannot find ID for token"。

用法
----
    python scripts/verify_voice_engine.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "kws-zipformer-wenetspeech-3.3M"

ENCODER = "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
DECODER = "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
JOINER = "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"

#: U06 给出的自定义唤醒词拼音 token 行（「你好管家」）
WAKE_WORD_LINE = " n ǐ h ǎo g u ǎn j i ā @你好管家"

#: 从测试音频**转写结果**里取出的、确实存在的短语（见文件头说明）。
#: 用它们才能验证 KWS —— 模型仓自带的 test_keywords.txt 与音频不对应。
VERIFIED_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("介绍", "j iè sh ào"),
    ("感兴趣", "g ǎn x ìng q ù"),
    ("我想说的是", "w ǒ x iǎng sh uō d e sh ì"),
    ("大家", "d à j iā"),
)

#: 四条里至少命中这么多才算推理层通过（留一条余量：边界切分本就可能漏）
MIN_HITS = 3

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_results: list[tuple[str, str, str]] = []


def record(layer: str, status: str, detail: str) -> None:
    _results.append((layer, status, detail))
    print(f"   [{status}] {layer}：{detail}")


def fail(message: str) -> None:
    print(f"   [FAIL] {message}")
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
def load_tokens() -> set[str]:
    path = MODEL / "tokens.txt"
    if not path.is_file():
        fail("缺少 tokens.txt")
    return {line.split()[0] for line in path.read_text(encoding="utf-8").splitlines()
            if len(line.split()) >= 2}


def check_import() -> None:
    print("[1/5] 引擎导入（U06/U20/U21/U22 四项裁定各自的类）")
    try:
        import sherpa_onnx
    except Exception as error:
        fail(f"未安装 sherpa-onnx：{error}")
    required = {
        "U06 离线唤醒": "KeywordSpotter",
        "U20 流式 ASR": "OnlineRecognizer",
        "U21 端侧 TTS": "OfflineTts",
        "U22 VAD": "VadModel",
    }
    for label, name in required.items():
        if not hasattr(sherpa_onnx, name):
            fail(f"{label} 需要的 {name} 不存在")
    record("引擎层·类可用性", PASS, "、".join(required.values()) + " 均存在")


def check_model_files() -> float:
    print("[2/5] 模型文件完整性（U06：int8 全套约 4.9MB）")
    sizes: dict[str, int] = {}
    for name in (ENCODER, DECODER, JOINER, "tokens.txt", "keywords.txt"):
        path = MODEL / name
        if not path.is_file():
            fail(f"缺少 {name}")
        sizes[name] = path.stat().st_size
    int8_total = sum(sizes[n] for n in (ENCODER, DECODER, JOINER))
    mb = int8_total / 1024 / 1024
    if not 4.0 <= mb <= 6.0:
        fail(f"int8 体量 {mb:.2f} MB 与裁定 U06 记载的「约 4.9MB」差距过大")
    record("引擎层·模型完整性", PASS, f"int8 三件套 {mb:.2f} MB（裁定 U06 记为「约 4.9MB」）")
    return int8_total


def read_wave(path: Path) -> tuple[list[float], int]:
    """读 16bit PCM wav。

    **不用 sherpa_onnx.read_wave**：该版本只提供 write_wave，没有读函数（实测 AttributeError）。
    """
    import array
    import wave

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            fail(f"{path.name} 不是 16bit PCM")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    samples = array.array("h", raw)
    if channels > 1:
        samples = samples[::channels]
    scale = 1.0 / 32768.0
    return [s * scale for s in samples], rate


def transcribe(wav: Path) -> str:
    """用同一套模型当普通 ASR 转写（U20 的用法）。"""
    import sherpa_onnx

    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(MODEL / "tokens.txt"),
        encoder=str(MODEL / ENCODER), decoder=str(MODEL / DECODER),
        joiner=str(MODEL / JOINER), num_threads=1, sample_rate=16000,
        feature_dim=80, decoding_method="greedy_search", provider="cpu")
    samples, sample_rate = read_wave(wav)
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    stream.accept_waveform(sample_rate, [0.0] * int(0.5 * sample_rate))
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    return recognizer.get_result(stream) or ""


def detect(keyword_lines: list[str], wav: Path, *, score: float = 1.0,
           threshold: float = 0.25) -> list[str]:
    """返回本段音频中检出的关键词。

    两个必须照做的用法（都踩过）：
    1. **结果要在解码循环内逐块读** —— KWS 的结果逐块产生，循环结束后再读只会拿到空串；
    2. 检出后要 reset_stream，否则同一条流不会报第二次。
    """
    import sherpa_onnx

    path = MODEL / "_verify_keywords.txt"
    path.write_text("\n".join(keyword_lines) + "\n", encoding="utf-8")
    spotter = sherpa_onnx.KeywordSpotter(
        tokens=str(MODEL / "tokens.txt"),
        encoder=str(MODEL / ENCODER), decoder=str(MODEL / DECODER),
        joiner=str(MODEL / JOINER), keywords_file=str(path),
        num_threads=1, keywords_score=score, keywords_threshold=threshold,
        provider="cpu")

    samples, sample_rate = read_wave(wav)
    stream = spotter.create_stream()
    stream.accept_waveform(sample_rate, samples)
    stream.accept_waveform(sample_rate, [0.0] * int(0.6 * sample_rate))
    stream.input_finished()

    found: list[str] = []
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        result = spotter.get_result(stream)
        if result:
            found.append(result)
            spotter.reset_stream(stream)
    return found


def check_detection() -> bool:
    print("[3/5] 真实推理（推理层）")
    wavs = sorted((MODEL / "test_wavs").glob("*.wav"))
    if not wavs:
        fail("没有测试音频")
    wav = wavs[0]

    # 先转写，证明这套模型确实能读懂音频
    text = transcribe(wav)
    if not text:
        record("推理层·ASR 转写", FAIL, "转写为空，模型链路不成立")
        return False
    print(f"   ASR 转写 {wav.name}：{text}")

    # 再拿转写结果里确实存在的短语构造关键词，验证 KWS 能检出
    hits: list[str] = []
    for label, pinyin in VERIFIED_KEYWORDS:
        got = detect([f" {pinyin} @{label}"], wav)
        if got:
            hits.append(label)
            print(f"   检出「{label}」（{pinyin}）")
        else:
            print(f"   未检出「{label}」（{pinyin}）")

    record("推理层·ASR 转写", PASS, f"{wav.name} 转写成功（{len(text)} 字符）")
    if len(hits) >= MIN_HITS:
        record("推理层·唤醒检出", PASS,
               f"{len(VERIFIED_KEYWORDS)} 条关键词检出 {len(hits)} 条（阈值 {MIN_HITS}）")
        return True
    record("推理层·唤醒检出", WARN,
           f"仅检出 {len(hits)}/{len(VERIFIED_KEYWORDS)} 条，低于阈值 {MIN_HITS}")
    return False


def check_custom_wake_word() -> None:
    print("[4/5] 自定义唤醒词（U06：「你好管家」只需追加拼音 token 行）")
    custom = MODEL / "keywords_butler.txt"
    if not custom.is_file():
        fail("缺少 keywords_butler.txt")
    lines = [x for x in custom.read_text(encoding="utf-8").splitlines() if x.strip()]
    if WAKE_WORD_LINE not in lines:
        fail("自定义唤醒词行未写入")

    tokens = load_tokens()
    pinyin = WAKE_WORD_LINE.split("@")[0].split()
    missing = [t for t in pinyin if t not in tokens]
    if missing:
        fail(f"「你好管家」含 tokens.txt 里没有的 token：{missing}")
    record("引擎层·自定义唤醒词", PASS,
           f"「你好管家」拆为 {len(pinyin)} 个 token（声母+韵母），全部在 tokens.txt 内；"
           f"关键词表共 {len(lines)} 行")


def check_other_engines() -> None:
    print("[5/5] 其余两件引擎的可构造性")
    import sherpa_onnx

    vad = sherpa_onnx.SileroVadModelConfig()
    sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig()))
    record("引擎层·其余两件", PASS,
           f"VadModelConfig(threshold={vad.threshold}) / OfflineTtsConfig 可构造")


def main() -> int:
    print("=" * 70)
    print("P7 端侧语音引擎验证（真机之前的最后一层证据）")
    print("=" * 70)
    check_import()
    int8 = check_model_files()
    detected = check_detection()
    check_custom_wake_word()
    check_other_engines()

    print("=" * 70)
    engine_layer = [r for r in _results if r[0].startswith("引擎层")]
    print("分层结论：")
    print(f"  引擎层（本机可验证）：{len(engine_layer)} 项，"
          f"{sum(1 for r in engine_layer if r[1] == PASS)} 项 PASS")
    print(f"  推理层（本机可验证）：{'PASS' if detected else '未通过'}")
    print("  真机层（只有真机能回答）：麦克风权限、后台保活、功耗、"
          "Android 14 前台服务限制（U23）—— 本脚本不覆盖")
    print(f"  模型体量：int8 {int8/1024/1024:.2f} MB")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
