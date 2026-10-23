"""评测中台 Agent —— 结果与过程并重的七大维度评测（横切 Agent）。

用户指令（2026-10-23，裁定 U55）
------------------------------
「再补充添加一个评测中台 agent；能直观反映出工程师的工程水平。
真正的评测绝非仅靠大模型离线评分，而是需要全面覆盖结果与过程的
七大核心维度。强调评测集面试重要性——很多求职者误以为评测只是构建一个
包含一两百条问答的黄金数据集，然后让大模型离线跑一下进行评分。」

定位
----
横切 Agent（`core/naming.py` 的 USER_AGENT_TITLES，来源是用户指令而非
计划书原文）：它不属于任何一个阶段，而是**消费所有阶段 Agent 的过程
痕迹**（检索 / 工具 / 大模型 / 异常四类），按七大维度评定并输出报告。
计划书三、P9 的「全维度指标监控看板」是它的下游消费者。

与「离线跑分」的本质区别
------------------------
一两百条黄金数据集 + 大模型离线评分，只覆盖七大维度里的**结果侧**一维。
本 Agent 评的是过程：检索漏没漏、证据忠不忠、工具调用真不真、
稳定不稳定、异常能不能判责、成本几何、权限守不守规矩。

纪律（与全项目同一套）
--------------------
1. 只读全局 State，仅写入自有字段 ~{eval_platform}~（计划书三、P4）。
2. 出口必经 Redactor，并如实标注脱敏层级（P9「禁止展示明文用户信息」）。
3. 安全一票否决：结论先看安全，再看其他维度（与 U49 同构）。
4. LLM 裁判是**可选端口**：未注入就用确定性启发式，且报告里写明用了哪种
   （声明≠能力——不冒充做了模型级判定）。
5. **未接线项（如实登记）**：痕迹的**生产端**（各阶段 Agent 运行时埋点、
   把 ToolDispatcher 的调用写进 State）尚未接线。本 Agent 当前消费调用方
   显式提供的 traces；生产端接线属独立工作项，不在本模块假装已完成。
"""

from __future__ import annotations

# 直接运行本文件（python xxx.py / PyCharm 右键）时，sys.path[0] 是本文件所在目录
# 而非项目根，`import ai_butler` 会直接 ModuleNotFoundError。此处把项目根补进去；
# 作为包被 import 时 __package__ 非空，这段不执行。
if not __package__:  # pragma: no cover - 仅直接运行时触发
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    del _sys, _Path

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ai_butler.agents.base import SubAgent
from ai_butler.evaluation.platform import EvaluationCase, EvaluationPlatform
from ai_butler.evaluation.redaction import Redactor
from ai_butler.evaluation.trace import traces_from_dicts


@dataclass
class EvaluationPlatformAgent(SubAgent):
    """评测中台：吃过程痕迹，吐七维度报告。"""

    name = "eval_platform_agent"
    writes = ("eval_platform",)
    #: 本 Agent 是纯计算：读 State 里的痕迹与金标准，不触达任何外部能力。
    #: 不虚声明能力——未声明的能力根本不会发到手上（计划书三、P4）。
    capabilities = ()
    responsibilities = ("消费各阶段 Agent 的过程痕迹，按七大维度（检索召回/证据忠实/"
                        "工具真实/稳定异常/成本效率/安全权限/输出质量）评定，"
                        "安全一票否决，出口脱敏")

    tools: Any = None
    #: 可选的 LLM 裁判端口（维度二证据忠实度）：judge(claim, evidences) -> bool
    judge: Any = None
    #: 脱敏模型层分析器（如 Presidio 适配器），与 P9 同一端口；未注入即只用正则兜底
    analyzer: Any = None
    redactor: Redactor = field(init=False)
    platform: EvaluationPlatform = field(init=False)

    def __post_init__(self) -> None:
        self.redactor = Redactor(analyzer=self.analyzer)
        self.platform = EvaluationPlatform(judge=self.judge, redactor=self.redactor)

    # ------------------------------------------------------------------ #
    # State 解析：State 里只有可 JSON 化的 dict，显式转回领域对象。
    # 解析失败一律上抛——带着半套痕迹评出来的结论比没有结论更危险。
    # ------------------------------------------------------------------ #
    def _cases(self, state: Mapping[str, Any]) -> list[EvaluationCase]:
        raw = state.get("eval_cases") or []
        if not isinstance(raw, (list, tuple)):
            raise TypeError("eval_cases 应为列表")
        return [EvaluationCase.from_dict(item) for item in raw]

    def _traces(self, state: Mapping[str, Any]) -> list:
        raw = state.get("agent_traces") or []
        if not isinstance(raw, (list, tuple)):
            raise TypeError("agent_traces 应为列表")
        return list(traces_from_dicts(raw))

    # ------------------------------------------------------------------ #
    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        cases = self._cases(state)
        traces = self._traces(state)

        # 按 question 分组：同一问题的多次运行构成一个运行组
        # （稳定性维度的分母就在这里形成）
        groups: dict[str, list] = {}
        for trace in traces:
            groups.setdefault(trace.question, []).append(trace)

        reports = [self.platform.evaluate(groups.get(case.question, []), case)
                   for case in cases]

        passed = [r for r in reports if r.passed]
        vetoed = [r for r in reports if r.vetoed]
        failed = [r for r in reports if not r.passed]
        return {"eval_platform": {
            "user_id": uid,
            "case_count": len(cases),
            "trace_count": len(traces),
            "summary": {
                "passed": len(passed),
                "failed": len(failed),
                "vetoed": len(vetoed),
                "mean_score": (round(sum(r.overall_score for r in reports) / len(reports), 4)
                               if reports else 0.0),
                "failed_case_ids": [r.case_id for r in failed],
                "vetoed_case_ids": [r.case_id for r in vetoed],
            },
            "reports": [r.as_dict(self.redactor) for r in reports],
            # 两层都**从 redactor 推导**，不写死（与 P9 看板同一立场）
            "redaction": {"regex_layer": self.redactor.regex_layer_active,
                          "model_layer": self.redactor.model_layer_active},
        }}


if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
