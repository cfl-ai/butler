"""下载 P2 所需的本地模型。

裁定依据：decisions.U10（人工授权）
- Embedding：BAAI/bge-small-zh-v1.5（512 维，约 92 MB）
- Rerank   ：BAAI/bge-reranker-base（约 1.07 GB）

为什么不用 huggingface_hub 的 snapshot_download：
本机沙箱下 tempfile.mkdtemp 生成的目录不可写也不可删（见 MEMORY.md E-03），
而 huggingface_hub 的下载暂存依赖它，会中途失败。因此这里直接按文件 HTTP 拉取，
落到普通目录（工作区内），并用仓库真实文件清单驱动，避免列出不存在的文件。

用法：.venv\\Scripts\\python.exe scripts\\download_models.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
MIRROR = "https://hf-mirror.com"

MODELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bge-small-zh-v1.5": ("BAAI/bge-small-zh-v1.5", (
        "config.json", "config_sentence_transformers.json", "modules.json",
        "sentence_bert_config.json", "model.safetensors", "tokenizer.json",
        "tokenizer_config.json", "special_tokens_map.json", "vocab.txt",
        "1_Pooling/config.json")),
    "bge-reranker-base": ("BAAI/bge-reranker-base", (
        "config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json")),
}


def repo_files(repo: str) -> set[str]:
    r = httpx.get(f"{MIRROR}/api/models/{repo}", timeout=30)
    r.raise_for_status()
    return {s["rfilename"] for s in r.json().get("siblings", [])}


def download(name: str, repo: str, files: tuple[str, ...]) -> int:
    target = ROOT / "models" / name
    target.mkdir(parents=True, exist_ok=True)
    available = repo_files(repo)
    total = 0
    with httpx.Client(timeout=600, follow_redirects=True) as client:
        for filename in files:
            if filename not in available:
                print(f"  [跳过] {filename} 不在仓库中")
                continue
            dest = target / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size > 0:
                total += dest.stat().st_size
                print(f"  [已存在] {filename}")
                continue
            resp = client.get(f"{MIRROR}/{repo}/resolve/main/{filename}")
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            total += len(resp.content)
            print(f"  {filename:34s} {len(resp.content)/1024/1024:8.1f} MB")
    return total


def main() -> int:
    for name, (repo, files) in MODELS.items():
        print(f"=== {name} ({repo}) ===")
        started = time.time()
        total = download(name, repo, files)
        print(f"  合计 {total/1024/1024:.1f} MB，耗时 {time.time()-started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
