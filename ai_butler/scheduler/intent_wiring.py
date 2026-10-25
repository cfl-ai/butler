"""P3 五层意图路由的装配点（U74）：把「写好的层」接成「跑起来的管道」。

计划书依据
----------
- 一、项目概述 / 三、P3「五层意图路由：规则匹配 → Jev → 向量 → 垂类决策模型 → 大模型兜底」
- 三、P3「全链路降级与回滚机制：任一环节失败自动降级，不阻断主流程」
- 八、五层路由冻结：层数与顺序不得变更（表 6）
- 三、P3「Jev 支持影子灰度模式」

为什么单独一个装配模块
--------------------
E-77 的教训：`HardRuleLayer` / `VectorLayer` / `JevLayer` 三个类**有实现、有测试**，
但全项目没有一处构造它们（`grep` 引用数为 0）。于是「五层意图路由」在运行时
不存在，而所有测试都是绿的 —— 测试只能证明类可用，证明不了链路在用。

所以装配收成一个可导入、可断言的函数，和 `retrieval/wiring.py` 同形：
`app.py` 只拿一个 `IntentRouter`，不认识阈值、语料、模型目录。

实际接的是三层，不是五层 —— 原因写在这里，不靠省略
------------------------------------------------
| 层 | 状态 | 为什么 |
|---|---|---|
| L1 硬规则 | **接** | 正则**从 `planner.CATEGORY_SIGNALS` 派生**，不另存一份（E-44：两处正则必漂移） |
| L2 Jev | **接** | 影子期走旁路（不阻塞主链路）；`JEV_SHADOW_MODE=false` 即生效 |
| L3 向量 | **接** | 本地 bge 编码，断网可用；懒加载，进程内共享 |
| L4 垂类模型 | **不接** | 全项目只有 `VerticalDecisionModel` 协议，没有任何实现；U03 要求先标定 |
| L5 大模型兜底 | **不接** | 与 L2 是同一件事（同网关、同 prompt 形态、同解析），接了两份账 |

**层数仍然是五层**（表 6 冻结），这里只是「有实现的层才进管道」。
装配出来的 `layer_names` 必须仍是 `LAYERS` 的子序列 —— 由守护测试钉住。

阈值 0.62 的来历与限制（必须如实标注）
------------------------------------
```
0.62 不是标定值。它是 router.DEFAULT_VECTOR_THRESHOLD 的既有取值，
在 62 句自建语料上实测优于 0.80 / 0.85（总准 83.9% vs 77.4%）。
但那次实测的示例语料与测试句同源 —— L3 的成绩被**高估**。
U03 要求阈值在人工标注的中文验证集上重新标定：**此项未完成**。
```
所以这里有 `VECTOR_THRESHOLD_CALIBRATED = False` 这个常量，而不是一句注释：
只要它还是 False，`describe()` 就会把它报成「未标定」，不允许对外声称已标定。

为什么 L3 排在 L2 后面（而不是按「便宜的先跑」调换）
------------------------------------------------
五层顺序是【八】冻结的，不得调换。实测显示 L3（6.5ms）比 L2（1050ms）便宜
且更准，但**顺序不是可优化项** —— 想省 L2 的调用，靠的是 L3 的阈值把
残量压下去（实测 L2 调用率 30.6% → 8.1%），不是靠改顺序。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Sequence

from ai_butler.core.config import Settings
from ai_butler.infra.llm_gateway import JevClient, LLMGateway
from ai_butler.retrieval.planner import CATEGORY_SIGNALS, QueryCategory
from ai_butler.scheduler.router import (
    LAYERS,
    LAYER_HARD_RULE,
    LAYER_JEV,
    LAYER_LLM,
    LAYER_VECTOR,
    LAYER_VERTICAL,
    HardRule,
    HardRuleLayer,
    IntentRouter,
    JevLayer,
    RouteError,
    VectorLayer,
)

#: 路由标签集 = `QueryCategory` 的全部取值。
#:
#: 【文档未定义项 #1】计划书只说「Jev 输出结构化枚举标签」，没给清单。
#: U74 的裁定是**不另造一套标签**：既然检索侧已经有 10 类且每类对应不同工具序列，
#: 意图标签就用它。这样路由结论能**直接**被 `planner.plan()` 消费，
#: 不需要一张「路由标签 → 查询分类」的翻译表（多一张表就多一处漂移）。
INTENT_LABELS: tuple[str, ...] = tuple(c.value for c in QueryCategory)

#: 阈值是否在人工标注的中文验证集上标定过。**U03 未完成，保持 False。**
VECTOR_THRESHOLD_CALIBRATED = False

#: 等待冷启动的最长时间（秒）。超过就降级到下一层，**不占用整层的 5s 超时预算**。
READY_WAIT_SECONDS = 0.5

#: L3 示例语料：每类 10 句，**书面、规整**表达。
#:
#: 为什么用书面语而不用口语：它是「类别原型」而不是「用户说法样本」。
#: 代价是口语化提问的相似度会被拉低（实测：口语测试句命中率低于书面句），
#: 所以阈值只能偏保守；等真实语料攒够再做标定（U03）。
EXEMPLARS: dict[str, tuple[str, ...]] = {
    QueryCategory.IDENTITY_LOOKUP.value: (
        "请问订单号 12345 的当前状态",
        "查询工单编号的处理进度",
        "这个错误码代表什么含义",
        "该版本号对应的更新内容",
        "帮我核对一下流水号",
        "运单号的签收情况",
        "合同编号为多少",
        "产品型号是哪一个",
        "查询单号对应的记录",
        "状态码对应的错误说明",
    ),
    QueryCategory.FACT_QUESTION.value: (
        "营业时间是几点到几点",
        "这个服务包含哪些内容",
        "两地之间的交通需要多长时间",
        "发票是电子版还是纸质版",
        "价格是多少",
        "这个功能支持哪些格式",
        "客服电话是多少",
        "办理需要多长时间",
        "这个东西是什么材质",
        "地点在哪里",
    ),
    QueryCategory.POLICY_RULE.value: (
        "是否允许未成年人注册",
        "超过七天是否可以退货",
        "报销需要满足什么条件",
        "能否同时使用两张优惠券",
        "这样的操作是否属于违规",
        "政策规定了哪些限制",
        "需要什么资格才能申请",
        "是否合规",
        "有没有次数限制",
        "什么情况下不允许办理",
    ),
    QueryCategory.PROCEDURE.value: (
        "如何修改绑定的手机号码",
        "申请退款的操作步骤",
        "发票开具流程是什么",
        "怎样取消自动续费",
        "开户的具体流程",
        "在哪里办理这项业务",
        "变更信息需要怎么操作",
        "具体步骤是怎样的",
        "如何提交材料",
        "办理流程分几步",
    ),
    QueryCategory.COMPARISON.value: (
        "两种套餐之间的区别",
        "这两个版本的优缺点对比",
        "哪一家的速度更快",
        "两个方案差异在哪里",
        "选择哪一个更合适",
        "对比一下两者的价格",
        "这两种方式哪个更好",
        "它和另一个有什么不同",
        "特点对比",
        "之间的差异分析",
    ),
    QueryCategory.RECOMMENDATION.value: (
        "推荐一个适合我的方案",
        "帮我挑选一款产品",
        "有什么好的建议",
        "你建议我选择哪一个",
        "给一个可行的方案",
        "适合新手的推荐",
        "怎么选比较划算",
        "希望得到购买建议",
        "帮我决定用哪一个",
        "有没有值得推荐的选项",
    ),
    QueryCategory.PERSONAL_RECALL.value: (
        "我上次提到的那个需求",
        "我之前问过的问题",
        "我刚才说的内容",
        "我的历史记录里有什么",
        "我说过的那个想法",
        "之前让你记录的事情",
        "我那个订单的后续",
        "前段时间我咨询的内容",
        "我此前提到的偏好",
        "上次那个方案",
    ),
    QueryCategory.SMALL_TALK.value: (
        "你好",
        "在吗",
        "谢谢",
        "早上好",
        "辛苦了",
        "再见",
        "哈哈",
        "您好呀",
        "拜拜",
        "多谢",
    ),
    QueryCategory.MULTI_INTENT.value: (
        "帮我查订单顺便推荐套餐",
        "退款怎么弄以及发票怎么开",
        "既想改地址又想查物流",
        "会员价格多少另外能否开票",
        "两个问题一起问",
        "同时需要查询和办理",
        "先查一下再说说怎么办",
        "这两件事都要处理",
        "查询进度并且修改信息",
        "既要退款也要开发票",
    ),
    QueryCategory.UNKNOWN.value: (
        "嗯",
        "那个东西",
        "帮我看看",
        "随便问问",
        "这个怎么办",
        "不太清楚",
        "你看着办",
        "就是那个",
        "没了",
        "想一想",
    ),
}


# --------------------------------------------------------------------------- #
# L1：硬规则 —— 从单一真值源派生，不另写一份正则
# --------------------------------------------------------------------------- #
def build_hard_rules() -> list[HardRule]:
    """把 `planner.CATEGORY_SIGNALS` 摊平成硬规则表。

    **派生而不是手抄**：同一批正则存在两处，早晚会漂移（E-44 的形态）。
    这里只做两件转换：

    1. 同一分类的多条正则合并成一条交替式 —— 硬规则层只关心「命中哪一类」，
       不关心命中该类的第几条；
    2. 出现顺序 → 优先级。`HardRuleLayer` 按优先级降序试，于是「先出现在表里的
       分类先判」，与 `planner.classify()` 的「表序即优先级」逐字一致。

    不包含 `small_talk` 与 `multi_intent`：它们不是单条正则能表达的
    （前者要「长度上限 + 寒暄词 + 无实体」，后者要「并列连接词 + 两类不同信号」），
    交给 planner 自己的确定性判定，以及 L3 的示例语料。
    """
    order: list[str] = []
    patterns: dict[str, list[str]] = {}
    for pattern, _name, category in CATEGORY_SIGNALS:
        key = category.value
        if key not in patterns:
            patterns[key] = []
            order.append(key)
        patterns[key].append(pattern)

    total = len(order)
    rules: list[HardRule] = []
    for index, label in enumerate(order):
        if label not in INTENT_LABELS:            # 派生结果必须落在冻结标签集内
            raise RouteError(f"分类 {label!r} 不在意图标签集内，硬规则无法表达")
        rules.append(HardRule(
            pattern="|".join(f"(?:{p})" for p in patterns[label]),
            label=label,
            priority=total - index,
            note=f"派生自 planner.CATEGORY_SIGNALS 第 {index + 1} 组",
        ))
    return rules


# --------------------------------------------------------------------------- #
# L3：懒加载向量层
# --------------------------------------------------------------------------- #
#: 进程内共享：模型只加载一次，所有 App 实例复用。
#: 测试里每个用例都会 `create_app`，不共享就意味着每个用例重跑一次模型加载。
#: **键里不含阈值以外的东西**，因为默认工厂只有 bge 一种；
#: 注入自定义嵌入器（测试用的 HashingEmbedder）时不走这个缓存 ——
#: 不同 dim 的替身若共用键，会拿到上一条用例的向量表，属于静默错配。
_VECTOR_CACHE: dict[tuple[str, float], VectorLayer] = {}
_CACHE_LOCK = threading.RLock()
#: 每个键一把锁。**不能用一把全局锁**：模型加载要 7 秒，全局锁会让
#: 「同一进程里另一个不相关的向量层」排队等这 7 秒，实测表现是请求超时 +
#: 莫名其妙的「冷启动中」。锁的粒度必须跟着资源粒度走。
_KEY_LOCKS: dict[tuple[str, float], threading.Lock] = {}


def _key_lock(key: tuple[str, float]) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


def _default_embedder_factory(settings: Settings):
    """构造 bge 嵌入器。**只在真正需要时才调用**（模型加载 1~3 秒）。"""
    from ai_butler.material_center.embedding import BGEEmbedder

    model_dir = Path("models") / "bge-small-zh-v1.5"
    if not (model_dir / "config.json").is_file():
        raise RouteError(f"向量层模型缺失：{model_dir}（运行 scripts/download_models.py）")
    return BGEEmbedder(model_dir)


class LazyVectorLayer:
    """首次使用时才建索引的向量层，并支持**后台预热**。

    **为什么懒**：`create_app` 会被几百个用例调用，构造期同步加载 BGE 会让
    「跑一次测试」从秒级变成分钟级。

    **为什么还要预热**：冷启动实测 —— 加载模型 6.9s + 建 100 句索引 2.8s ≈ **9.7s**，
    而单层超时预算是 5s。也就是说「第一个请求顺手把模型加载了」这个想法不成立：
    它必然撞超时，然后降级。所以预热放在装配后立刻用守护线程做（不阻塞启动），
    把这段成本从**用户请求**挪到**进程启动**。

    冷启动窗口内（预热未完成）请求会看到一层 RouteError，路由据此降级到 L2/L1，
    并把「向量层冷启动中」写进 `degradation_notes` —— 降级要留痕，不能静默。
    """

    name = LAYER_VECTOR

    def __init__(self, factory: Callable[[], object], *,
                 threshold: float, exemplars: dict[str, Sequence[str]],
                 cache_key: str | None = None) -> None:
        self._factory = factory
        self.threshold = threshold
        self._exemplars = exemplars
        self._cache_key = cache_key
        self._layer: VectorLayer | None = None
        self._error: str = ""
        self._ready = False
        self._warm_thread: threading.Thread | None = None

    # ---------------- 构建 ----------------
    def _ensure(self) -> VectorLayer:
        """同步构建（会加载模型）。**必须在工作线程里调**，且可被并发调用。

        并发安全按**资源粒度**加锁：同一个模型 + 同一个阈值只建一次，
        不同阈值/不同嵌入器互不排队（一把全局锁会让 7 秒的加载阻塞无关请求）。
        """
        if self._layer is not None:
            return self._layer

        if self._cache_key is None:                # 注入替身：不进共享缓存，也不占全局锁
            layer = VectorLayer(self._factory(), self._exemplars,
                                threshold=self.threshold)
            self._layer = layer
            self._ready = True
            return layer

        key = (self._cache_key, self.threshold)
        with _key_lock(key):
            cached = _VECTOR_CACHE.get(key)
            if cached is not None:
                self._layer = cached
                self._ready = True
                return cached
            layer = VectorLayer(self._factory(), self._exemplars,
                                threshold=self.threshold)
            _VECTOR_CACHE[key] = layer
            self._layer = layer
            self._ready = True
            return layer

    def start_warmup(self) -> None:
        """装配后立刻预热。**守护线程 + 吞异常**：预热失败不该让启动失败。"""
        if self._warm_thread is not None or self._ready:
            return

        def _run() -> None:
            try:
                self._ensure()
            except Exception as exc:              # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=_run, name="intent-vector-warmup", daemon=True)
        thread.start()
        self._warm_thread = thread

    # ---------------- 判定 ----------------
    async def evaluate(self, request):
        import asyncio

        if self._layer is None:
            if self._error:
                # 已经确定不可用：不再重试，也不再让每个请求等一次
                raise RouteError(f"向量层不可用：{self._error}")
            try:
                # **只在就绪上等一小会，不把整层超时预算耗在冷启动上**。
                # 实测冷启动约 9.7s，而单层预算是 5s：等满预算就等于「每层都超时」，
                # 请求白等 5 秒还是降级。0.5s 足够判断「预热好了没」。
                await asyncio.wait_for(asyncio.to_thread(self._ensure),
                                       timeout=READY_WAIT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise RouteError("向量层冷启动中（预热未完成）：本层跳过") from exc
            except Exception as exc:              # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"
                raise RouteError(f"向量层不可用：{self._error}") from exc
            if self._layer is None:
                raise RouteError("向量层冷启动中：本层跳过，交由下一层判定")
        return await self._layer.evaluate(request)


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
def build_intent_router(settings: Settings, *, gateway: LLMGateway | None = None,
                        embedder_factory: Callable[[], object] | None = None,
                        on_shadow=None) -> IntentRouter | None:
    """装配意图路由。**唯一装配点** —— 测试直接断言它的输出。

    返回 None 只在「总开关关闭」时发生，且调用方必须把这件事说出来，
    不能悄悄退化成「只有正则分类」。
    """
    if not settings.intent_router_enable:
        return None

    layers = []

    rules = build_hard_rules()
    if rules:
        layers.append(HardRuleLayer(rules))

    if settings.jev_enable and gateway is not None:
        # 影子态由构造参数决定（旁路），不再依赖「决策拿回来之后再判」
        layers.append(JevLayer(JevClient(gateway, settings),
                               on_shadow=on_shadow,
                               shadow=settings.jev_shadow_mode))

    if settings.intent_vector_enable:
        factory = embedder_factory or (lambda: _default_embedder_factory(settings))
        # 注入替身时**不进进程缓存**：替身的维度/实现各不相同，共用键会静默错配
        key = None if embedder_factory is not None else "bge-small-zh-v1.5"
        vector_layer = LazyVectorLayer(factory,
                                       threshold=settings.intent_vector_threshold,
                                       exemplars=EXEMPLARS, cache_key=key)
        vector_layer.start_warmup()
        layers.append(vector_layer)

    if not layers:
        return None

    router = IntentRouter(layers)
    names = router.layer_names
    position = [LAYERS.index(n) for n in names]
    if position != sorted(position):              # 五层顺序冻结，装配也不得违反
        raise RouteError(f"装配后的层顺序违反冻结表 6：{names}")
    return router


def describe(settings: Settings) -> dict[str, object]:
    """路由装配的人读形态，供 `/api/retrieval/plan` 之外的运维核对使用。"""
    wired = [name for name in LAYERS if _wired(name, settings)]
    return {
        "layers_total": len(LAYERS),
        "layers_wired": wired,
        "layers_skipped": {name: _skip_reason(name, settings)
                           for name in LAYERS if name not in wired},
        "labels": list(INTENT_LABELS),
        "vector_threshold": settings.intent_vector_threshold,
        "vector_threshold_calibrated": VECTOR_THRESHOLD_CALIBRATED,
        "jev_shadow_mode": settings.jev_shadow_mode,
        "jev_allow_clarify": settings.jev_allow_clarify,
        "exemplars_per_label": {k: len(v) for k, v in EXEMPLARS.items()},
    }


def _wired(name: str, settings: Settings) -> bool:
    if not settings.intent_router_enable:
        return False
    if name == LAYER_HARD_RULE:
        return bool(CATEGORY_SIGNALS)
    if name == LAYER_JEV:
        return bool(settings.jev_enable)
    if name == LAYER_VECTOR:
        return bool(settings.intent_vector_enable)
    return False


def _skip_reason(name: str, settings: Settings) -> str:
    if not settings.intent_router_enable:
        return "intent_router_enable=false：整条路由未装配"
    if name == LAYER_VERTICAL:
        return "全项目无 VerticalDecisionModel 实现（只有协议）；U03 要求先标定"
    if name == LAYER_LLM:
        return "与 L2 Jev 是同一件事（同网关、同 prompt 形态、同解析），不重复接"
    if name == LAYER_JEV:
        return "jev_enable=false"
    if name == LAYER_VECTOR:
        return "intent_vector_enable=false"
    return "未装配"
