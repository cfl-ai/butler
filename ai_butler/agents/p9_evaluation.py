"""P9 指标评估与增量微调体系｜持续优化体系。

计划书依据
----------
三、P9 六项开发内容 + 触发时机「系统后台常态化持续运行」
裁定 U28（LLaMA-Factory + LoRA/QLoRA）、U29（Alpaca 格式）、
U30（Presidio + 正则兜底）、U31（指标口径自持，Metabase 消费）

一个必须讲清楚的取舍
--------------------
**系统级看板默认只统计公共 Workspace。** 把私有 Vault 的内容聚合进全局看板，
即使脱敏了字段名，聚合本身也可能反推出个体（小样本尤其危险）。
因此 ~{scope}~ 默认 "public"，要统计某账号就显式带上 user_id 走私有视图。
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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agents.capabilities import CAP_DATA_READ, CAP_WIKI_READ
from ai_butler.agents.phase import PhaseAgent
from ai_butler.evaluation.cases import deposit_from_logs
from ai_butler.evaluation.dataset import (
    SftDataset,
    SftSample,
    assert_offline_source,
    dataset_info_entry,
)
from ai_butler.evaluation.finetune import (
    DEFAULT_INTERVAL_DAYS,
    DEFAULT_MIN_SAMPLES,
    TARGET_JEV_TEMPLATE,
    TARGET_VERTICAL_MODEL,
    FinetunePipeline,
)
from ai_butler.evaluation.metrics import JeVObservation, build_board
from ai_butler.evaluation.redaction import Redactor
from ai_butler.evaluation.report import build_report

#: 公共 Workspace 的 scope 名（无 user_id 的公共域）
SCOPE_PUBLIC = "public"
SCOPE_PRIVATE = "vault"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EvaluationAgent(PhaseAgent):
    """P9 阶段 Agent：算指标、沉案例、建数据集、排微调、出报告。"""

    phase_code: ClassVar[str] = "P9"
    writes: ClassVar[tuple[str, ...]] = ("p9_evaluation",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_DATA_READ, CAP_WIKI_READ)

    #: 离线链路严禁访问私有 Vault（计划书三、P10）
    #: 模型层分析器（如 Presidio 适配器）。未注入即只用正则兜底。
    analyzer: Any = None
    stale_days: int = 180
    interval_days: int = DEFAULT_INTERVAL_DAYS
    min_samples: int = DEFAULT_MIN_SAMPLES

    def _observations(self, state: Mapping[str, Any]) -> list[JeVObservation]:
        out: list[JeVObservation] = []
        for row in (state.get("jev_observations") or []):
            row = row or {}
            out.append(JeVObservation(
                expected_intent=str(row.get("expected_intent") or ""),
                actual_intent=str(row.get("actual_intent") or ""),
                multi_intent=bool(row.get("multi_intent")),
                detected_multi=bool(row.get("detected_multi"))))
        return out

    def _dataset(self, state: Mapping[str, Any], *, redactor: Redactor) -> SftDataset:
        dataset = SftDataset(name=str(state.get("dataset_name") or "butler_sft"))
        for row in (state.get("sft_samples") or []):
            row = row or {}
            history = tuple((str(p[0]), str(p[1])) for p in (row.get("history") or []))
            # 公共 Workspace 之外一律拒绝（含私有 Vault）
            assert_offline_source(scope=str(row.get("scope") or SCOPE_PUBLIC))
            dataset.add(SftSample(
                instruction=str(row.get("instruction") or ""),
                output=str(row.get("output") or ""),
                input=str(row.get("input") or ""),
                system=row.get("system"),
                history=history,
                source=str(row.get("source") or "public_workspace"),
                user_id=str(row.get("user_id") or "")))
        return dataset

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        now = _now()
        redactor = Redactor(analyzer=self.analyzer)

        # ---- 指标口径：系统级看板默认只看公共 Workspace（见模块头说明）----
        wiki = self.tools.require(CAP_WIKI_READ)
        pages = list(wiki.list_pages())          # 无 user_id 即公共域
        scope = SCOPE_PUBLIC
        board = build_board(observations=self._observations(state), pages=pages,
                            now=now, stale_days=self.stale_days,
                            generated_at=now.isoformat(), redactor=redactor)
        board_payload = board.as_dict()
        board_payload["scope"] = scope

        # ---- 日志沉淀与案例分类 ----
        deposit = deposit_from_logs(state.get("log_rows") or [])
        deposit_payload = deposit.as_dict(redactor)

        # ---- 领域 SFT 数据集（来源白名单 + 身份剥离）----
        dataset = self._dataset(state, redactor=redactor)
        dataset_payload = dataset.as_dict(redactor)
        dataset_payload["dataset_info"] = dataset_info_entry(
            name=dataset.name, file_name=dataset.name + ".json")

        # ---- 周期性增量微调流水线（只编排，不训练）----
        last_run = state.get("last_finetune_at")
        pipeline = FinetunePipeline(
            base_models=dict(state.get("base_models") or {
                TARGET_JEV_TEMPLATE: str(state.get("jev_base_model") or ""),
                TARGET_VERTICAL_MODEL: str(state.get("vertical_base_model") or ""),
            }),
            interval_days=self.interval_days, min_samples=self.min_samples)
        runs = pipeline.plan(
            now=now,
            last_run_at=(last_run if isinstance(last_run, datetime)
                         else datetime.fromisoformat(str(last_run)) if last_run else None),
            sample_counts=dict(state.get("sample_counts") or {}),
            method=str(state.get("finetune_method") or "lora"))

        # ---- 迭代评估报告 ----
        report = build_report(
            board=board_payload, deposit=deposit_payload, dataset=dataset_payload,
            pipeline=pipeline.as_dict(), runs=[r.as_dict() for r in runs],
            generated_at=now.isoformat(), redactor=redactor)

        return {"p9_evaluation": {
            "user_id": uid, "phase": self.phase_code,
            "scope": scope, "board": board_payload, "cases": deposit_payload,
            "dataset": dataset_payload, "pipeline": pipeline.as_dict(),
            "runs": [r.as_dict() for r in runs], "report": report,
            # 两层都**从 redactor 推导**，不写死。
            # 原先 regex_layer 是硬编码 True —— 声称脱敏了，但规则表没了也不会变。
            "redaction": {"regex_layer": redactor.regex_layer_active,
                          "model_layer": redactor.model_layer_active},
        }}

if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
