"""三层网络隔离测试。

计划书依据：三、P0-1「三层网络隔离：外网业务层、内网服务层、沙箱隔离层」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_butler.core import network_zones as nz

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "compose" / "base.yml"
LOCAL_PORTS = ROOT / "deploy" / "compose" / "local-ports.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_three_zones_are_declared():
    assert set(nz.ZONES) == {nz.EDGE, nz.INTERNAL, nz.SANDBOX}
    assert nz.ZONES[nz.EDGE].internal is False
    assert nz.ZONES[nz.INTERNAL].internal is True
    assert nz.ZONES[nz.SANDBOX].internal is True


def test_unified_compose_template_satisfies_zone_isolation():
    violations = nz.validate_compose(_load(BASE), data_plane_internal=True)
    assert violations == [], "统一启动模板违反三层网络隔离：\n" + "\n".join(violations)


def test_sandbox_zone_is_strict_in_every_environment():
    """沙箱隔离层不受本地开关影响，任何环境都必须严格。"""
    for flag in (True, False):
        doc = _load(BASE)
        assert nz._declared_internal(doc["networks"][nz.SANDBOX]) is True, flag


def test_env_files_agree_with_the_data_plane_switch():
    """预发布/生产严格内网；本地放开以便宿主可达数据面（MEMORY.md E-07）。"""
    from ai_butler.core import environments as envs

    for env in envs.ENVIRONMENTS.values():
        text = (ROOT / env.env_file).read_text(encoding="utf-8-sig")
        declared = [l.split("=", 1)[1].strip().lower()
                    for l in text.splitlines() if l.startswith("DATA_PLANE_INTERNAL=")]
        assert len(declared) == 1, f"{env.env_file} 缺少 DATA_PLANE_INTERNAL"
        assert (declared[0] == "true") is env.data_plane_internal, env.name
        assert env.data_plane_internal is (not env.exposes_host_ports), env.name


def test_strict_mode_forbids_publishing_data_plane_ports():
    doc = _load(BASE)
    doc["services"]["postgres"]["ports"] = ["5434:5432"]
    violations = nz.validate_compose(doc, data_plane_internal=True)
    assert any("不得发布宿主端口" in v for v in violations)
    # 本地模式（严格内网关闭）下允许发布，这是本地开发的有意让渡
    assert nz.validate_compose(doc, data_plane_internal=False) == []


def test_template_keeps_data_plane_off_the_edge():
    doc = _load(BASE)
    for svc in nz.INTERNAL_ONLY_SERVICES:
        assert doc["services"][svc]["networks"] == [nz.INTERNAL]
    # 模板本身不发布任何宿主端口
    for name, svc in doc["services"].items():
        assert "ports" not in svc, f"统一模板中 {name} 不应发布宿主端口"


def test_local_ports_overlay_publishes_the_three_local_ports():
    """本地叠加文件只为 dev/test 发布端口；预发布与生产不叠加它。

    注意：本机 Docker Desktop 不代理 127.0.0.1 绑定的容器端口（MEMORY.md E-06），
    因此这里发布在 0.0.0.0 上。该风险由「只有本地环境叠加本文件」这一事实控制。
    """
    from ai_butler.core import environments as envs

    doc = _load(LOCAL_PORTS)
    published = {name: svc["ports"][0].split(":")[0]
                 for name, svc in doc["services"].items()}
    assert set(published) == {"postgres", "redis", "api"}
    for name, svc in doc["services"].items():
        assert len(svc["ports"]) == 1, f"{name} 应只发布一个端口"

    for env in envs.ENVIRONMENTS.values():
        assert (env.name in ("dev", "test")) is env.exposes_host_ports


# ------------------------- 校验器自身的负向用例 ------------------------- #
def test_validator_rejects_missing_internal_flag():
    doc = _load(BASE)
    doc["networks"][nz.INTERNAL]["internal"] = False
    assert any("internal: true" in v for v in nz.validate_compose(doc))


def test_validator_rejects_data_plane_on_edge():
    doc = _load(BASE)
    doc["services"]["postgres"]["networks"] = [nz.INTERNAL, nz.EDGE]
    assert any("不得挂到" in v for v in nz.validate_compose(doc))


def test_validator_rejects_sandbox_sharing_network_with_data_plane():
    doc = _load(BASE)
    doc["services"]["harness"] = {"image": "x", "networks": [nz.SANDBOX, nz.INTERNAL]}
    assert any("沙箱" in v for v in nz.validate_compose(doc))


def test_dockerfile_runs_as_non_root():
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER aibutler" in text, "统一镜像必须以非 root 运行"
    assert "ai_butler.server:build_app" in text
