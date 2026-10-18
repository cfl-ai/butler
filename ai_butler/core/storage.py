"""P0-1 统一文件存储 · 日志存储 · 版本备份 · 项目回溯机制。

计划书依据
----------
- 三、P0-1「统一文件存储、日志存储、版本备份、项目回溯机制」
- 三、P0-1「日志脱敏规则：…保留 user_id 用于排障，剥离原始隐私内容」
- 三、P0-1「搭建数据版本溯源、变更审计、回溯系统」（三、P1 同义要求）
- 三、P0-1「会话失效直接终止 Graph 流程」（不影响本模块，此处仅列隔离要求）

作用域
------
- 全局作用域：workspace / logs / backups / exports
- 用户作用域：vault/{user_id} —— 与 Agent-Wiki 一致，操作必须携带 user_id，
           且绝不跨账号；本模块不提供任何「遍历全部用户」的入口。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCOPE_WORKSPACE = "workspace"
SCOPE_VAULT = "vault"
SCOPE_LOGS = "logs"
SCOPE_BACKUPS = "backups"
SCOPE_EXPORTS = "exports"
#: P0-2 Prompt 版本库（三、P0-2「搭建 Prompt 版本库」，属全局资产非用户私有）
SCOPE_PROMPTS = "prompts"
#: P1 冲突人工复核队列（三、P1「冲突人工复核」「Wiki 检测到矛盾自动送入冲突复核队列」）
SCOPE_REVIEW = "review"
#: P1 数据版本溯源与变更审计（三、P1「数据版本溯源、变更审计、回溯系统」）
SCOPE_AUDIT = "audit"
#: P3 会话上下文（三、P3「会话上下文管理：多轮对话状态保持」）
#: 按 user_id 分片存放，跨账号物理不可见。
SCOPE_CONVERSATIONS = "conversations"

GLOBAL_SCOPES: tuple[str, ...] = (
    SCOPE_WORKSPACE, SCOPE_LOGS, SCOPE_BACKUPS, SCOPE_EXPORTS, SCOPE_PROMPTS, SCOPE_REVIEW,
    SCOPE_AUDIT, SCOPE_CONVERSATIONS,
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._\-\u4e00-\u9fff]{1,128}$")


class StorageIsolationError(RuntimeError):
    """越权路径、跨账号访问或非法名称。"""


def _check_name(part: str) -> str:
    if not isinstance(part, str) or not _SAFE_NAME.match(part) or part in (".", ".."):
        raise StorageIsolationError(
            f"非法路径片段 {part!r}：禁止路径穿越、分隔符注入与空名"
        )
    return part


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FileStore:
    """统一文件存储：所有落盘路径都必须经过本模块。"""

    def __init__(self, root: str | Path, *, settings) -> None:
        self.root = Path(root)
        self.settings = settings

    # ---------------- 路径解析 ----------------
    def scope_dir(self, scope: str, *, user_id: str | None = None) -> Path:
        if scope not in GLOBAL_SCOPES + (SCOPE_VAULT,):
            raise StorageIsolationError(f"未知作用域 {scope!r}")
        if scope == SCOPE_VAULT:
            if user_id is None or not str(user_id).strip():
                raise StorageIsolationError(
                    "【行级隔离】用户私有存储必须携带 user_id，不允许为空"
                )
            return self.root / SCOPE_VAULT / _check_name(str(user_id))
        if user_id is not None:
            raise StorageIsolationError(
                f"全局作用域 {scope} 不得携带 user_id（计划书：公共域无用户 ID）"
            )
        return self.root / scope

    def path(self, scope: str, *parts: str, user_id: str | None = None) -> Path:
        base = self.scope_dir(scope, user_id=user_id)
        for part in parts:
            base = base / _check_name(part)
        return base

    # ---------------- 读写 ----------------
    def put_bytes(self, scope: str, parts: Iterable[str], data: bytes, *,
                  user_id: str | None = None) -> Path:
        target = self.path(scope, *parts, user_id=user_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def put_text(self, scope: str, parts: Iterable[str], text: str, *,
                 user_id: str | None = None) -> Path:
        return self.put_bytes(scope, parts, text.encode("utf-8"), user_id=user_id)

    def get_bytes(self, scope: str, parts: Iterable[str], *,
                  user_id: str | None = None) -> bytes:
        target = self.path(scope, *parts, user_id=user_id)
        if not target.is_file():
            raise FileNotFoundError(str(target))
        return target.read_bytes()

    def exists(self, scope: str, parts: Iterable[str], *, user_id: str | None = None) -> bool:
        return self.path(scope, *parts, user_id=user_id).exists()

    def list_names(self, scope: str, *, user_id: str | None = None) -> list[str]:
        base = self.scope_dir(scope, user_id=user_id)
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir())

    def delete(self, scope: str, parts: Iterable[str], *, user_id: str | None = None) -> bool:
        target = self.path(scope, *parts, user_id=user_id)
        if target.is_dir():
            shutil.rmtree(target)
            return True
        if target.is_file():
            target.unlink()
            return True
        return False

    # ---------------- 日志存储 ----------------
    def log_path(self, name: str = "aibutler.log") -> Path:
        target = self.path(SCOPE_LOGS, _check_name(name))
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


# --------------------------------------------------------------------------- #
# 版本备份 + 项目回溯
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BackupManifest:
    version: str
    label: str
    created_at: str
    sources: tuple[str, ...]                 # 形如 "workspace" 或 "vault:user-A"
    files: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    total_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BackupManager:
    """版本备份与项目回溯。备份产物存放在全局 backups 作用域。"""

    def __init__(self, store: FileStore) -> None:
        self.store = store

    def _version_dir(self, version: str) -> Path:
        return self.store.path(SCOPE_BACKUPS, _check_name(version))

    # ---------------- 备份 ----------------
    def create(self, *, label: str, sources: Iterable[str]) -> BackupManifest:
        label = _check_name(label)
        source_list = tuple(dict.fromkeys(str(s) for s in sources))
        if not source_list:
            raise ValueError("备份来源不得为空")

        created = _now_iso()
        version = f"{created.replace(':', '').replace('-', '')}-{hashlib.sha256(label.encode()).hexdigest()[:6]}"
        version_dir = self._version_dir(version)
        tree = version_dir / "tree"
        files: list[dict[str, Any]] = []

        for source in source_list:
            scope, _, uid = source.partition(":")
            base = self.store.scope_dir(scope, user_id=uid or None)
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(base)
                dest = tree / source.replace(":", "_") / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                files.append({"source": source, "relpath": str(rel).replace("\\", "/"),
                              "sha256": _sha256(dest), "size": dest.stat().st_size})

        manifest = BackupManifest(
            version=version, label=label, created_at=created,
            sources=source_list, files=tuple(files),
            total_bytes=sum(f["size"] for f in files),
        )
        version_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / "manifest.json").write_text(
            json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    # ---------------- 查询 ----------------
    def list_versions(self) -> list[BackupManifest]:
        base = self.store.scope_dir(SCOPE_BACKUPS)
        if not base.is_dir():
            return []
        out: list[BackupManifest] = []
        for path in sorted(base.glob("*/manifest.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(BackupManifest(
                version=data["version"], label=data["label"], created_at=data["created_at"],
                sources=tuple(data["sources"]), files=tuple(data["files"]),
                total_bytes=data["total_bytes"]))
        return out

    def verify(self, version: str) -> list[str]:
        """校验备份完整性，返回不一致项（空表示完好）。"""
        manifest = self._manifest(version)
        problems: list[str] = []
        tree = self._version_dir(version) / "tree"
        for record in manifest.files:
            path = tree / record["source"].replace(":", "_") / record["relpath"]
            if not path.is_file():
                problems.append(f"缺失文件 {record['relpath']}")
            elif _sha256(path) != record["sha256"]:
                problems.append(f"内容不一致 {record['relpath']}")
        return problems

    def _manifest(self, version: str) -> BackupManifest:
        path = self._version_dir(version) / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError(f"版本不存在：{version!r}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return BackupManifest(version=data["version"], label=data["label"],
                              created_at=data["created_at"], sources=tuple(data["sources"]),
                              files=tuple(data["files"]), total_bytes=data["total_bytes"])

    # ---------------- 项目回溯 ----------------
    def restore(self, version: str) -> int:
        """把某版本回写为当前状态（项目回溯）。返回恢复的文件数。"""
        manifest = self._manifest(version)
        tree = self._version_dir(version) / "tree"
        restored = 0
        for record in manifest.files:
            scope, _, uid = record["source"].partition(":")
            src = tree / record["source"].replace(":", "_") / record["relpath"]
            dest = self.store.path(scope, *record["relpath"].split("/"), user_id=uid or None)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored += 1
        return restored
