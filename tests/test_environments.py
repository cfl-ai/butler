"""四环境隔离测试。

计划书依据：三、P0-1「搭建四环境隔离体系：开发、测试、预发布、生产完全独立隔离」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_butler.core import environments as envs

ROOT = Path(__file__).resolve().parents[1]


def _read_env_file(name: str) -> dict[str, str]:
    kv: dict[str, str] = {}
    for line in (ROOT / name).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def test_four_environments_are_declared():
    assert envs.ENV_NAMES == ("dev", "test", "staging", "prod")
    assert set(envs.ENVIRONMENTS) == set(envs.ENV_NAMES)


def test_environments_share_no_resource():
    """「完全独立隔离」的可执行定义：任一维度都不相交。"""
    envs.assert_disjoint()


def test_unknown_environment_is_refused():
    with pytest.raises(envs.EnvironmentIsolationError):
        envs.get("sandbox")


def test_runtime_guard_accepts_matching_resources():
    e = envs.get("dev")
    envs.validate_runtime("dev", postgres_port=e.postgres_port,
                          postgres_db=e.postgres_db, redis_port=e.redis_port)


def test_runtime_guard_catches_development_config_pointed_at_production():
    """最危险的一类事故：用开发配置连上了生产库。"""
    prod = envs.get("prod")
    with pytest.raises(envs.EnvironmentIsolationError) as ei:
        envs.validate_runtime("dev", postgres_port=prod.postgres_port,
                              postgres_db=prod.postgres_db, redis_port=prod.redis_port)
    msg = str(ei.value)
    assert "跨环境串用" in msg and "PostgreSQL 端口" in msg


def test_only_local_environments_expose_host_ports():
    assert envs.get("dev").exposes_host_ports is True
    assert envs.get("test").exposes_host_ports is True
    assert envs.get("staging").exposes_host_ports is False
    assert envs.get("prod").exposes_host_ports is False


def test_every_environment_has_its_own_config_file():
    for env in envs.ENVIRONMENTS.values():
        assert (ROOT / env.env_file).is_file(), f"缺少 {env.env_file}"


def test_settings_bind_to_the_test_environment():
    """测试夹具连的是 test 环境，且资源与环境声明一致（否则测试会打到 dev 库）。"""
    import os

    from ai_butler.core.config import load_settings

    os.environ["AIBUTLER_ENV"] = str(ROOT / ".env.test")
    s = load_settings()
    assert s.environment == "test"
    envs.validate_runtime(s.environment, postgres_port=s.postgres_port,
                          postgres_db=s.postgres_db, redis_port=s.redis_port)


def test_secrets_are_not_shared_between_environments():
    """四环境密钥必须各自独立；预发布/生产不得复用开发的外部凭据。"""
    data = {name: _read_env_file(f".env.{name}") for name in envs.ENV_NAMES}

    for field in ("SESSION_TOKEN_SECRET", "FIELD_ENCRYPTION_KEY", "POSTGRES_PASSWORD"):
        values = [data[n][field] for n in envs.ENV_NAMES]
        assert len(set(values)) == 4, f"{field} 在四环境间出现重复"
        assert all(len(v) >= 16 for v in values), f"{field} 长度不足"

    for name in ("dev", "test"):
        assert data[name]["DEEPSEEK_API_KEY"], f"{name} 应持有本地开发凭据"
    for name in ("staging", "prod"):
        assert not data[name]["DEEPSEEK_API_KEY"], f"{name} 不得复用开发的外部凭据"


def _need(data: dict, key: str, where: str) -> str:
    """取一个必需键；缺失时点名**哪个文件缺哪个键**。

    裸 data[key] 抛的 KeyError 不告诉你是哪个文件，
    在四个环境里排查要多跑几轮。
    """
    if key not in data:
        raise AssertionError(where + " 缺少必需变量 " + key)
    return data[key]


def test_env_files_match_their_environment_declaration():
    """每个 `.env.*` 里的资源值必须与 environments 的声明一致。

    此前只验了「四个文件都存在」与「密钥互不相同」，
    **文件里的端口 / 库 / 存储根从未被校验过** ——
    把 .env.prod 改成 dev 的端口与库，不会有任何测试变红。
    而「四环境完全独立隔离」是一条计划书硬要求（三、P0-1）。
    """
    for name in envs.ENV_NAMES:
        decl = envs.get(name)
        data = _read_env_file(decl.env_file)

        assert _need(data, "ENVIRONMENT", decl.env_file) == name, (
            f"{decl.env_file} 声明的环境是 {data['ENVIRONMENT']!r}，应为 {name!r}")
        assert int(_need(data, "POSTGRES_PORT", decl.env_file)) == decl.postgres_port
        assert int(_need(data, "REDIS_PORT", decl.env_file)) == decl.redis_port
        assert _need(data, "POSTGRES_DB", decl.env_file) == decl.postgres_db
        # API_PORT 同样由 local-ports.yml 引用，必须与声明一致
        assert int(_need(data, "API_PORT", decl.env_file)) == decl.api_port
        assert _need(data, "STORAGE_ROOT", decl.env_file) == f"{decl.data_root}/storage"
        assert _need(data, "WIKI_ROOT", decl.env_file) == f"{decl.data_root}/agent_wiki"


def test_env_files_share_no_resource_either():
    """四个**文件**之间也不得共用资源 —— 不能只看声明。

    `assert_disjoint()` 校验的是声明对象；这里校验的是**真正被加载的文件**。
    两者都要成立，「完全独立隔离」才算落实。
    """
    data = {name: _read_env_file(f".env.{name}") for name in envs.ENV_NAMES}
    fields = ("POSTGRES_PORT", "REDIS_PORT", "API_PORT", "POSTGRES_DB",
              "STORAGE_ROOT", "WIKI_ROOT")
    for field in fields:
        values = [data[name][field] for name in envs.ENV_NAMES]
        assert len(set(values)) == 4, (
            f"{field} 在四环境文件间出现重复：{dict(zip(envs.ENV_NAMES, values))}")


def test_every_variable_referenced_by_compose_exists_in_every_env_file():
    """compose 里用 ${VAR} 引用的变量，四个 env 文件都必须提供。

    compose 对**未定义变量**的处理是静默替换成空串 ——
    `${API_PORT}:8000` 会变成 `:8000`，配置错了却不报错，
    直到 docker compose 起不来或起错端口才被发现。
    """
    import re

    compose = (ROOT / "deploy" / "compose" / "local-ports.yml").read_text(
        encoding="utf-8")
    referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", compose))
    assert referenced, "local-ports.yml 里没解析到任何 ${VAR} 引用（解析规则可能已失效）"

    problems: list[str] = []
    for name in envs.ENV_NAMES:
        data = _read_env_file(f".env.{name}")
        for var in sorted(referenced):
            if not data.get(var):
                problems.append(f".env.{name} 缺少 {var}")
    assert not problems, (
        "compose 引用了但 env 文件没提供的变量：\n  " + "\n  ".join(problems))
