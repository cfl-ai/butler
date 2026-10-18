"""Agent-Wiki v2 运行时存储层：公共 Workspace / 用户私有 Vault。

计划书依据
----------
- 三、P0-1「Agent-Wiki v2 运行时存储层：公共 Workspace 存储全系统公开素材；
  每个用户独立私有 Vault，绑定 user_id；提供编译流水线，输入 Markdown 页面输出机器可读缓存，
  自动检测声明、证据、矛盾、过期素材」
- 一、项目概述「区分公共 Workspace（全用户共享公开知识）与用户私有 Vault（绑定 user_id）」
- 三、P2「检索入参携带 user_id；合并返回公共 Workspace + 当前用户私有 Vault 内容；
  **不会加载其他用户私有向量与 Wiki 页面**」
- 三、P1「素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID」
- 七、17「区分公共 Workspace（全用户可读）、用户私有 Vault（user_id 绑定，互相隔离）」
- 八、wiki_public_workspace_enable / wiki_user_vault_per_user

开源借鉴依据
-----------
- U19：公共库与私有库**共用同一套 schema**，靠作用域 + user_id 隔离，不建两套结构。
  该做法参考 cognee 的 NodeSet 分组（同图多主题隔离）。
"""

from __future__ import annotations

from pathlib import Path

from ai_butler.agent_wiki.ontology import PAGE_TYPES, WikiPage, validate

SCOPE_WORKSPACE = "workspace"
SCOPE_VAULT = "vault"


class VaultIsolationError(RuntimeError):
    """跨账号访问或缺失 user_id。计划书：禁止跨用户访问，user_id 不得为空。"""


class WikiStore:
    def __init__(self, root: str | Path, *, settings) -> None:
        self.root = Path(root)
        self.settings = settings

    # ---------------- 作用域解析 ----------------
    @property
    def workspace_dir(self) -> Path:
        if not self.settings.wiki_public_workspace_enable:
            raise VaultIsolationError("wiki_public_workspace_enable=false，公共 Workspace 已停用")
        return self.root / SCOPE_WORKSPACE

    def vault_dir(self, user_id: str | None) -> Path:
        if not self.settings.wiki_user_vault_per_user:
            raise VaultIsolationError("wiki_user_vault_per_user=false，用户私有 Vault 已停用")
        if user_id is None or not str(user_id).strip():
            raise VaultIsolationError(
                "【行级隔离】私有 Vault 必须携带 user_id。计划书三、P0-1："
                "每个用户独立私有 Vault，绑定 user_id；不允许硬编码、不能为空。"
            )
        return self.root / SCOPE_VAULT / str(user_id)

    def _dir_for(self, scope: str, user_id: str | None) -> Path:
        return self.workspace_dir if scope == SCOPE_WORKSPACE else self.vault_dir(user_id)

    @staticmethod
    def _scope_of(user_id: str | None) -> str:
        return SCOPE_VAULT if user_id else SCOPE_WORKSPACE

    # ---------------- 写入 ----------------
    def write_page(self, page: WikiPage, *, user_id: str | None = None) -> Path:
        """写入页面。作用域由 user_id 决定，且与页面自带的 user_id 必须一致。

        - user_id 为 None → 写公共 Workspace，页面**不得**带 user_id
          （计划书：素材溯源库属于公共 Workspace，无用户 ID）；
        - user_id 给定 → 写该用户私有 Vault，页面 user_id 必须等于它
          （防止把 A 的数据写进 B 的库）。
        """
        validate(page)
        scope = self._scope_of(user_id)
        if scope == SCOPE_WORKSPACE:
            if page.user_id:
                raise VaultIsolationError(
                    f"【公私域隔离】公共 Workspace 页面不得携带 user_id，实际 {page.user_id!r}"
                )
        else:
            if page.user_id != user_id:
                raise VaultIsolationError(
                    f"【行级隔离】页面 user_id={page.user_id!r} 与目标 Vault 账号 {user_id!r} 不一致"
                )
        target = self._dir_for(scope, user_id) / page.type
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{page.id}.md"
        path.write_text(page.to_markdown(), encoding="utf-8")
        return path

    def delete_page(self, page_id: str, *, user_id: str | None = None) -> bool:
        scope = self._scope_of(user_id)
        for page_type in PAGE_TYPES:
            path = self._dir_for(scope, user_id) / page_type / f"{page_id}.md"
            if path.exists():
                path.unlink()
                return True
        return False

    # ---------------- 读取 ----------------
    def read_page(self, page_id: str, *, user_id: str | None = None) -> WikiPage | None:
        """只读指定作用域：user_id 为空读公共 Workspace，否则读该用户 Vault。"""
        scope = self._scope_of(user_id)
        base = self._dir_for(scope, user_id)
        if not base.exists():
            return None
        for page_type in PAGE_TYPES:
            path = base / page_type / f"{page_id}.md"
            if path.exists():
                return WikiPage.parse(path.read_text(encoding="utf-8"))
        return None

    def list_pages(self, *, user_id: str | None = None, page_type: str | None = None) -> list[WikiPage]:
        """列出指定作用域的页面。绝不跨账号。"""
        scope = self._scope_of(user_id)
        base = self._dir_for(scope, user_id)
        return self._collect(base, page_type)

    def list_pages_for_retrieval(self, user_id: str, *, page_type: str | None = None) -> list[WikiPage]:
        """检索语义：合并「公共 Workspace + 当前用户私有 Vault」。

        计划书三、P2 明确：合并返回公共 Workspace + 当前用户私有 Vault 内容，
        **不会加载其他用户私有 Wiki 页面** —— 本方法只读这两个作用域，不遍历 vault 目录。
        """
        if user_id is None or not str(user_id).strip():
            raise VaultIsolationError("【行级隔离】检索必须携带 user_id（计划书三、P2）")
        pages = list(self._collect(self.workspace_dir, page_type))
        pages += list(self._collect(self.vault_dir(user_id), page_type))
        return pages

    @staticmethod
    def _collect(base: Path, page_type: str | None) -> list[WikiPage]:
        if not base.exists():
            return []
        types = (page_type,) if page_type else PAGE_TYPES
        out: list[WikiPage] = []
        for t in types:
            folder = base / t
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.md")):
                out.append(WikiPage.parse(path.read_text(encoding="utf-8")))
        return out
