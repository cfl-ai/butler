"""账号注册表：账号密码的加盐哈希存储（计划书三、P0-1「账号密码加盐哈希存储」）。

为什么是文件而不是数据库表
--------------------------
计划书三、P0-1 只规定了「加盐哈希存储、数据库不保存明文密码」，
**未定义账号表的结构与归属域**。台账 U42 记录：当前实现为
**存储根下的一个 JSON 文件**（storage_root/users.json），
每个账号只存 bcrypt 哈希，不存明文。

这是实现形态的选择，不改变语义：**密码永远不落明文**。
后续若接入数据库，只需替换本模块的 load/save，接口不变。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_butler.core.security import hash_password, verify_password


class UserError(ValueError):
    """账号相关错误。"""


class UserRegistry:
    """账号注册表。文件即真值源，进程内不缓存口令。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---------------- 读写 ---------------- #
    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---------------- 对外 ---------------- #
    def register(self, *, user_id: str, password: str) -> None:
        uid = str(user_id or "").strip()
        if not uid:
            raise UserError("账号不得为空")
        if len(str(password or "")) < 8:
            raise UserError("密码至少 8 位")
        data = self._load()
        if uid in data:
            raise UserError(f"账号 {uid!r} 已存在")
        # 只存哈希：明文密码不落任何持久化载体
        data[uid] = {"password_hash": hash_password(password)}
        self._save(data)

    def verify(self, *, user_id: str, password: str) -> bool:
        """校验账号口令。账号不存在与口令错误返回同一个结果（不泄露账号是否存在）。"""
        record = self._load().get(str(user_id or "").strip())
        if not record:
            # 仍做一次哈希运算，避免用响应时间区分「账号不存在」与「口令错误」
            verify_password(str(password or ""), hash_password("dummy-password-for-timing"))
            return False
        return verify_password(str(password or ""), str(record.get("password_hash", "")))
