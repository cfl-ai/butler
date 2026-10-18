"""P7 Android 产物验证：确认 APK 里真的带着端侧语音的完整原生栈。

为什么单独有这么一个脚本
------------------------
「构建成功」这四个字不足以证明「端侧语音能在真机上跑」。
APK 里必须真的含有 sherpa-onnx 与 onnxruntime 的原生库，
否则 App 装上去也只是一个没有耳朵的空壳。

判据（都是可机检的，不靠肉眼）
------------------------------
- 每个 APK 都必须有 AndroidManifest.xml / classes.dex / resources.arsc；
- release 的 arm64 / armeabi-v7a / x86_64 都必须含
  libsherpa-onnx-c-api.so 与 libonnxruntime.so；
- 每个 APK 的 lib 目录下，.so 必须与它声明的 ABI 一致。

产物不存在时**不算失败也不算通过** —— 报「未构建」并以 2 退出，
以免把「没构建」误读成「构建有问题」。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APK_DIR = ROOT / "mobile" / "build" / "app" / "outputs" / "flutter-apk"

#: 每个 APK 必须含有的条目
REQUIRED_COMMON = ("AndroidManifest.xml", "classes.dex", "resources.arsc")

#: 必须随包下发的端侧语音原生库（裁定 U06 / U20）
REQUIRED_NATIVE = ("libsherpa-onnx-c-api.so", "libonnxruntime.so")

#: 期望的 release 产物（ABI 名 -> 文件名）
RELEASE_APKS = {
    "arm64-v8a": "app-arm64-v8a-release.apk",
    "armeabi-v7a": "app-armeabi-v7a-release.apk",
    "x86_64": "app-x86_64-release.apk",
}

#: P7 必须声明的权限。缺任何一条，语音在真机上都用不了。
#: 依据：三、P7（离线唤醒 / 随时打断 / 设备采集）+ 裁定 U23（Android 14 前台服务限制）。
REQUIRED_PERMISSIONS = {
    "android.permission.INTERNET": "云端交互",
    "android.permission.RECORD_AUDIO": "语音输入与离线唤醒",
    "android.permission.FOREGROUND_SERVICE": "后台常驻唤醒（Android 9+）",
    "android.permission.FOREGROUND_SERVICE_MICROPHONE": "后台常驻唤醒（Android 14+ 强制）",
    "android.permission.POST_NOTIFICATIONS": "前台服务通知（Android 13+）",
    "android.permission.WAKE_LOCK": "后台保活",
    "android.permission.ACCESS_FINE_LOCATION": "设备采集",
    "android.permission.ACCESS_COARSE_LOCATION": "设备采集",
}

#: 后台唤醒的前台服务必须声明 microphone 类型（Android 14+ 否则启动即抛异常）
REQUIRED_SERVICE_TYPE = "microphone"

#: 唤醒词模型必须**随包下发**（裁定 U06 要的是离线唤醒；
#: 靠首次启动下载的话，第一次断网就唤不醒）。
REQUIRED_WAKE_MODEL = (
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "tokens.txt",
    "keywords.txt",
)

#: **不得随包下发**的唤醒词：模型仓自带的这份是别家产品的商标。
FORBIDDEN_WAKE_WORDS = ("小爱同学", "小艺小艺", "小米小米", "你好军哥", "蛋哥蛋哥", "你好问问")


def _check_apk(path: Path, expect_abi: str | None) -> list[str]:
    """返回该 APK 的问题列表（空表示通过）。"""
    problems: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for required in REQUIRED_COMMON:
            if required not in names:
                problems.append(f"{path.name} 缺少 {required}")

        native = {n.rsplit("/", 1)[-1]: n for n in names if n.endswith(".so")}
        for required in REQUIRED_NATIVE:
            if required not in native:
                problems.append(f"{path.name} 缺少原生库 {required}")

        if expect_abi is not None:
            ours = [n for n in names if n.endswith(".so") and n.startswith("lib/")]
            wrong = [n for n in ours if n.split("/")[1] != expect_abi]
            if wrong:
                problems.append(
                    f"{path.name} 里的 .so 与声明的 ABI（{expect_abi}）不符：{wrong[:3]}")
    return problems


def _find_aapt2() -> str | None:
    """找一个可用的 aapt2。找不到就跳过清单检查（并说明跳过了）。"""
    candidates = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env_name)
        if root:
            candidates.extend(Path(root).glob("build-tools/*/aapt2.exe"))
            candidates.extend(Path(root).glob("build-tools/*/aapt2"))
    found = shutil.which("aapt2")
    if found:
        candidates.append(Path(found))
    existing = [c for c in candidates if c.exists()]
    if not existing:
        return None
    # 版本号大的优先
    existing.sort(key=lambda p: p.parent.name, reverse=True)
    return str(existing[0])


def _check_manifest(apk: Path, aapt2: str) -> list[str]:
    """用 aapt2 读 APK 的清单，核对权限与前台服务类型。"""
    problems: list[str] = []
    try:
        badging = subprocess.run(
            [aapt2, "dump", "badging", str(apk)],
            capture_output=True, text=True, timeout=120, errors="replace",
        )
        xmltree = subprocess.run(
            [aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)],
            capture_output=True, text=True, timeout=120, errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [f"aapt2 执行失败：{error}"]

    declared = set(re.findall(r"uses-permission: name='([^']+)'", badging.stdout))
    for permission, why in REQUIRED_PERMISSIONS.items():
        if permission not in declared:
            problems.append(f"{apk.name} 未声明权限 {permission}（{why}）")

    if REQUIRED_SERVICE_TYPE not in xmltree.stdout:
        problems.append(
            f"{apk.name} 的清单里没有 foregroundServiceType={REQUIRED_SERVICE_TYPE} 的服务"
            "（Android 14+ 启动前台服务会抛 SecurityException）")
    return problems


def _check_wake_model(apk: Path) -> list[str]:
    """唤醒词模型必须随包，且不得夹带别家产品的唤醒词。"""
    problems: list[str] = []
    with zipfile.ZipFile(apk) as z:
        entries = {n.rsplit("/", 1)[-1]: n for n in z.namelist() if "models/kws/" in n}

        for name in REQUIRED_WAKE_MODEL:
            if name not in entries:
                problems.append(f"{apk.name} 未随包下发唤醒词模型文件 {name}")

        for name, entry in entries.items():
            if not name.endswith(".txt"):
                continue
            text = z.read(entry).decode("utf-8", errors="replace")
            for word in FORBIDDEN_WAKE_WORDS:
                if word in text:
                    problems.append(
                        f"{apk.name} 的 {name} 含第三方唤醒词「{word}」——"
                        "模型仓自带的这份是别家产品商标，不得随包下发")
    return problems


def main() -> int:
    print("=" * 68)
    print("P7 Android 产物验证（APK 内的端侧语音原生栈）")
    print("=" * 68)

    if not APK_DIR.exists():
        print(f"  未找到产物目录：{APK_DIR.relative_to(ROOT)}")
        print("  → 尚未构建。请先执行 flutter build apk（见统一架构规范文档第 32/33 节）。")
        return 2

    apks = sorted(APK_DIR.glob("*.apk"))
    if not apks:
        print(f"  {APK_DIR.relative_to(ROOT)} 下没有 .apk → 尚未构建。")
        return 2

    aapt2 = _find_aapt2()
    if aapt2:
        print(f"  清单检查用：{aapt2}")
    else:
        print("  未找到 aapt2 → **跳过权限与服务检查**（结论仅覆盖 .so 与条目）")

    problems: list[str] = []
    for abi, filename in RELEASE_APKS.items():
        path = APK_DIR / filename
        if not path.exists():
            problems.append(f"缺少 release 产物：{filename}")
            continue
        found = _check_apk(path, expect_abi=abi)
        found += _check_wake_model(path)
        if aapt2:
            found += _check_manifest(path, aapt2)
        problems.extend(found)
        size_mb = path.stat().st_size / 1024 / 1024
        mark = "PASS" if not found else "FAIL"
        print(f"  [{mark}] {filename:32s} {size_mb:6.1f} MB")

    debug_apk = APK_DIR / "app-debug.apk"
    if debug_apk.exists():
        found = _check_apk(debug_apk, expect_abi=None)
        found += _check_wake_model(debug_apk)
        if aapt2:
            found += _check_manifest(debug_apk, aapt2)
        problems.extend(found)
        size_mb = debug_apk.stat().st_size / 1024 / 1024
        print(f"  [{'PASS' if not found else 'FAIL'}] {'app-debug.apk':32s} {size_mb:6.1f} MB")

    print("=" * 68)
    if problems:
        print(f"结论：FAIL（{len(problems)} 项）")
        for p in problems:
            print("  - " + p)
        return 1
    print("结论：PASS —— 端侧语音原生栈已随包下发，且真机所需的声明已齐备")
    print()
    print("本脚本覆盖的是「声明层」：")
    print("  · 原生库是否随包（.so）")
    print("  · 清单是否声明了 P7 需要的全部权限")
    print("  · 后台唤醒服务是否声明了 foregroundServiceType=microphone")
    print("  · 唤醒词模型是否随包（离线唤醒的前提），且不含第三方商标")
    print()
    print("本脚本**不覆盖**的是「运行时层」，只有真机能回答（裁定 U23）：")
    print("  · 用户是否真的授予了麦克风权限")
    print("  · 前台服务在 Android 14+ 上能否长期存活、会不会被系统回收")
    print("  · 实际功耗，以及揣在兜里能否稳定唤醒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
