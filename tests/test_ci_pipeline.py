"""CI 流水线的守护测试（计划书三、P0-2「CI 自动构建」）。

为什么要有它
------------
CI 配置是**最容易悄悄腐坏**的东西：它平时不跑，没人看，
新加的验证脚本也不会自动进 CI。等真去用了才发现它早就坏了。

本文件发现并守住的第一个真问题
------------------------------
ci.yml 里「生成 .env.test」那一步 import cryptography，
而 pip install -r requirements.txt 排在它**后面** —— 顺序反了，
CI 第一次运行就会 ImportError。（见 MEMORY E-26）
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".gitea" / "workflows" / "ci.yml"

#: 流水线里**必须出现**的验证命令。
#: 加新的验证脚本时，这里也要加一条 —— 否则它会悄悄留在 CI 之外。
REQUIRED_COMMANDS = {
    "python -m pytest": "契约与单元测试",
    "scripts/smoke_p0.py": "P0-1 底座冒烟",
    "scripts/smoke_p1_p4.py": "P1–P4 端到端冒烟",
}

#: 期望存在的 job
REQUIRED_JOBS = {"test", "dart-core"}


@pytest.fixture(scope="module")
def ci() -> dict:
    assert CI_PATH.exists(), f"CI 配置不存在：{CI_PATH}"
    return yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))


def _all_run_scripts(ci: dict) -> list[str]:
    scripts: list[str] = []
    for job in ci.get("jobs", {}).values():
        for step in job.get("steps", []):
            run = step.get("run")
            if run:
                scripts.append(run)
    return scripts


def test_ci_is_valid_yaml_with_expected_jobs(ci):
    assert REQUIRED_JOBS <= set(ci.get("jobs", {})), "CI 缺少必需的 job"


def test_pipeline_covers_every_verification_entry_point(ci):
    """每条验证入口都必须被 CI 调用到，否则新加的验证会悄悄留在流水线之外。"""
    joined = "\n".join(_all_run_scripts(ci))
    missing = [
        f"{cmd}（{why}）"
        for cmd, why in REQUIRED_COMMANDS.items()
        if cmd not in joined
    ]
    assert not missing, "CI 未覆盖以下验证入口：\n  " + "\n  ".join(missing)


def test_referenced_scripts_exist(ci):
    """CI 里点名的脚本必须真的存在 —— 别让流水线指向一个已改名的文件。"""
    joined = "\n".join(_all_run_scripts(ci))
    missing = [
        rel for rel in ("scripts/smoke_p0.py", "scripts/smoke_p1_p4.py",
                        "scripts/verify_voice_engine.py")
        if rel in joined and not (ROOT / rel).exists()
    ]
    assert not missing, f"CI 引用了不存在的脚本：{missing}"


def test_dependencies_install_before_anything_that_imports_them(ci):
    """装依赖必须排在「用到依赖的步骤」之前。

    这是踩过的真坑：生成 .env.test 的脚本 import cryptography，
    而它来自 requirements.txt —— 顺序反了，CI 第一次运行就 ImportError。
    """
    steps = ci["jobs"]["test"]["steps"]

    def step_text(step) -> str:
        return (step.get("run") or "")

    install_idx = [
        i for i, s in enumerate(steps)
        if "pip install -r requirements.txt" in step_text(s)
    ]
    assert install_idx, "CI 里没有安装 requirements.txt 的步骤"
    install_at = install_idx[0]

    # 「用到第三方依赖」的判据：脚本里 import 了 requirements.txt 中的包
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    requirement_names = {
        line.split("=")[0].split(">")[0].split("<")[0].strip()
        for line in requirements.splitlines()
        if line.strip() and not line.startswith("#")
    }
    requirement_names.discard("")

    offenders: list[str] = []
    for i, step in enumerate(steps):
        script = step_text(step)
        for name in requirement_names:
            # 两种导入写法都要认：
            #   import cryptography
            #   from cryptography.fernet import Fernet   ← 第一版只认了前者，
            #   结果这个守护在真正的 bug 面前**是绿的**。
            if f"import {name}" in script or f"from {name}" in script:
                if i < install_at:
                    offenders.append(
                        f"第 {i + 1} 步「{step.get('name', '?')}」用到了 {name}，"
                        f"但它排在安装依赖（第 {install_at + 1} 步）之前"
                    )
    assert not offenders, "\n".join(offenders)


def test_no_step_pins_a_github_only_action_in_the_dart_job(ci):
    """Dart job 只依赖 dart-lang/setup-dart 与 checkout，避免引入更多外部依赖。"""
    uses = [
        s.get("uses", "")
        for s in ci["jobs"]["dart-core"]["steps"]
        if s.get("uses")
    ]
    assert all(u.startswith(("actions/", "dart-lang/")) for u in uses), (
        f"Dart job 引入了预期之外的 action：{uses}"
    )


#: **有意不进 CI** 的验证脚本，必须写明理由。
#: 加新脚本时，要么让 CI 调用它，要么在这里登记 —— 两者都不做会变红。
STRUCTURAL_MARKS = {"parametrize", "skipif", "xfail",
                    "filterwarnings", "usefixtures"}

NOT_IN_CI: dict[str, str] = {
    "scripts/verify_android_artifact.py": (
        "校验的是**构建产物**（APK 里的 .so / 权限 / 前台服务 / 随包模型）；"
        "CI 只跑契约与单元测试，不打 APK（需 Flutter SDK + Android SDK + NDK）；"
        "本地构建后由发布流程执行。**这是有意排除，不是遗漏。**"),
    "scripts/verify_voice_engine.py": (
        "端侧语音引擎验证需要两件 CI 依赖面里没有的东西："
        "① sherpa-onnx 原生推理包 —— 本机 venv 已装 1.13.8、PyPI 亦有 manylinux cp312 wheel，"
        "但它不在 requirements.txt（CI 只装纯 Python 依赖；约 2GB 的 torch 系在 requirements-ml.txt）；"
        "② models/kws-zipformer-wenetspeech-3.3M 全套约 5.2MB —— models/ 整体被 .gitignore，"
        "且 U06 登记的 ModelScope 仓库 ID 实测已 404，上游位置需重新核实后才能脚本化获取。"
        "接通远端前已本地逐条验证：pytest（含 AIBUTLER_BLOCK_ML 依赖面模拟）、smoke_p0、"
        "smoke_p1_p4、场景库覆盖率全部通过，**唯一必红的就是这一步** —— "
        "留着它等于让 CI 上线即失败。**这是有意排除，不是遗漏。** "
        "接入前置条件（两项独立可验证）：requirements.txt 声明 sherpa-onnx；"
        "为 KWS 模型补一个上游可核实的获取步骤。"),
}


def test_every_verification_script_is_either_in_ci_or_explicitly_excluded(ci):
    """每个 smoke_*/verify_* 脚本都必须有归属：CI 调用，或登记为有意排除。

    REQUIRED_COMMANDS 是**手工维护**的清单 —— 新加脚本没人提醒你。
    这条反过来扫**磁盘上真实存在的脚本**，逼着做一次分类：
    要么进 CI，要么写清为什么不进。
    """
    on_disk = sorted(
        q.name for q in (ROOT / "scripts").glob("*.py")
        if q.name.startswith(("smoke_", "verify_")))
    assert on_disk, "scripts/ 下一个 smoke_*/verify_* 都没找到（扫描规则可能已失效）"

    joined = "\n".join(_all_run_scripts(ci))
    problems: list[str] = []
    for name in on_disk:
        rel = "scripts/" + name
        if rel in joined:
            continue
        reason = NOT_IN_CI.get(rel)
        if reason is None:
            problems.append(rel + " 既不在 CI 里，也没有登记排除理由")
        elif len(reason) < 20:
            problems.append(rel + " 的排除理由过于简略")
    assert not problems, "以下验证脚本没有归属：\n  " + "\n  ".join(problems)


def test_excluded_scripts_still_exist():
    """登记为「有意排除」的脚本必须真的存在 —— 改名后的漏网之鱼会在这里露出来。"""
    missing = [rel for rel in NOT_IN_CI if not (ROOT / rel).exists()]
    assert not missing, "排除名单里的脚本已不存在：" + str(missing)


def test_live_marker_is_the_only_deliberate_test_exclusion():
    """CI 不跑带 live 标记的用例 —— 这条把「有意跳过」写成断言。

    那三条要连**真实的 Ollama 服务**与**本地 BGE 模型**（数百 MB，不入库），
    CI 里跑不了。但此前**没有任何地方说明这一点**，
    读者会以为 CI 覆盖了全部测试。

    只看**会造成排除**的标记；parametrize / skipif 之类是结构性的，
    不参与 -m 选择（第一版把它们也算进来，误报了一次）。
    """
    marks = set()
    for f in (ROOT / "tests").glob("test_*.py"):
        for line in f.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("@pytest.mark."):
                marks.add(s[len("@pytest.mark."):].split("(")[0].strip())
    selective = marks - STRUCTURAL_MARKS
    assert selective == {"live"}, (
        "出现了 live 之外的**选择性**标记：" + str(sorted(selective - {"live"})) +
        " —— 请确认 CI 是否会跑到它们；不会的话要在此处登记理由")
