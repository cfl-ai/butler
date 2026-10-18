"""开户脚本：离线创建一个账号（P7 只规定了登录，没有注册）。

为什么不做「公开注册接口」
------------------------
计划书三、P7 步骤3 只写了「账号密码登录」，**没有注册**。
若在 HTTP 上开一个匿名注册接口，等于让任何人往系统里塞账号 ——
这是本条实现最不该顺手打开的缺口。

因此账号由**运维侧离线开户**：本脚本直接写账号表，不经过网络。
移动端只有登录页，与此一致。

用法
----
    python scripts/create_account.py <账号>
    # 口令从标准输入读，不落命令行历史

环境
----
与后端使用同一份 .env（默认 .env.dev）：账号表位置由 storage_root 决定。
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_butler.core.config import load_settings   # noqa: E402
from ai_butler.core.users import UserError, UserRegistry  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python scripts/create_account.py <账号>", file=sys.stderr)
        return 2
    account = argv[1].strip()

    settings = load_settings()
    registry = UserRegistry(Path(settings.storage_root) / "users.json")

    password = getpass.getpass("口令（至少 8 位，输入不回显）: ")
    confirm = getpass.getpass("再输一次: ")
    if password != confirm:
        print("两次输入不一致", file=sys.stderr)
        return 1

    try:
        registry.register(user_id=account, password=password)
    except UserError as exc:
        print(f"开户失败：{exc}", file=sys.stderr)
        return 1

    print(f"已开户：{account}")
    print(f"账号表：{registry.path}")
    print("口令以 bcrypt 哈希存储，明文不落盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
