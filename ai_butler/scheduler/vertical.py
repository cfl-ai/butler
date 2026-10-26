"""P3 第 4 层：垂类专用决策模型（U110，落地 U98 协议）。

计划书依据
----------
- 三、P3「五层意图路由：… → 垂类专用决策模型 → …」；U03 要求照 RouteLLM 的
  bert router 形态（中文基座小分类器 + 阈值路由），且阈值须在**中文验证集**上标定。
- U98 裁定要做、但长期只有 `VerticalDecisionModel` 协议无实现（声明≠能力，E-86）。
  本模块是协议的首个落地实现。

实现形态与标定纪律
----------------
U03 要求「小分类器 + 阈值」，但本机没有标注好的中文垂类验证集，强行塞一个
bert 分类器会重蹈 U108 的覆辙（声明可用、实则点错）。因此首版采用**可解释的
关键词 + 结构特征打分器**（`KeywordVerticalModel`）：
- 每个垂类在 `VerticalRegistry` 里登记一组带权关键词与结构信号；
- `predict(utterance, labels)` 对候选垂类逐一打分，返回最高分 `(vertical, score)`；
- 阈值（默认 0.55，与 `router.DEFAULT_VERTICAL_THRESHOLD` 对齐）由
  `tests/test_vertical_dispatch.py` 的**标定样本守护**钉住——旅游样本必须 ≥ 阈值、
  非旅游样本必须 < 阈值；哪天要换成真 bert，只需替换 `KeywordVerticalModel` 这一层，
  协议与守护不动。

**默认不激活进主路由**：`intent_wiring.build_intent_router` 仅在
`settings.vertical_router_enable=True` 时把本层接进管道（U110）。理由：L4 与
L2/L3 是互补而非互斥，接进来前标定守护必须全绿，避免未标定的阈值污染线上分发。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ai_butler.scheduler.router import (
    DEFAULT_VERTICAL_THRESHOLD,
    LAYER_VERTICAL,
    LayerVerdict,
    RouteError,
    VerticalDecisionModel,
)

#: 阈值是否在人工标注的中文验证集上标定过。**U03 未完成，保持 False。**
VERTICAL_THRESHOLD_CALIBRATED = False


@dataclass(frozen=True)
class VerticalMeta:
    """一个垂类的注册元数据：L4 路由表的一行。"""

    key: str                                          # 垂类标识，同时充当意图标签，如 "tour"
    display: str                                      # 中文名，如 "旅游规划"
    description: str = ""
    keywords: Mapping[str, float] = field(default_factory=dict)   # (关键词, 权重)
    signals: Mapping[str, float] = field(default_factory=dict)    # (结构信号短语, 权重)
    threshold: float = DEFAULT_VERTICAL_THRESHOLD
    #: 该垂类的生产装配点标识（字符串），L4 命中后由派发器据此调用对应工厂。
    #: 与 U108 的 `make_amap_source` 同形——「唯一装配点」范式，不手写第二份。
    factory: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise RouteError("垂类元数据必须声明 key")
        if not self.display:
            raise RouteError(f"垂类 {self.key!r} 必须声明中文名")


class VerticalRegistry:
    """L4 的垂类路由表（垂类键 → 元信息）。"""

    def __init__(self, items: Sequence[VerticalMeta]) -> None:
        self._by_key: dict[str, VerticalMeta] = {}
        for item in items:
            if item.key in self._by_key:
                raise RouteError(f"垂类键重复注册：{item.key!r}")
            self._by_key[item.key] = item

    def keys(self) -> tuple[str, ...]:
        return tuple(self._by_key)

    def items(self) -> tuple[VerticalMeta, ...]:
        return tuple(self._by_key.values())

    def get(self, key: str) -> VerticalMeta | None:
        return self._by_key.get(key)

    def register(self, meta: VerticalMeta) -> None:
        if meta.key in self._by_key:
            raise RouteError(f"垂类键重复注册：{meta.key!r}")
        self._by_key[meta.key] = meta

    def as_labels(self) -> tuple[str, ...]:
        """L4 的候选标签集 = 已注册的垂类键。"""
        return self.keys()


class KeywordVerticalModel:
    """关键词 + 结构特征的可解释打分器（`VerticalDecisionModel` 协议的落地）。

    不是 bert，但满足 U03 的「阈值路由」形态，且**可解释、可标定、可替换**。
    """

    name = "vertical-keyword-scorer"

    def __init__(self, registry: VerticalRegistry) -> None:
        self.registry = registry

    def predict(self, utterance: str, labels: Sequence[str]) -> tuple[str, float] | None:
        """返回 `(垂类键, 置信)` 或 None。

        `labels` 是候选垂类键；为空或与注册表不相交时，退化为对全部已注册垂类打分。
        """
        if labels:
            candidate_keys = [k for k in labels if self.registry.get(k)]
            if not candidate_keys:
                candidate_keys = list(self.registry.keys())
        else:
            candidate_keys = list(self.registry.keys())

        best_key: str | None = None
        best_score = 0.0
        for key in candidate_keys:
            meta = self.registry.get(key)
            if meta is None:
                continue
            score = self._score(utterance, meta)
            if score > best_score:
                best_key, best_score = key, score
        if best_key is None:
            return None
        return (best_key, round(best_score, 4))

    @staticmethod
    def _score(utterance: str, meta: VerticalMeta) -> float:
        total = 0.0
        for kw, weight in meta.keywords.items():
            if kw and kw in utterance:
                total += weight
        for sig, weight in meta.signals.items():
            if sig and sig in utterance:
                total += weight
        return min(1.0, total)


class VerticalDispatchLayer:
    """L4 的路由层包装（与 `router.VerticalModelLayer` 同层名，但走垂类自有标签空间）。

    五层路由的 `RouteRequest.labels` 是 `QueryCategory` 检索分类，不含垂类键；
    垂类派发是**正交**的一轴，因此本层把注册表键当作候选标签传给模型，
    命中即返回垂类键（如 "tour"），不要求它落在检索分类集里。
    """

    name = LAYER_VERTICAL

    def __init__(self, model: KeywordVerticalModel, *,
                 threshold: float = DEFAULT_VERTICAL_THRESHOLD) -> None:
        self.model = model
        self.threshold = threshold

    async def evaluate(self, request) -> LayerVerdict | None:
        outcome = self.model.predict(request.utterance, self.model.registry.keys())
        if not outcome:
            return None
        label, confidence = outcome
        if confidence < self.threshold:
            return LayerVerdict(
                accepted=False, label=label, confidence=confidence,
                note=f"垂类模型置信 {confidence:.3f} < 阈值 {self.threshold}")
        return LayerVerdict(
            accepted=True, label=label, confidence=confidence,
            note=f"垂类模型命中 {confidence:.3f}（{label}）")


# --------------------------------------------------------------------------- #
# 旅游垂类的默认注册（U110：把 tour 注册进 VerticalRegistry，使 L4 可达）
# --------------------------------------------------------------------------- #
#: 旅游垂类的默认关键词（强信号，命中即明显加分）
_TOUR_KEYWORDS: Mapping[str, float] = {
    "旅游": 0.40, "旅行": 0.40, "行程": 0.40, "攻略": 0.40, "路线": 0.40,
    "景点": 0.40, "自驾游": 0.40, "周边游": 0.40, "度假": 0.40, "出游": 0.40,
    "门票": 0.40, "酒店": 0.40, "民宿": 0.40, "一日游": 0.40, "两日游": 0.40,
    "三日游": 0.40, "自由行": 0.40, "跟团": 0.40, "打卡": 0.30,
}
#: 旅游垂类的结构信号（弱信号，配合关键词才够阈值）
_TOUR_SIGNALS: Mapping[str, float] = {
    "规划": 0.22, "安排": 0.22, "推荐": 0.22, "想去": 0.22, "周末去": 0.22,
    "帮我安排": 0.22, "怎么玩": 0.22, "去哪玩": 0.22, "玩几天": 0.22,
    "路线推荐": 0.22, "行程规划": 0.22,
}


def build_tour_meta() -> VerticalMeta:
    """旅游垂类的注册元数据。"""
    return VerticalMeta(
        key="tour", display="旅游规划",
        description="景区/路线/行程/住宿/门票相关的旅游规划垂类（表 11）",
        keywords=_TOUR_KEYWORDS, signals=_TOUR_SIGNALS,
        threshold=DEFAULT_VERTICAL_THRESHOLD,
        factory="ai_butler.verticals.tour.wiring:build_tour_agent",
    )


def default_vertical_registry() -> VerticalRegistry:
    """L4 的默认路由表：首版只含旅游垂类，后续垂类按同形注册进来即可。"""
    return VerticalRegistry([build_tour_meta()])


def build_vertical_model(*, registry: VerticalRegistry | None = None) -> KeywordVerticalModel:
    """装配 L4 决策模型。

    不强制依赖 `Settings`：注册表与模型都是纯本机可构造的对象，
    标定守护可在无配置环境下跑（U03 标定纪律）。
    """
    if registry is None:
        registry = default_vertical_registry()
    return KeywordVerticalModel(registry)


# --------------------------------------------------------------------------- #
# 派发：把「L4 命中的垂类键」变成「真正跑起来的垂类 Agent」
# --------------------------------------------------------------------------- #
class VerticalDispatcher:
    """垂类派发器（U110，闭合 E-86 的最后一环）。

    L4 只回答「这像不像旅游任务」（`KeywordVerticalModel`），**不负责把它跑起来**。
    跑起来这一步必须有个唯一的、可断言的地方，`build_tour_agent` 才不是
    「有实现、有测试、但没人调用」（E-86 的原文）。

    注册表里的 `factory` 是 `"module:attr"` 字符串而不是直接持有可调用对象：
    - 避免在 `scheduler` 里 import `verticals.tour`，把调度层和业务垂类**反向耦合**；
    - 惰性解析 + 缓存：只有真命中该垂类时才付导入与装配成本。

    解析失败**降级但留痕**：写进 `self.errors`，由调用方混进 `degradation`，
    不允许「派发失败」表现为「什么都没发生」。
    """

    def __init__(self, registry: VerticalRegistry | None = None) -> None:
        self.registry = registry or default_vertical_registry()
        self._cache: dict[str, Any] = {}
        self.errors: dict[str, str] = {}

    def has(self, key: str) -> bool:
        """该意图键是不是已注册的垂类。"""
        return self.registry.get(key) is not None

    def keys(self) -> tuple[str, ...]:
        return self.registry.keys()

    def resolve(self, key: str) -> Any | None:
        """按注册表解析出垂类 Agent 实例；失败返回 None 并把原因写进 `errors`。"""
        if key in self._cache:
            return self._cache[key]
        meta = self.registry.get(key)
        if meta is None:
            return None
        if not meta.factory:
            self.errors[key] = f"垂类 {key!r} 未声明装配点 factory"
            return None
        try:
            agent = _import_factory(meta.factory)()
        except Exception as exc:                      # noqa: BLE001 — 装配失败要可见
            # 错误信息里带上装配点本身：只说「哪个垂类失败」不足以定位是**哪个工厂**坏了
            self.errors[key] = (f"装配垂类 {key!r}（{meta.factory}）失败："
                                f"{type(exc).__name__}: {exc}")
            return None
        self._cache[key] = agent
        return agent

    def bind(self, key: str, agent: Any) -> None:
        """显式注入已装配的垂类 Agent，跳过 factory 解析。

        给测试替身与预装配覆盖留口子（与 `build_tour_agent(collector=...)` 的
        覆盖参数同形）；**不是**常规装配路径，常规路径走 `factory`。
        """
        self._cache[key] = agent

    def note_error(self, key: str) -> str:
        return self.errors.get(key, "")


def _import_factory(spec: str):
    """解析 `"module:attr"` 形态的装配点，返回可调用对象。

    **唯一解析处**：与 `make_amap_source` 的「唯一装配点」同形，
    不允许调用方自己 `TourAgent(...)`（会绕过官方 key 注入与逐段降级不变量）。
    """
    from importlib import import_module

    if ":" not in spec:
        raise RouteError(f"垂类装配点格式应为 'module:attr'，实际为 {spec!r}")
    module_name, attr = spec.split(":", 1)
    if not module_name or not attr:
        raise RouteError(f"垂类装配点格式应为 'module:attr'，实际为 {spec!r}")
    module = import_module(module_name)
    factory = getattr(module, attr, None)
    if factory is None or not callable(factory):
        raise RouteError(f"垂类装配点 {spec!r} 未解析到可调用对象")
    return factory
