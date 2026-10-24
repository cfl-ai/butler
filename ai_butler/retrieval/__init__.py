"""检索侧规划包（查询分类 + 工具选择）。

与 `material_center`（检索执行）分工：
- `material_center.routing`：判「走哪条检索路线」（U46）
- `retrieval.planner`：判「属于哪类查询、选哪个工具」（U66）
"""

from ai_butler.retrieval.planner import (
    CATEGORY_TOOLS,
    TOOLS,
    PlanError,
    PlannedStep,
    QueryCategory,
    RetrievalMethod,
    RetrievalPlan,
    classify,
    describe_categories,
    describe_tools,
    plan,
    tools_for,
)

__all__ = [
    "CATEGORY_TOOLS", "TOOLS", "PlanError", "PlannedStep", "QueryCategory",
    "RetrievalMethod", "RetrievalPlan", "classify", "describe_categories",
    "describe_tools", "plan", "tools_for",
]
