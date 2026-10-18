"""P9 周期性增量微调流水线（裁定 U28：LLaMA-Factory + LoRA/QLoRA）。

计划书依据
----------
三、P9「**周期性增量微调流水线**；支持微调 **Jev 决策 Question 模板**与**垂类模型**」
三、P9 触发时机「系统后台**常态化持续运行**」

范围声明（重要）
----------------
本模块负责**流水线编排与配置生成**：什么时候该拉什么数据、产出什么配置、
该微调哪个目标。**实际训练作业不在 CI 内执行**——本机是 CPU 推理环境、无 GPU（见 U28）。
把「编排可验证」与「训练需真机」分开，才不会让 CI 假装跑过训练。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

#: 裁定 U28：LoRA 为主，显存不足退 QLoRA
METHOD_LORA = "lora"
METHOD_QLORA = "qlora"
METHODS: tuple[str, ...] = (METHOD_LORA, METHOD_QLORA)

#: 微调目标（计划书点名的两类）
TARGET_JEV_TEMPLATE = "jev_question_template"
TARGET_VERTICAL_MODEL = "vertical_model"
TARGETS: tuple[str, ...] = (TARGET_JEV_TEMPLATE, TARGET_VERTICAL_MODEL)

#: 周期性：默认 7 天（实现参数；计划书只说「周期性」，未给数值）
DEFAULT_INTERVAL_DAYS = 7

#: 触发微调所需的最小样本量（实现参数）
DEFAULT_MIN_SAMPLES = 200


class PipelineError(ValueError):
    pass


@dataclass(frozen=True)
class FinetuneConfig:
    """一份 LLaMA-Factory 可消费的训练配置。"""

    target: str
    base_model: str
    dataset_name: str
    method: str = METHOD_LORA
    output_dir: str = "outputs"
    learning_rate: float = 1e-4
    num_epochs: float = 3.0
    cutoff_len: int = 2048
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise PipelineError(f"未知微调方式 {self.method!r}，可选 {list(METHODS)}")
        if self.target not in TARGETS:
            raise PipelineError(f"未知微调目标 {self.target!r}，可选 {list(TARGETS)}")
        if not self.base_model:
            raise PipelineError("必须指定 base_model")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": "sft",
            "finetuning_type": self.method,
            "model_name_or_path": self.base_model,
            "dataset": self.dataset_name,
            "template": "default",
            "output_dir": self.output_dir,
            "learning_rate": self.learning_rate,
            "num_train_epochs": self.num_epochs,
            "cutoff_len": self.cutoff_len,
            "target": self.target,
        }
        if self.method == METHOD_QLORA:
            payload["quantization_bit"] = 4
        payload.update(dict(self.extra))
        return payload


@dataclass(frozen=True)
class PipelineRun:
    target: str
    should_run: bool
    reason: str
    config: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "should_run": self.should_run,
                "reason": self.reason, "config": dict(self.config)}


@dataclass
class FinetunePipeline:
    """周期性增量微调流水线（编排层）。"""

    base_models: Mapping[str, str]
    interval_days: int = DEFAULT_INTERVAL_DAYS
    min_samples: int = DEFAULT_MIN_SAMPLES

    def due(self, *, last_run_at: datetime | None, now: datetime) -> bool:
        if last_run_at is None:
            return True
        return now - last_run_at >= timedelta(days=self.interval_days)

    def plan(self, *, now: datetime, last_run_at: datetime | None,
             sample_counts: Mapping[str, int],
             method: str = METHOD_LORA) -> list[PipelineRun]:
        """决定本轮要不要微调、微调谁、用什么配置。

        **样本不足时不硬跑**——用很少的数据反复微调只会把模型带偏，
        所以这里给出明确的 not-run 理由而不是「尽力而为」。
        """
        if not self.due(last_run_at=last_run_at, now=now):
            return [PipelineRun(t, False, f"未到周期（{self.interval_days} 天）")
                    for t in TARGETS]

        runs: list[PipelineRun] = []
        for target in TARGETS:
            count = int(sample_counts.get(target, 0))
            if count < self.min_samples:
                runs.append(PipelineRun(
                    target, False,
                    f"样本不足：{count} < {self.min_samples}，本轮不微调"))
                continue
            if target not in self.base_models:
                runs.append(PipelineRun(target, False, f"未配置 {target} 的基础模型"))
                continue
            config = FinetuneConfig(
                target=target, base_model=self.base_models[target],
                dataset_name=target, method=method)
            runs.append(PipelineRun(target, True,
                                    f"样本 {count} 条，周期已到，产出训练配置",
                                    config.as_dict()))
        return runs

    def as_dict(self) -> dict[str, Any]:
        return {"interval_days": self.interval_days, "min_samples": self.min_samples,
                "targets": list(TARGETS), "methods": list(METHODS),
                "base_models": dict(self.base_models),
                "training_in_ci": False,
                "note": "本模块只做编排与配置生成；训练需 GPU 环境，不在 CI 内执行（U28）"}
