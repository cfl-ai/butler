"""检索意图分类与工具选择 —— 「这句话属于哪一类，该用哪条路、哪个工具去查」。

计划书依据
----------
- 一、项目概述 / 四、1「**五层意图路由**：规则匹配 → Jev → 向量 → 垂类决策模型 → 大模型兜底」
- 三、P2「搭建 BM25 + 向量检索 + Rerank 三重混合检索服务」
- 三、P2「对外统一检索 API，供规划 Agent、质检 Agent、Jev 决策节点调用」
- 三、P3「落地五层意图路由」+「实现多意图冲突处理：不自动选择优先级，必须向用户确认」
- 八、五层路由冻结（层数与顺序不得变更）

裁定
----
**U66（人工授权，2026-10-24）**：用户指令「检索信息和检索意图路由要么有具体分类
和方法选择合适工具」。落到本模块，含义是**把「怎么查」从散落的 if 里抽出来，
变成可枚举的分类 + 可点名的工具**。

三层职责必须分清，不得互相越界：

| 谁 | 回答什么问题 | 在哪 |
|---|---|---|
| 五层意图路由 | 这句话**想干什么**（`PlaceOrder` / `QueryOrder` …） | `scheduler/router.py`（**冻结**） |
| 本模块的分类 | 这句话**属于哪一类查询**（事实/规则/对比/私有…） | 这里 |
| 本模块的工具选择 | 这一类查询**该用哪个工具**（直查/BM25/向量/混合/反问） | 这里 |
| `material_center.routing` | 检索**路线**粗判（直查 / 混合 / 大模型） | U46（保留，不重复造） |

**不新增第六层**：本模块只在检索侧做细粒度决策，消费五层的结论（`intent_layer`），
不改写它。这与 U46 的边界一致 —— U46 定路线，本模块定工具。

为什么需要「具体分类」
--------------------
只按「精确标识 / 语义」二分是不够的：同样是语义查询，
「公司年假几天」要去制度库取权威条文并**带证据**，
「帮我挑个吹风机」要的是**方案生成**而不是找一段原文，
「我上次说的那个订单」要查**私有 Vault**（且必须登录）。
三类走的方法、需要的工具、失败时的动作都不同。分类就是把这些差异显式化。

为什么工具要带「能力要求」
------------------------
工具不是都能给所有人用的。U64 定了访客只能文字查询，
所以 `VAULT_SEARCH`（私有库）与 `TOOL_CALL`（业务工具）在访客层级必须**被裁掉**。
裁剪发生在规划期而不是执行期：**执行期才发现没权限，等于白花一次检索**，
而且报错会变成「查无结果」，掩盖真实原因。
被裁掉的工具要进 `dropped_tools` 并写明理由 —— 不静默消失。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence

from ai_butler.core import access
from ai_butler.material_center.routing import (
    LAYER_LLM_FALLBACK,
    QueryRoute,
    RoutingDecision,
    route_query,
)

# --------------------------------------------------------------------------- #
# 分类：具体到「决策会因此改变」的粒度，而不是百科式细分
# --------------------------------------------------------------------------- #
class QueryCategory(str, Enum):
    """查询分类。**只收「选择不同工具」的类别** —— 分得再细但不改动作就没意义。"""

    IDENTITY_LOOKUP = "identity_lookup"      # 精确标识查询：订单号 / 编号 / 版本 / 状态码
    FACT_QUESTION = "fact_question"          # 事实问答：是什么 / 多少 / 何时 / 谁
    POLICY_RULE = "policy_rule"              # 规则政策：能不能 / 算不算 / 什么条件
    PROCEDURE = "procedure"                  # 操作步骤：怎么做 / 流程 / 步骤
    COMPARISON = "comparison"                # 对比选择：A 和 B 哪个好 / 区别
    RECOMMENDATION = "recommendation"        # 推荐方案：帮我挑 / 给我个方案
    PERSONAL_RECALL = "personal_recall"      # 私有回忆：我上次 / 我的 / 之前说的
    SMALL_TALK = "small_talk"                # 闲聊寒暄：不需要检索
    MULTI_INTENT = "multi_intent"            # 多意图并存：必须反问（JEV-003）
    UNKNOWN = "unknown"                      # 判不出来：走最稳的一条路


class RetrievalMethod(str, Enum):
    """检索方法（= 工具的能力形态）。"""

    EXACT_LOOKUP = "exact_lookup"        # 精确匹配直查结构库，**不过大模型**
    BM25_KEYWORD = "bm25_keyword"        # 关键词倒排
    VECTOR_SEMANTIC = "vector_semantic"  # 纯向量语义
    HYBRID_RERANK = "hybrid_rerank"      # BM25 + 向量 → 融合 → Rerank（P2 主线）
    VAULT_SEARCH = "vault_search"        # 私有 Vault（需登录）
    TOOL_CALL = "tool_call"              # 调业务工具（查订单/天气等，需登录）
    MODEL_DIRECT = "model_direct"        # 不检索，直接由大模型组织回答
    CLARIFY = "clarify"                  # 反问用户，暂停分发


@dataclass(frozen=True)
class ToolSpec:
    """一个可选工具。`requires` 是硬门槛，不是建议。"""

    name: str
    method: RetrievalMethod
    description: str
    requires: frozenset[access.Capability] = frozenset({access.Capability.TEXT_QUERY})
    #: 是否**完全不经过大模型**（省一次调用，也不会被语义漂移带偏）
    bypasses_llm: bool = False
    #: 是否需要**证据可追溯**（用于判定「证据不足时该弃答还是该答」）
    needs_evidence: bool = True

    def available_to(self, tier: access.AccessTier) -> tuple[bool, str]:
        for capability in sorted(self.requires, key=lambda c: c.value):
            verdict = access.authorize(tier, capability)
            if not verdict.allowed:
                return False, verdict.reason
        return True, ""


#: 工具注册表。**key 即工具名**，一处登记、全链路引用，不在调用点写字符串。
TOOLS: Mapping[str, ToolSpec] = {
    spec.name: spec for spec in (
        ToolSpec(
            "structured.lookup", RetrievalMethod.EXACT_LOOKUP,
            "按精确标识（编号/日期/版本/状态码）直查结构化库与倒排索引",
            bypasses_llm=True),
        ToolSpec(
            "index.bm25", RetrievalMethod.BM25_KEYWORD,
            "关键词倒排检索（BM25）：术语、专名、短查询"),
        ToolSpec(
            "index.vector", RetrievalMethod.VECTOR_SEMANTIC,
            "向量语义检索：同义改写、口语化提问"),
        ToolSpec(
            "retrieval.hybrid_rerank", RetrievalMethod.HYBRID_RERANK,
            "P2 三重混合检索：BM25 + 向量并行 → 融合 → topN → Rerank → topK"),
        ToolSpec(
            "vault.search", RetrievalMethod.VAULT_SEARCH,
            "当前账号私有 Vault 检索（只读自己，不加载他人向量）",
            requires=frozenset({access.Capability.TEXT_QUERY,
                                access.Capability.PRIVATE_RETRIEVAL})),
        ToolSpec(
            "business.tool_call", RetrievalMethod.TOOL_CALL,
            "调业务工具取实时数据（订单/物流/账户等）",
            requires=frozenset({access.Capability.TEXT_QUERY})),
        ToolSpec(
            "model.direct", RetrievalMethod.MODEL_DIRECT,
            "不检索，直接由大模型组织回答（闲聊、通用常识）",
            needs_evidence=False),
        ToolSpec(
            "clarify.ask", RetrievalMethod.CLARIFY,
            "反问用户以消解歧义（JEV-003：不得自动选择优先级）",
            needs_evidence=False),
    )
}


@dataclass(frozen=True)
class PlannedStep:
    """规划出的一步：**先做什么、用什么工具、为什么**。"""

    order: int
    tool: str
    method: RetrievalMethod
    role: str            # primary / fallback / verify / synthesize

    def as_dict(self) -> dict[str, object]:
        return {"order": self.order, "tool": self.tool,
                "method": self.method.value, "role": self.role}


@dataclass(frozen=True)
class DroppedTool:
    tool: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"tool": self.tool, "reason": self.reason}


@dataclass(frozen=True)
class RetrievalPlan:
    query: str
    category: QueryCategory
    #: 分类依据：命中哪些信号 / 由哪一层给出。**必须说得出理由**（E-53）
    decided_by: str
    signals: tuple[str, ...] = ()
    steps: tuple[PlannedStep, ...] = ()
    dropped_tools: tuple[DroppedTool, ...] = ()
    route: QueryRoute | None = None
    needs_clarification: bool = False
    clarify_question: str = ""
    tier: access.AccessTier = access.AccessTier.GUEST
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(step.tool for step in self.steps)

    @property
    def bypasses_llm(self) -> bool:
        """**首步**是否完全不经过大模型。

        只看首步是刻意的：`bypasses_llm` 描述的是「这条规划能不能省掉模型」，
        而省不省得掉取决于**先做的那一步**。后面的 fallback 是「万一没查到」的
        补救路径，它用不用模型不影响主路径是否省。
        若要看「整条链路有没有可能用到模型」，看 `may_use_llm`。
        """
        return bool(self.steps) and TOOLS[self.steps[0].tool].bypasses_llm

    @property
    def may_use_llm(self) -> bool:
        """整条规划里**是否有任何一步**需要大模型（fallback 也算）。"""
        return any(not TOOLS[step.tool].bypasses_llm for step in self.steps)

    @property
    def needs_evidence(self) -> bool:
        return any(TOOLS[step.tool].needs_evidence for step in self.steps)

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "category": self.category.value,
            "decided_by": self.decided_by,
            "signals": list(self.signals),
            "steps": [s.as_dict() for s in self.steps],
            "tools": list(self.tools),
            "dropped_tools": [d.as_dict() for d in self.dropped_tools],
            "route": self.route.route.value if self.route else None,
            "route_reason": self.route.reason if self.route else "",
            "bypasses_llm": self.bypasses_llm,
            "needs_evidence": self.needs_evidence,
            "needs_clarification": self.needs_clarification,
            "clarify_question": self.clarify_question,
            "tier": self.tier.value,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# 分类判据：**确定性信号优先，判不出才交给五层/大模型**（与 U46 同款原则）
# --------------------------------------------------------------------------- #
#: (正则, 信号说明, 指向的分类)。顺序即优先级：越具体的类别放越前。
#:
#: 2026-10-25 按 62 句实测（8 条误判 / 19 条漏判）重整本表。每一处改动都写明
#: **语言依据**与**哪条实测暴露的**，不写「因为测试要求」——
#: 否则下一轮换语料就会漂移（E-44 同一教训）。
CATEGORY_SIGNALS: tuple[tuple[str, str, QueryCategory], ...] = (
    # ---- 私有回忆：只认「回忆性指代」，不认裸「我的」----
    # 「我的发票」「我的订单」里的「我的」是**限定语**，不是回忆；
    # 真正指向「上次说过什么」的是时间指代（我上次 / 我之前）或回忆动词（我说过 / 我提过）。
    # 旧正则把裸「我的」也收进来，导致两条真值分别是 fact / procedure 的句子
    # （「为什么我的订单还没到」「我的发票什么时候能开出来」）被抢成 personal_recall。
    (r"(我(?:上次|之前|刚才|上回)|我说过|我提过|我那个|上次那个|之前那个)", "回忆性指代",
     QueryCategory.PERSONAL_RECALL),
    # ---- 是非型规则问句：必须排在标识之前 ----
    # 标识是**对象**，规则是**诉求**。「订单 2024101200001 能改收货地址吗」问的是
    # 「能不能改」，不是「这个订单到哪了」；让「长数字编号」先命中就答非所问了。
    # 限定为**是非问句**（「吗」收尾）：不加这个限定，「我的发票什么时候能开出来」
    # 这种陈述式疑问也会被吸进来（它问的是时间，不是许不许可）。
    (r"(?:能|可以)(?:改|退|换|开|用|补|续|取消|转让|延期)[^，。；？！]{0,12}吗[？?]?$",
     "是非型规则问句", QueryCategory.POLICY_RULE),
    # ---- 精确标识：编号、日期、版本、状态码 —— 语义检索的固有盲区 ----
    (r"\d{5,}", "长数字编号", QueryCategory.IDENTITY_LOOKUP),
    (r"[A-Za-z]{2,}[-_/]?\d{2,}", "字母数字混合编号", QueryCategory.IDENTITY_LOOKUP),
    (r"\bv?\d+(\.\d+){1,3}\b", "版本号", QueryCategory.IDENTITY_LOOKUP),
    (r"(状态码|错误码|订单号|编号|流水号|型号|工单号)", "标识类字段词",
     QueryCategory.IDENTITY_LOOKUP),
    # 「查一下物流 / 查下进度」是**按可标识对象查**，不是泛语义查询。
    # 也用于让「改地址，以及查物流」凑齐两类诉求（多意图的必要条件）。
    (r"(查(?:一下|下)?(?:物流|快递|运单|进度|记录|明细|流水))", "查询动作指向可标识对象",
     QueryCategory.IDENTITY_LOOKUP),
    # ---- 对比选择 ----
    # 「哪个快 / 哪个便宜」是中文最常见的比较问法，只写「哪个好 / 哪个更」会漏掉大半
    (r"(对比|区别|差异|优缺点|哪个(?:好|快|便宜|划算|强|稳|合适)|哪个更|还是.*好|vs\.?|VS)",
     "比较诉求", QueryCategory.COMPARISON),
    # ---- 推荐方案 ----
    # 「给我个方案」漏在只有「给个方案」；「预算三千买什么」是预算约束型推荐诉求；
    # 「便宜点」是隐性推荐诉求（要更低价的选择，不是查一个事实）
    (r"(推荐|帮我挑|帮我选|给(?:我)?个?方案|有什么好的|怎么选|建议(?:我|你)?(?:选|买)"
     r"|预算[^，。；？！]{0,10}(?:买|选)|便宜(?:点|些|一点))", "推荐诉求",
     QueryCategory.RECOMMENDATION),
    # ---- 规则政策 ----
    (r"(能不能|可不可以|算不算|允许吗|什么条件|有没有限制|是否违规|合规|政策|规定)",
     "规则判定诉求", QueryCategory.POLICY_RULE),
    # ---- 操作步骤 ----
    # `怎么` 要排除「怎么样 / 怎么了」—— 那是问状况，不是问步骤，
    # 判成 PROCEDURE 会把「那个东西怎么样了」错送到流程检索（实测踩过）。
    # 但「怎么样才能申请退款」里的「怎么样才」**确实是问步骤**，所以单独放行，
    # 不能因为一个例外把整类排掉。
    # 「在哪开 / 改地址」是操作动词，旧表只有「在哪办」，漏掉「发票在哪开」。
    (r"(怎么操作|怎么(?!样|了)|怎么样(?:才|可以)|如何|步骤|流程|在哪(?:办|开|申请|查|改|退|补)"
     r"|怎么办|怎么弄|怎么申请|改(?:地址|手机|密码|邮箱|绑定))", "操作步骤诉求",
     QueryCategory.PROCEDURE),
    # ---- 事实问答 ----
    # 「多久」是最高频的事实要素之一，旧表只有「多长」，漏掉「高铁要多久」
    (r"(是什么|多少钱|多少|何时|什么时候|几天|多久|哪里|在哪|是谁|为什么|多长|是不是)",
     "事实要素", QueryCategory.FACT_QUESTION),
)

#: 闲聊：短句 + 寒暄词（允许句末语气词），且不含任何实体诉求。
_SMALL_TALK = re.compile(
    r"^(你好|您好|hi|hello|嗨|在吗|谢谢|多谢|辛苦了|早上好|晚上好|再见|拜拜|哈哈)"
    r"[呀啊哦嗯呢啦了嘛!！。~～\s]*$", re.IGNORECASE)

#: 多意图：分句符 + 并列连接词。**只用于提示需要反问**，
#: 精确的多意图判定归 Jev（U14 的 require_clarification）。
#:
#: **裸逗号刻意不收**（2026-10-25 实测后的取舍）：
#: 收进来能把「这个多少钱，怎么付款」判对（+1），但会把
#: 「给我个方案，怎么部署内网服务」判成多意图（-1）——
#: 后者是**一个诉求 + 方案细节**，反问一次会白白打断用户。
#: 净收益为 0 而体验明确变差，所以只收**显式并列/顺承连词**：
#: 「然后」也是显式顺承（「推荐一下，然后怎么下单」确实是两件事）。
_MULTI_INTENT = re.compile(r"[?？；;]|以及|并且|另外|同时|顺便|然后")

#: 客套/无实体的短句上限：超过这个长度还判成闲聊就是分类错误
SMALL_TALK_MAX_CHARS = 12


def classify(query: str, *, intent_layer: str | None = None,
             intent_label: str | None = None) -> tuple[QueryCategory, str, tuple[str, ...]]:
    """给出 (分类, 判定依据, 命中信号)。

    判定顺序固定，**先确定性后概率性**：
    闲聊 → 多意图 → 逐条信号（优先级即表序）→ 五层路由结论 → UNKNOWN。

    `intent_label` 是五层意图路由（U74）给出的标签，**只在确定性信号全部落空时
    才被采用**。顺序不能反过来：正则是可复现的判据，模型判定不是；
    让概率结论覆盖确定性结论，等于用「大概对」换掉「确定对」，
    而且会让同一条查询在不同时间得到不同工具序列（E-53 要的是「说得出理由」）。
    """
    text = (query or "").strip()
    if not text:
        return QueryCategory.UNKNOWN, "空查询，判不出分类", ()

    if len(text) <= SMALL_TALK_MAX_CHARS and _SMALL_TALK.match(text):
        return (QueryCategory.SMALL_TALK, "命中寒暄词且无实体诉求",
                ("small_talk",))

    hits: list[tuple[str, QueryCategory]] = []
    for pattern, name, category in CATEGORY_SIGNALS:
        if re.search(pattern, text):
            hits.append((name, category))

    # 多意图：出现并列连接词，且命中了两类不同信号 → 必须先反问
    if _MULTI_INTENT.search(text) and len({c for _, c in hits}) >= 2:
        return (QueryCategory.MULTI_INTENT,
                "同时命中并列连接词与两类不同诉求，按 multi_intent_auto_resolve=False 反问",
                tuple(name for name, _ in hits) + ("multi_intent",))

    if hits:
        # 表序即优先级：第一命中即主分类（设计上更具体的类别排在更泛的前面）
        return hits[0][1], f"命中信号「{hits[0][0]}」", tuple(n for n, _ in hits)

    # 确定性信号全落空：这时才用五层路由的结论（它是概率性的，必须排最后）
    if intent_label:
        try:
            category = QueryCategory(intent_label)
        except ValueError:
            # 标签不在冻结集合内：不猜，按 UNKNOWN 走最稳的路，并留痕
            return (QueryCategory.UNKNOWN,
                    f"五层路由给出未登记的标签 {intent_label!r}，按未知处理",
                    ("intent_label_invalid",))
        if category is QueryCategory.UNKNOWN:
            return (QueryCategory.UNKNOWN,
                    f"五层路由（{intent_layer or '未知层'}）亦判为 unknown",
                    (f"intent={intent_label}",))
        return (category,
                f"五层意图路由（{intent_layer or '未知层'}）判定为 {intent_label}",
                (f"intent={intent_label}", f"layer={intent_layer or ''}"))

    # 没有任何确定性信号：把决定权交给五层路由的结论，不再自己猜
    if intent_layer == LAYER_LLM_FALLBACK:
        return (QueryCategory.UNKNOWN,
                "五层路由已到 llm_fallback，分类同样无法确定", ("intent_layer=llm_fallback",))
    return QueryCategory.UNKNOWN, "无确定性信号，按语义查询处理", ()


# --------------------------------------------------------------------------- #
# 分类 → 工具序列（唯一真值表）
# --------------------------------------------------------------------------- #
#: 每类查询的步骤：(工具名, role)。**第一条必须是 primary**。
#: 顺序表达的是「先做什么、失败了再做什么」，不是并列候选。
CATEGORY_TOOLS: Mapping[QueryCategory, tuple[tuple[str, str], ...]] = {
    QueryCategory.IDENTITY_LOOKUP: (
        ("structured.lookup", "primary"),        # 先直查，不浪费一次大模型
        ("retrieval.hybrid_rerank", "fallback"),  # 编号记错了也要能兜住
    ),
    QueryCategory.FACT_QUESTION: (
        ("retrieval.hybrid_rerank", "primary"),
        ("model.direct", "synthesize"),
    ),
    QueryCategory.POLICY_RULE: (
        ("retrieval.hybrid_rerank", "primary"),   # 规则必须引条文，不能凭印象
        ("vault.search", "fallback"),             # 账号内的补充规定
    ),
    QueryCategory.PROCEDURE: (
        ("retrieval.hybrid_rerank", "primary"),
        ("index.bm25", "fallback"),               # 步骤类问题关键词很硬
    ),
    QueryCategory.COMPARISON: (
        ("retrieval.hybrid_rerank", "primary"),
        ("model.direct", "synthesize"),
    ),
    QueryCategory.RECOMMENDATION: (
        ("retrieval.hybrid_rerank", "primary"),
        ("model.direct", "synthesize"),
    ),
    QueryCategory.PERSONAL_RECALL: (
        ("vault.search", "primary"),              # 只有私有库知道他「上次」说了什么
        ("retrieval.hybrid_rerank", "fallback"),  # 访客层级会被裁掉，退到公共库
    ),
    QueryCategory.SMALL_TALK: (
        ("model.direct", "primary"),              # 闲聊去检索只会捞出无关条文
    ),
    QueryCategory.MULTI_INTENT: (
        ("clarify.ask", "primary"),
    ),
    QueryCategory.UNKNOWN: (
        ("retrieval.hybrid_rerank", "primary"),   # 判不出来就别走捷径
        ("model.direct", "synthesize"),
    ),
}

#: 反问话术。**由系统按枚举渲染，不让模型自由生成**（decisions.U25 的同一原则）。
CLARIFY_TEXT = "您这句话里有不止一个诉求，请先告诉我要处理哪一个？"


class PlanError(ValueError):
    pass


def plan(query: str, *, tier: access.AccessTier = access.AccessTier.GUEST,
         intent_layer: str | None = None, intent_label: str | None = None,
         min_confidence: float | None = None,
         with_route: bool = True) -> RetrievalPlan:
    """把一句话规划成「分类 + 工具序列」。

    参数
    ----
    tier:
        访问层级。**工具按层级裁剪**：没权限的工具在规划期就被剔除并记账，
        不让它跑到执行期才炸（那时用户看到的是「查不到」，而不是「去登录」）。
    intent_layer / intent_label:
        `intent_layer` 透传给 `material_center.routing.route_query`，**只消费不改写**（U46）。
        `intent_label` 是五层意图路由（U74）的结论，**仅在正则信号全落空时采用**。
    min_confidence:
        透传给路线判定。
    """
    text = (query or "").strip()
    if not text:
        raise PlanError("查询不得为空 —— 空查询无从规划")

    category, decided_by, signals = classify(text, intent_layer=intent_layer,
                                             intent_label=intent_label)

    route: RoutingDecision | None = None
    notes: list[str] = []
    if with_route:
        try:
            route = route_query(text, intent_layer=intent_layer,
                                min_confidence=min_confidence)
        except Exception as exc:                      # noqa: BLE001 —— 路线判不出不阻断规划
            notes.append(f"路线判定失败（{type(exc).__name__}: {exc}），按分类结果执行")
        else:
            # 路线与分类冲突时**如实记录**，但不偷偷改分类：
            # 两者判据不同（路线看标识/歧义，分类看诉求），冲突本身是有用的信号
            if route.route is QueryRoute.LLM_ANALYSIS and category is not QueryCategory.MULTI_INTENT:
                notes.append("路线走到大模型分析，但分类未判为多意图：按分类执行并加强复检")

    steps: list[PlannedStep] = []
    dropped: list[DroppedTool] = []
    for order, (tool, role) in enumerate(CATEGORY_TOOLS[category], start=1):
        spec = TOOLS.get(tool)
        if spec is None:
            raise PlanError(f"分类 {category.value} 引用了未登记的工具 {tool!r}")
        allowed, reason = spec.available_to(tier)
        if not allowed:
            dropped.append(DroppedTool(tool, f"{tier.value} 层级不可用：{reason}"))
            continue
        steps.append(PlannedStep(order=order, tool=tool, method=spec.method, role=role))

    # primary 被裁掉之后，剩下的第一步顶上 —— 否则「无步骤」会让调用方无从下手
    if steps and steps[0].role != "primary":
        head = steps[0]
        steps[0] = PlannedStep(order=1, tool=head.tool, method=head.method, role="primary")

    if not steps:
        # 全被裁掉时不假装能查：如实降级为「直接回答」，并把原因带出去
        fallback = TOOLS["model.direct"]
        allowed, _ = fallback.available_to(tier)
        if not allowed:
            raise PlanError(f"分类 {category.value} 在该层级没有任何可用工具，且兜底也被拒")
        steps = [PlannedStep(order=1, tool="model.direct",
                             method=RetrievalMethod.MODEL_DIRECT, role="primary")]
        notes.append("该分类的工具全部被层级裁剪，降级为直接回答")

    needs_clarify = category is QueryCategory.MULTI_INTENT and bool(steps) \
        and steps[0].method is RetrievalMethod.CLARIFY

    return RetrievalPlan(
        query=text, category=category, decided_by=decided_by, signals=signals,
        steps=tuple(steps), dropped_tools=tuple(dropped), route=route,
        needs_clarification=needs_clarify,
        clarify_question=CLARIFY_TEXT if needs_clarify else "",
        tier=tier, notes=tuple(notes),
    )


def describe_categories() -> dict[str, dict[str, object]]:
    """分类 → 工具序列的人读形式，供自检与 `/api/retrieval/plan` 的自描述。"""
    out: dict[str, dict[str, object]] = {}
    for category, steps in CATEGORY_TOOLS.items():
        out[category.value] = {
            "steps": [{"tool": t, "role": r,
                       "method": TOOLS[t].method.value,
                       "bypasses_llm": TOOLS[t].bypasses_llm} for t, r in steps],
            "needs_evidence": any(TOOLS[t].needs_evidence for t, _ in steps),
        }
    return out


def describe_tools() -> list[dict[str, object]]:
    return [{"name": spec.name, "method": spec.method.value,
             "description": spec.description,
             "requires": sorted(c.value for c in spec.requires),
             "bypasses_llm": spec.bypasses_llm,
             "needs_evidence": spec.needs_evidence}
            for spec in TOOLS.values()]


def tools_for(tier: access.AccessTier) -> tuple[str, ...]:
    """该层级可用的工具名单（供端侧/运维核对「他能用什么」）。"""
    names: Iterable[str] = (name for name, spec in TOOLS.items()
                            if spec.available_to(tier)[0])
    return tuple(sorted(names))
