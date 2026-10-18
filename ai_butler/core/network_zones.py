"""三层网络隔离。

计划书依据
----------
- 三、P0-1「三层网络隔离：外网业务层、内网服务层、沙箱隔离层」
- 三、P10「对接 AI Infra 隔离沙箱，支持代码修改、命令执行、单元测试」
- 三、P10「离线链路绝对不能访问原始未脱敏用户私有数据」

分区口径
--------
- edge     外网业务层：唯一对外可达；承载 API 入口。
- internal 内网服务层：预发布/生产严格无外网出口；PostgreSQL / Redis 只允许挂这一层。
- sandbox  沙箱隔离层：**任何环境都严格 internal**，承载离线自进化链路执行环境；
           不得与 internal 共享成员，确保沙箱内代码执行够不到用户私有数据。

关于本地环境
-----------
Docker **不会**为「只挂 internal 网络」的容器完成端口转发（对照实验见 MEMORY.md E-07），
而本地开发/测试需要宿主可达数据面。因此模板把 internal 写成环境驱动的
（见 environments.Environment.data_plane_internal）：预发布/生产为 true，本地为 false。
沙箱隔离层不受该开关影响，始终严格。校验器接受并显式检查这一开关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EDGE = "edge"
INTERNAL = "internal"
SANDBOX = "sandbox"

INTERNAL_ONLY_SERVICES: tuple[str, ...] = ("postgres", "redis")
SANDBOX_ONLY_SERVICES: tuple[str, ...] = ("sandbox", "harness")


@dataclass(frozen=True)
class Zone:
    name: str
    internal: bool
    description: str


ZONES: dict[str, Zone] = {
    EDGE: Zone(EDGE, False, "外网业务层：唯一对外可达，承载 API 入口"),
    INTERNAL: Zone(INTERNAL, True, "内网服务层：无外网出口，数据面只在此层"),
    SANDBOX: Zone(SANDBOX, True, "沙箱隔离层：离线进化链路执行环境，与内网服务层不共享成员"),
}


def _attached(svc: dict[str, Any]) -> list[str]:
    attached = svc.get("networks") or []
    if isinstance(attached, dict):
        return list(attached.keys())
    return list(attached)


def _declared_internal(zone: Any) -> bool:
    """internal 可能是字面量 true，也可能是 ${DATA_PLANE_INTERNAL:-true} 这类占位。"""
    if not isinstance(zone, dict):
        return False
    value = zone.get("internal", False)
    if isinstance(value, bool):
        return value
    return ":-true" in str(value)


def validate_compose(doc: dict[str, Any], *, data_plane_internal: bool = True) -> list[str]:
    """静态校验一个 compose 文档是否满足三层网络隔离。返回违规描述（空表示合规）。"""
    violations: list[str] = []
    networks = doc.get("networks") or {}
    services = doc.get("services") or {}

    # 沙箱隔离层任何环境都必须严格；内网服务层仅在严格模式下要求
    must_be_internal = [SANDBOX] + ([INTERNAL] if data_plane_internal else [])
    for name in must_be_internal:
        zone = networks.get(name)
        if zone is None:
            violations.append(f"缺少网络分区 {name}")
        elif not _declared_internal(zone):
            violations.append(f"网络分区 {name} 必须声明 internal: true")

    if EDGE not in networks:
        violations.append(f"缺少网络分区 {EDGE}")

    for svc_name, svc in services.items():
        attached = _attached(svc)

        if svc_name in INTERNAL_ONLY_SERVICES:
            if INTERNAL not in attached:
                violations.append(f"数据面服务 {svc_name} 必须挂在 {INTERNAL}")
            extra = [n for n in attached if n != INTERNAL]
            if extra:
                violations.append(f"数据面服务 {svc_name} 不得挂到 {extra}")
            if data_plane_internal and svc.get("ports"):
                violations.append(f"严格内网模式下数据面服务 {svc_name} 不得发布宿主端口")

        if svc_name in SANDBOX_ONLY_SERVICES and INTERNAL in attached:
            violations.append(f"沙箱服务 {svc_name} 不得挂到 {INTERNAL}（沙箱必须与数据面隔离）")

        if SANDBOX in attached and INTERNAL in attached:
            violations.append(f"服务 {svc_name} 同时挂在 {SANDBOX} 与 {INTERNAL}，破坏沙箱隔离")

    return violations
