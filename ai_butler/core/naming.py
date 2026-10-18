"""命名规范 —— 全项目标识的单一真值源。

计划书依据
----------
- 文档标题：《AI管家Agent生态体系项目计划书》（封面：管家 Agent 生态体系项目计划书）
- 三、P0-1「输出统一架构规范文档，全项目强制执行」
- 六、全体系可复用资产库（长期生态复用）
- 五、模块定位「点餐、售后等其他垂类直接复制本模块结构，替换业务逻辑、工具集即可快速接入」

命名派生规则（命名根取自计划书标题）
----------------------------------
| 标识       | 取值                   | 派生自                         |
| ROOT       | ai-butler              | 标题「AI管家」                  |
| ECOSYSTEM  | ai-butler-ecosystem    | 标题「AI管家 Agent 生态体系」    |
| PACKAGE    | ai_butler              | ROOT 的下划线形式               |
| NAMESPACE  | aibutler               | 资源前缀：Redis 键、容器名       |
| ENV_PREFIX | AIBUTLER_              | 应用级环境变量前缀              |
| DB_NAME    | ai_butler              | 与包名一致                      |

层名同样取自计划书自身用语：core（底座）/ infra（AI Infra 架构底座）/ middleware（中间件）。

可调整
------
后期改名只需改本文件或设置 AIBUTLER_ROOT / AIBUTLER_ECOSYSTEM / AIBUTLER_NAMESPACE，
业务代码不出现任何硬编码标识。

可复用
------
垂类资源统一走 vertical_key("travel", ...) / vertical_container("travel", ...)。
计划书五要求点餐、售后等垂类复用同一套结构，因此新增垂类**不新增命名规则**。
"""

from __future__ import annotations

import os

ROOT: str = os.environ.get("AIBUTLER_ROOT", "ai-butler")
ECOSYSTEM: str = os.environ.get("AIBUTLER_ECOSYSTEM", f"{ROOT}-ecosystem")
PACKAGE: str = "ai_butler"
NAMESPACE: str = os.environ.get("AIBUTLER_NAMESPACE", "aibutler").strip().lower()
ENV_PREFIX: str = "AIBUTLER_"
DB_NAME: str = "ai_butler"


def redis_key(*parts: object) -> str:
    """统一资源键：{NAMESPACE}:{part}:{part}..."""
    return ":".join([NAMESPACE, *[str(p) for p in parts]])


def vertical_key(vertical: str, *parts: object) -> str:
    """垂类命名空间。新增垂类只传垂类名，不新增命名规则（计划书五）。"""
    return redis_key(vertical, *parts)


def container(suffix: str) -> str:
    return f"{NAMESPACE}-{suffix}"


def env(name: str) -> str:
    return f"{ENV_PREFIX}{name}"


def describe() -> dict[str, str]:
    """供自检与文档生成使用。"""
    return {
        "ROOT": ROOT, "ECOSYSTEM": ECOSYSTEM, "PACKAGE": PACKAGE,
        "NAMESPACE": NAMESPACE, "ENV_PREFIX": ENV_PREFIX, "DB_NAME": DB_NAME,
    }
