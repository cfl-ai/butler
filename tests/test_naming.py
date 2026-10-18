"""命名规范测试。

计划书依据：三、P0-1「输出统一架构规范文档，全项目强制执行」；
六、全体系可复用资产库（长期生态复用）；五、垂类复用同一套结构。
"""

from __future__ import annotations

import re
from pathlib import Path

from ai_butler.core import naming

PKG_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = [PKG_ROOT / "ai_butler", PKG_ROOT / "tests", PKG_ROOT / "scripts"]


def test_identifiers_derive_from_plan_title():
    """命名根必须能回溯到计划书标题「AI管家 Agent 生态体系」。"""
    d = naming.describe()
    assert d["ROOT"] == "ai-butler"                       # ← 标题「AI管家」
    assert d["ECOSYSTEM"] == "ai-butler-ecosystem"        # ← 标题全文
    assert d["PACKAGE"] == "ai_butler"
    assert d["NAMESPACE"] == "aibutler"
    assert d["ENV_PREFIX"] == "AIBUTLER_"
    assert d["DB_NAME"] == "ai_butler"


def test_key_and_container_builders_use_namespace():
    assert naming.redis_key("session", "abc") == f"{naming.NAMESPACE}:session:abc"
    assert naming.redis_key("ratelimit", "llm", "u-1", 100) == f"{naming.NAMESPACE}:ratelimit:llm:u-1:100"
    assert naming.container("pg") == f"{naming.NAMESPACE}-pg"
    assert naming.env("ENV") == f"{naming.ENV_PREFIX}ENV"


def test_vertical_namespace_is_reusable_without_new_rules():
    """计划书五：点餐、售后等垂类复用同一套结构 → 新增垂类不新增命名规则。"""
    for vertical in ("travel", "food", "aftersale"):
        key = naming.vertical_key(vertical, "session", "abc")
        assert key == f"{naming.NAMESPACE}:{vertical}:session:abc"


def test_compose_identifiers_match_naming_spec():
    """漂移防护：四环境的项目名与容器名必须由命名规范派生，且与环境表一致。

    后期改用 AIBUTLER_NAMESPACE / AIBUTLER_ECOSYSTEM 调整命名时，
    若忘记同步 deploy/compose 或 .env.<环境>，本用例会立即失败。
    """
    import yaml

    from ai_butler.core import environments as envs

    base = yaml.safe_load(
        (PKG_ROOT / "deploy" / "compose" / "base.yml").read_text(encoding="utf-8"))
    assert base["name"] == "${COMPOSE_PROJECT_NAME}", "项目名必须由环境文件提供"
    for svc, suffix in (("postgres", "pg"), ("redis", "redis"), ("api", "api")):
        assert base["services"][svc]["container_name"] == "${CONTAINER_PREFIX}-" + suffix

    for env in envs.ENVIRONMENTS.values():
        text = (PKG_ROOT / env.env_file).read_text(encoding="utf-8-sig")
        kv = dict(line.split("=", 1) for line in text.splitlines()
                  if line and not line.startswith("#") and "=" in line)
        assert kv["COMPOSE_PROJECT_NAME"] == env.project_name, env.name
        assert kv["CONTAINER_PREFIX"] == env.container_prefix, env.name
        # 容器名前缀与项目名必须落在命名规范的命名空间下
        assert env.container_prefix.startswith(naming.NAMESPACE), env.name
        assert env.project_name.startswith(naming.ROOT), env.name


def test_no_hardcoded_identifier_outside_naming_module():
    """业务代码不得硬编码命名标识——后期改名只改 naming.py。"""
    offenders: list[str] = []
    for base in SCAN_ROOTS:
        for p in base.rglob("*.py"):
            if p.name == "naming.py" or "__pycache__" in str(p):
                continue
            txt = p.read_text(encoding="utf-8")
            if "test_naming" in p.name:
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if re.search(r'["\']ai-butler[:"]', line):
                    offenders.append(f"{p.relative_to(PKG_ROOT)}:{i}: {line.strip()[:80]}")
    assert not offenders, "发现硬编码命名标识：\n" + "\n".join(offenders)
