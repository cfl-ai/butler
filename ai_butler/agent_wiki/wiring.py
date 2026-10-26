"""Agent-Wiki 的生产装配点（U115）：编译流水线 + `wiki_digest` 注入。

要解决的问题
------------
编译流水线（`compiler.py`）与存储层（`workspace.py`）都早就有实现、有测试，
但**生产里没有一个调用点** —— `WikiCompiler` 只在 `scripts/smoke_p0.py` 里被构造过。
于是 `wiki_compile_auto_run=true` 是空转的：机器缓存（pages.json / claims.jsonl /
agent-digest.json）从来不会在生产里生成，计划书三、P3 要求的 State 必填字段
`wiki_digest` 也从来没人产出。机制在、链路空——E-86 的老形状。

三种「跑通」的含义，分别落地
--------------------------
1. **编译跑通**：`compile_workspace()` 在应用启动时执行一次（后台、失败不阻断服务）；
2. **读取跑通**：`WikiDigestProvider` 按 user_id 合并「公共 Workspace + 本人 Vault」
   的声明，产出受 `wiki_max_digest_size` 约束的 digest；
3. **消费跑通**：digest 注入对话提示词（`retrieval/chat.py`）与 `plan` 事件，
   让「已知事实」真的出现在模型与用户视野里 —— 生成了却没人读，等于没生成。

缓存与新鲜度
------------
每次提问都重编译整个库是不可接受的（写盘 + 全量解析）。所以按**目录 mtime 签名**
判定是否需要重编译：签名不变就复用上次结果。签名取自各类型子目录的 mtime，
因为页面文件写在 `workspace/<type>/*.md` 下，父目录 mtime 不会跟着变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_butler.agent_wiki.compiler import CompileReport, WikiCompiler
from ai_butler.agent_wiki.ontology import CLAIM, PAGE_TYPES
from ai_butler.agent_wiki.workspace import WikiStore


class WikiWiringError(RuntimeError):
    """装配失败。**不吞**：调用方据此如实降级（回答照给，说明没注入事实）。"""


def build_wiki_store(settings: Any) -> WikiStore:
    """按配置构造 Wiki 存储。唯一装配点。"""
    root = getattr(settings, "wiki_root", "") or "data/dev/agent_wiki"
    return WikiStore(Path(root), settings=settings)


def build_wiki_compiler(settings: Any, store: WikiStore | None = None) -> WikiCompiler:
    return WikiCompiler(store or build_wiki_store(settings))


def compile_on_startup(settings: Any, store: WikiStore | None = None) -> dict[str, Any]:
    """启动时编译一次公共 Workspace。**失败只记录，不阻断启动**。

    为什么只编译公共库：私有 Vault 属于各账号，启动时全量遍历等于把所有用户的
    私有数据读一遍 —— 既慢又踩隔离边界。私有库按需（提问时）编译。
    """
    if not getattr(settings, "wiki_compile_auto_run", False):
        return {"skipped": "wiki_compile_auto_run=false：编译流水线未启用"}
    try:
        report = build_wiki_compiler(settings, store).compile_workspace()
    except Exception as exc:                      # noqa: BLE001 —— 编译失败不阻断服务
        return {"error": f"{type(exc).__name__}: {exc}"}
    return report.as_dict()


def _dir_signature(base: Path) -> tuple[Any, ...]:
    """各类型子目录的 (存在, mtime_ns) 元组 —— 页面落盘会让它变化。"""
    sig: list[Any] = []
    for page_type in PAGE_TYPES:
        folder = base / page_type
        try:
            sig.append((page_type, folder.stat().st_mtime_ns) if folder.is_dir()
                       else (page_type, 0))
        except OSError:                            # 权限/竞争：视作不可读，宁可重编译
            sig.append((page_type, -1))
    return tuple(sig)


@dataclass
class DigestSnapshot:
    digest: dict[str, Any]
    signature: tuple[Any, ...]
    note: str = ""
    report: dict[str, Any] = field(default_factory=dict)


class WikiDigestProvider:
    """产出注入对话的 `wiki_digest`（计划书三、P3 State 必填字段）。

    返回结构**沿用 `WikiCompiler.build_digest`**（claims + max_digest_size +
    truncated），另加 `scopes` 说明本次合并了哪几个作用域 —— 隔离范围必须可见，
    否则「怎么只有公共知识」这种问题会变成无从排查的玄学。
    """

    def __init__(self, settings: Any, *, store: WikiStore | None = None) -> None:
        self.settings = settings
        self.store = store or build_wiki_store(settings)
        self._cache: dict[str, DigestSnapshot] = {}
        #: 上一次编译时各作用域的目录签名。**运行期新写的页面不会被启动时的编译覆盖**，
        #: 所以编译必须跟着读取走，否则机器缓存会永远停在启动那一刻。
        self._compiled: dict[str, tuple[Any, ...]] = {}

    # ---------------- 内部 ----------------
    def _recompile_if_changed(self, scope: str, base: Path, user_id: str | None) -> str:
        """签名变化才重编译。返回说明（空 = 无需编译或成功）。"""
        if not getattr(self.settings, "wiki_compile_auto_run", False):
            return ""
        signature = _dir_signature(base)
        if self._compiled.get(scope) == signature:
            return ""
        try:
            compiler = WikiCompiler(self.store)
            if user_id:
                compiler.compile_vault(user_id)
            else:
                compiler.compile_workspace()
        except Exception as exc:                  # noqa: BLE001 —— 编译失败不影响读取
            return f"Wiki 编译失败（{type(exc).__name__}）：机器缓存可能过期"
        self._compiled[scope] = signature
        return ""

    # ---------------- 对外 ----------------
    def __call__(self, user_id: str) -> DigestSnapshot:
        uid = (user_id or "").strip()
        try:
            base_pairs = [("workspace", self.store.workspace_dir)]
            if uid:
                base_pairs.append(("vault", self.store.vault_dir(uid)))
        except Exception as exc:                  # noqa: BLE001 —— 停用/非法作用域
            return DigestSnapshot(digest={}, signature=(),
                                  note=f"Wiki 作用域不可用：{exc}")
        signature = tuple((name, _dir_signature(base)) for name, base in base_pairs)

        cached = self._cache.get(uid)
        if cached is not None and cached.signature == signature:
            return cached

        # 缓存未命中 = 库有变化（或首次读取）：先把机器缓存追平，再读页面算 digest
        compile_notes = [self._recompile_if_changed(
            name, base, uid if name == "vault" else None) for name, base in base_pairs]

        try:
            pages = (self.store.list_pages_for_retrieval(uid) if uid
                     else self.store.list_pages())
        except Exception as exc:                  # noqa: BLE001
            snapshot = DigestSnapshot(digest={}, signature=signature,
                                      note=f"Wiki 读取失败：{type(exc).__name__}: {exc}")
            self._cache[uid] = snapshot
            return snapshot

        claims = [p for p in pages if p.type == CLAIM]
        compiler = WikiCompiler(self.store)
        digest, _size, _truncated = compiler.build_digest(claims)
        digest["scopes"] = [name for name, _ in base_pairs]
        digest["user_id"] = uid or None
        note_parts = [n for n in compile_notes if n]
        if not claims:
            note_parts.append("Wiki 里还没有声明页（claim），本次未注入已知事实")
        snapshot = DigestSnapshot(digest=digest, signature=signature,
                                  note="；".join(note_parts))
        self._cache[uid] = snapshot
        return snapshot

    # ---------------- 运维入口 ----------------
    def compile_scope(self, user_id: str | None = None) -> CompileReport:
        """显式编译一次（私有库在提问后由调用方决定何时触发，不在这里自动跑）。"""
        compiler = WikiCompiler(self.store)
        return compiler.compile_vault(user_id) if user_id else compiler.compile_workspace()

    def invalidate(self, user_id: str = "") -> None:
        self._cache.pop((user_id or "").strip(), None)


__all__ = [
    "DigestSnapshot",
    "WikiDigestProvider",
    "WikiWiringError",
    "build_wiki_compiler",
    "build_wiki_store",
    "compile_on_startup",
]
