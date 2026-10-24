"""下载 P7 语音链路所需的端侧/云端模型（VAD / ASR / TTS / 声纹）。

裁定依据
--------
- 三、P7 步骤4「开发 VAD 人声检测、实时收音、随时打断机制」→ **U22** silero-vad
- 三、P7 步骤2 / U20「离线唤醒 + 流式 ASR」→ sherpa-onnx 流式 ZipFormer
- U21「云端 CosyVoice2 为主，端侧 sherpa-onnx VITS 兜底」→ VITS 中文模型
- **U63（人工授权，2026-10-24）声纹识别**：计划书未定义，按开源借鉴取
  3D-Speaker CAM++ 说话人 embedding（Apache-2.0），见 decisions.U63。

为什么这些模型必须单独下载
--------------------------
`sherpa-onnx` 只带推理代码，**不带权重**。权重大小从 2 MB（VAD）到 100+ MB（TTS），
放进 git 与 wheel 都不合适。因此与 P2 的 bge 模型同一策略：按文件清单直连镜像拉取。

网络约束（实测，见 MEMORY「环境注意事项」）
-----------------------------------------
github.com 全域不通，**hf-mirror.com 可达**，故 MIRROR 默认取 hf-mirror，
可用 HF_MIRROR 环境变量覆盖。

用法
----
    .venv\\Scripts\\python.exe scripts\\download_speech_models.py
    .venv\\Scripts\\python.exe scripts\\download_speech_models.py --only vad，speaker
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
MIRROR = os.environ.get("HF_MIRROR", "https://hf-mirror.com").rstrip("/")

#: 目录名 → (HF 仓库, 需要的文件)。目录名即 models/speech/<目录名>。
MODELS: dict[str, tuple[str, tuple[str, ...]]] = {
    # VAD：silero_vad 约 2 MB，端侧可随包 —— 未登录也必须能本地判断「有没有人在说」
    "vad": ("csukuangfj/vad", ("silero_vad.onnx",)),
    # 流式 ASR：中文 14M 参数，int8 全套约 20 MB，端侧可随包
    "asr-streaming-zh": (
        "csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23",
        ("encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx",
         "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt"),
    ),
    # 声纹：CAM++ 中文说话人 embedding（U63）。约 28 MB，**只在云端跑**
    "speaker": ("csukuangfj/speaker-embedding-models",
                ("3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",)),
    # TTS：VITS 中文多说话人（U21 的端侧兜底形态）。体积大，默认只在云端跑
    "tts-zh": ("csukuangfj/sherpa-onnx-vits-zh-ll",
               ("model.onnx", "tokens.txt", "lexicon.txt",
                "dict/jieba.dict.utf8", "dict/hmm_model.utf8",
                "dict/user.dict.utf8", "dict/idf.utf8", "dict/stop_words.utf8")),
}


def repo_files(repo: str) -> set[str]:
    response = httpx.get(f"{MIRROR}/api/models/{repo}", timeout=30)
    response.raise_for_status()
    return {s["rfilename"] for s in response.json().get("siblings", [])}


def download(name: str, repo: str, files: tuple[str, ...]) -> int:
    target = ROOT / "models" / "speech" / name
    target.mkdir(parents=True, exist_ok=True)
    available = repo_files(repo)
    total = 0
    with httpx.Client(timeout=900, follow_redirects=True) as client:
        for filename in files:
            if filename not in available:
                print(f"  [跳过] {filename} 不在仓库 {repo} 中")
                continue
            dest = target / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size > 0:
                total += dest.stat().st_size
                print(f"  [已存在] {filename}")
                continue
            response = client.get(f"{MIRROR}/{repo}/resolve/main/{filename}")
            response.raise_for_status()
            dest.write_bytes(response.content)
            total += len(response.content)
            print(f"  {filename:52s} {len(response.content) / 1024 / 1024:8.1f} MB")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 P7 语音模型")
    parser.add_argument("--only", default="",
                        help="只下载指定目录，逗号分隔：vad,asr-streaming-zh,speaker,tts-zh")
    args = parser.parse_args(argv)
    wanted = [w.strip() for w in args.only.split(",") if w.strip()]

    failures: list[str] = []
    for name, (repo, files) in MODELS.items():
        if wanted and name not in wanted:
            continue
        print(f"=== {name} ({repo}) ===")
        started = time.time()
        try:
            total = download(name, repo, files)
        except Exception as exc:                      # noqa: BLE001 —— 逐项报告，不中断其余模型
            print(f"  [失败] {type(exc).__name__}: {exc}")
            failures.append(name)
            continue
        print(f"  合计 {total / 1024 / 1024:.1f} MB，耗时 {time.time() - started:.0f}s")

    if failures:
        print("以下模型下载失败：" + "、".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
