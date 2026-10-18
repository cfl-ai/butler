"""P1 数据中台 —— 四大核心数据表与字段规范。

计划书依据
----------
- 三、P1「核心能力：结构化存储、增量学习、权重衰减、冲突校验、版本溯源；
  对接 Agent-Wiki v2 本体，增加实体-声明-证据-矛盾追踪；
  严格区分公共 Workspace / 用户私有 Vault，user_id 行级隔离」
- 三、P1「构建四大核心数据表：**用户画像库、知识图谱库、素材溯源库、设备状态库**」
- 三、P1 表注「全部业务数据表增加 user_id；素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID；
  用户画像、设备状态、私有 Wiki Vault 必须绑定 user_id」
- 三、P1「定义字段规范、时效规则、权重规则、冲突标记规则；区分公共数据域、用户私有数据域」
- 三、P1「公共域：景点、天气、公开资讯，全账号可读；
  私有域：用户偏好、对话、消费、设备信息、私有 Vault，严格归属 user_id，禁止跨用户访问」

域归属（逐表对照计划书）
----------------------
| 表 | 域 | 隔离标记 | 计划书依据 |
| 用户画像库 | 私有 | UserScoped | 「用户画像…必须绑定 user_id」 |
| 知识图谱库 | **双域** | DomainScoped | 计划书未点名 → decisions.U24 |
| 素材溯源库 | 公共 | Base（无 user_id） | 「素材溯源库…无用户 ID」 |
| 设备状态库 | 私有 | UserScoped | 「设备状态…必须绑定 user_id」 |

字段规范中的实现参数
------------------
confidence 无默认值（不得凭空造置信度）；weight 默认 1.0、version 默认 1、
conflict_status 默认 none —— 均为实现参数，非计划书数值。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ai_butler.core.db import Base, DomainScoped, UserScoped

# --------------------------------------------------------------------------- #
# 冲突标记规则（计划书三、P1「冲突校验」「冲突标记规则」）
# --------------------------------------------------------------------------- #
CONFLICT_NONE = "none"
CONFLICT_PENDING = "pending"          # 冲突待人工复核（计划书 P1「冲突人工复核」）
CONFLICT_RESOLVED = "resolved"
CONFLICT_STATUSES: tuple[str, ...] = (CONFLICT_NONE, CONFLICT_PENDING, CONFLICT_RESOLVED)

# --------------------------------------------------------------------------- #
# 时效规则（计划书三、P1「时效规则」；三、P2「过期来源标记为 status:archived」）
# --------------------------------------------------------------------------- #
# 来源状态是 Agent-Wiki 的概念，常量归属本体；此处只做再导出，避免两处重复定义。
from ai_butler.agent_wiki.ontology import (      # noqa: E402
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    SOURCE_STATUSES,
)

# --------------------------------------------------------------------------- #
# 数据域取值（供双域表显式标注，与 DomainScoped 的 user_id 语义一致）
# --------------------------------------------------------------------------- #
DOMAIN_PUBLIC = "public"
DOMAIN_PRIVATE = "private"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataFieldSpec:
    """字段规范：时效规则 + 权重规则 + 冲突标记规则 + 版本溯源。

    计划书三、P1 要求这四类规则，但未给出字段名——字段命名为实现层约定。
    """

    #: 置信度：计划书三、P1「自动标记置信度」。**不设默认值**，避免凭空造数。
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    #: 权重：计划书三、P1「权重规则」「用户偏好权重衰减机制」。初始满权重。
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    #: 时效规则：有效期区间，NULL 表示长期有效
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: 冲突标记规则
    conflict_status: Mapped[str] = mapped_column(String(16), nullable=False,
                                                 default=CONFLICT_NONE, index=True)
    conflict_group: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    #: 溯源：关联素材溯源库 / Agent-Wiki Evidence 条目（计划书 P1「素材溯源」「证据绑定」）
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    #: 版本溯源（计划书三、P1「版本溯源」「数据版本溯源、变更审计、回溯系统」）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- #
# 1. 用户画像库（私有域）
# --------------------------------------------------------------------------- #
class UserProfileRecord(Base, UserScoped, DataFieldSpec):
    """用户画像库。

    计划书三、P1 表注：「用户画像…必须绑定 user_id」；
    表注另要求「用户画像实体同步至私有 Vault」——同步逻辑见 sync 模块（P1 后续步骤）。
    """

    __tablename__ = "data_user_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 画像属性名，如「偏好出行方式」
    attribute: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    #: 画像属性值
    value: Mapped[str] = mapped_column(Text, nullable=False)
    #: 归属类别，便于按域检索
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    #: 三、P1 表注「用户画像实体同步至私有 Vault」——本记录对应的 Vault 实体页面 id
    vault_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


# --------------------------------------------------------------------------- #
# 2. 知识图谱库（双域，见 decisions.U24）
# --------------------------------------------------------------------------- #
class KnowledgeGraphRecord(Base, DomainScoped, DataFieldSpec):
    """知识图谱库 —— 实体-关系-声明三元组。

    计划书三、P1「对接 Agent-Wiki v2 本体，增加实体-声明-证据-矛盾追踪」：
    subject/predicate/object 对应 Wiki 的 Entity 与 Relation，
    statement 对应 Claim，source_id 对应 Evidence，conflict_* 对应 Contradiction。

    双域：user_id 为 NULL 即公共域（景点、天气、公开资讯），非空即私有域（用户偏好）。
    """

    __tablename__ = "data_knowledge_graph"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    object: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    #: 对应的 Claim 声明文本（计划书 P1「每一条增量事实生成 Claim 声明」）
    statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 对应的 Agent-Wiki Claim 页面 id，供证据链回溯
    claim_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


# --------------------------------------------------------------------------- #
# 3. 素材溯源库（公共域，无 user_id）
# --------------------------------------------------------------------------- #
class MaterialSourceRecord(Base):
    """素材溯源库。

    计划书三、P1 表注：「素材溯源库属于 Agent-Wiki 公共 Workspace **无用户 ID**」
    ——因此本表继承 Base 而非 UserScoped，任何人可读且不受 user_id 过滤。
    """

    __tablename__ = "data_material_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 素材标识，供其他表以 source_id 引用。
    #: **不设唯一约束**：同一来源的多个版本要并存以支持「版本溯源」与「新旧版本增量融合」，
    #: 由业务层保证同一 source_key 至多一个 active 版本。
    source_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 版本号（三、P2 新旧版本增量融合；三、P1 版本溯源）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    publisher: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                   default=utcnow)
    #: 三、P2「过期来源标记为 status:archived 归档」
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SOURCE_ACTIVE,
                                        index=True)
    #: 归档时间（过期素材识别，三、P2）
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ---- 以下为 P2「清洗、去重、分类、时效打标」所需字段 ----
    #: 内容哈希，用于「去重 / 新旧版本增量融合」
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    #: 分类（景点 / 天气 / 交通 / 门票 / 公开资讯 / 其他）
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    #: 时效打标：超过该时刻即视为过期素材
    stale_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True,
                                                         index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# 4. 设备状态库（私有域）
# --------------------------------------------------------------------------- #
class DeviceStateRecord(Base, UserScoped, DataFieldSpec):
    """设备状态库。

    计划书三、P1 表注：「设备状态…必须绑定 user_id」。
    计划书三、P7「开发 GPS、设备状态定时上报；上报携带会话身份；未登录不上报用户私有设备信息」。
    """

    __tablename__ = "data_device_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 状态指标名，如 battery / location / network
    metric: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  default=utcnow, index=True)


#: 四大核心数据表（计划书三、P1 逐字点名的四张表）
CORE_TABLES: dict[str, type] = {
    "用户画像库": UserProfileRecord,
    "知识图谱库": KnowledgeGraphRecord,
    "素材溯源库": MaterialSourceRecord,
    "设备状态库": DeviceStateRecord,
}

#: 计划书三、P1 表注规定的域归属，供测试与自检核对
PLAN_DOMAIN_ASSIGNMENT: dict[str, str] = {
    "用户画像库": "private",      # 「用户画像…必须绑定 user_id」
    "知识图谱库": "dual",         # decisions.U24
    "素材溯源库": "public",       # 「素材溯源库…无用户 ID」
    "设备状态库": "private",      # 「设备状态…必须绑定 user_id」
}
