r"""本地开发后端：起服务、常驻、自愈、可停可查。

为什么需要它
------------
1. Web 对话页（`ai_butler/web/index.html`）相对路径调 `/api/*`，**只能**从后端
   进程打开（`/ui` 路由）。在 IDE 里点开 HTML 得到的是内置静态服务器
   （`http://localhost:<随机端口>/<项目名>/...`），那个源上没有 `/api/*`，
   登录/访客入口一律 404。
2. 直接 `python -m uvicorn ...` 只活在那一个终端窗口里：窗口一关、进程一崩，
   页面就回到上面的 404 状态，而人往往以为是自己密码敲错了。

所以这里固定一条路径：**前台看得见日志、后台能常驻、崩了会自己回来、
随时能查能停**。

用法
----
    .venv\Scripts\python.exe scripts\run_dev.py             # 前台跑（Ctrl+C 停）
    .venv\Scripts\python.exe scripts\run_dev.py --detach    # 后台常驻（关掉终端也不死）
    .venv\Scripts\python.exe scripts\run_dev.py --status    # 在跑吗？健康吗？
    .venv\Scripts\python.exe scripts\run_dev.py --stop      # 停掉

    可选：--port 8411  --host 127.0.0.1  --env-file .env.dev
          --no-browser  --reload  --no-restart  --wait 60

退出码：0 正常 / 1 停止或状态异常 / 2 参数或环境问题（端口占用、环境文件缺失）。
"""

from __future__ import annotations

# 临时目录自愈：系统临时目录不可写时改用工作区目录，
# 否则 chromadb 的 rust 绑定会报「unable to open database file」（见 MEMORY E-14）。
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(_ROOT))
from ai_butler.devkit.tempdir import ensure_writable_temp as _ensure_temp  # noqa: E402

_ensure_temp(_ROOT)

import argparse  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = _ROOT
DEFAULT_PORT = 8411
RUN_DIR = ROOT / ".run"
LOG_DIR = ROOT / "logs"

#: 崩溃重启的退避上限。1→2→4→8→15…；稳定跑满 RESTART_RESET 秒就把退避重置，
#: 免得一次偶发崩溃让后面每次重启都等 15 秒。
BACKOFF_CAP = 15.0
BACKOFF_RESET_AFTER = 30.0

#: Windows 的「脱离控制台」组合：DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP。
#: 少了它，父终端一关，子进程跟着收到 CTRL_CLOSE 就死了 —— 那正是要避免的。
_WIN_DETACHED = 0x00000008 | 0x00000200


# ---------------------------------------------------------------- 路径与小工具

def pid_path(port: int) -> Path:
    return RUN_DIR / f"dev_server_{port}.pid"


def log_path(port: int) -> Path:
    return LOG_DIR / f"dev_server_{port}.log"


def base_url(host: str, port: int) -> str:
    h = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{h}:{port}"


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """先自己判一次占用，把「换个端口」这句话说在该说的位置。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex((host, port)) == 0


def health_ok(base: str) -> bool:
    """只判端口连通不够：端口上可能是别的进程（比如 IDE 的静态服务器），
    它连得上，却答不出 `ecosystem`。这个区分是排查那次 404 的关键。
    """
    try:
        with urllib.request.urlopen(base + "/health", timeout=2) as resp:  # noqa: S310 本机固定地址
            return "ecosystem" in resp.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError):
        return False


def wait_health(base: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_ok(base):
            return True
        time.sleep(0.25)
    return False


def read_pid(port: int) -> int | None:
    p = pid_path(port)
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, errors="ignore")
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    """连子进程一起收掉。

    只用 terminate 不保险：uvicorn 一旦开了 reload/子 worker，父进程走了
    子进程还握着端口，下次启动就报占用 —— 这种「停不干净」最难查。
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, errors="ignore")
        return
    import signal as _signal
    try:
        os.killpg(os.getpgid(pid), _signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError:
            pass


def uvicorn_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, "-m", "uvicorn", "ai_butler.server:build_app",
           "--factory", "--host", args.host, "--port", str(args.port)]
    if args.reload:
        cmd += ["--reload", "--reload-dir", str(ROOT / "ai_butler")]
    return cmd


# ---------------------------------------------------------------- 监督循环

def supervise(args: argparse.Namespace, open_browser: bool) -> int:
    """起一个 uvicorn 子进程并守着它：崩了按退避重启，Ctrl+C 干净收场。

    为什么是子进程而不是 `uvicorn.run()` 同进程：同进程崩了就是整个脚本崩，
    没有「重启」可言；而且子进程的输出可以直接继承控制台/日志文件，不必
    自己搭一套管道泵。
    """
    env = dict(os.environ)
    env["AIBUTLER_ENV"] = str(ROOT / args.env_file)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    pid_path(args.port).write_text(str(os.getpid()), encoding="utf-8")

    base = base_url(args.host, args.port)
    if open_browser and not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(base, args.wait), daemon=True).start()

    backoff = 1.0
    try:
        while True:
            started = time.time()
            proc = subprocess.Popen(uvicorn_cmd(args), cwd=str(ROOT), env=env)
            try:
                code = proc.wait()
            except KeyboardInterrupt:
                print("\n[run_dev] 收到中断，正在停止…", flush=True)
                kill_tree(proc.pid)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                return 0

            if args.no_restart:
                print(f"[run_dev] 后端退出（code={code}），--no-restart 已指定，不再拉起", flush=True)
                return 0 if code == 0 else 1

            uptime = time.time() - started
            if uptime >= BACKOFF_RESET_AFTER:
                backoff = 1.0
            print(f"[run_dev] 后端退出（code={code}，存活 {uptime:.0f}s），"
                  f"{backoff:.0f}s 后重启…", flush=True)
            try:
                time.sleep(backoff)
            except KeyboardInterrupt:
                # 退避期间按 Ctrl+C 也该干净退出，不能甩一个 traceback 给用户。
                print("\n[run_dev] 收到中断，已停止", flush=True)
                return 0
            backoff = min(backoff * 2, BACKOFF_CAP)
    finally:
        try:
            if read_pid(args.port) == os.getpid():
                pid_path(args.port).unlink()
        except OSError:
            pass


def _open_when_ready(base: str, timeout: float) -> None:
    if not wait_health(base, timeout):
        print(f"[run_dev] {timeout:.0f}s 内 /health 没通，浏览器不自动打开。"
              f"手动确认：{base}/health", flush=True)
        return
    print(f"[run_dev] 后端就绪 → 打开 {base}/ui", flush=True)
    print("[run_dev] 注意：用这个地址，别从 IDE 里直接点开 index.html"
          "（那是静态预览，没有 /api）", flush=True)
    webbrowser.open(base + "/ui")


# ---------------------------------------------------------------- 后台常驻

def detach(args: argparse.Namespace) -> int:
    """把自己再拉一份、脱离当前控制台常驻，然后立刻返回。

    父进程只负责「拉起 + 报结果」，不长期持有子进程 —— 终端关掉不会波及它。
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = log_path(args.port)
    # 端口在入口已确认是空的，此刻残留的 pidfile 必然是上次没清掉的，先删。
    try:
        pid_path(args.port).unlink()
    except OSError:
        pass
    child_args = [sys.executable, str(Path(__file__).resolve()),
                  "--supervise", "--port", str(args.port), "--host", args.host,
                  "--env-file", args.env_file]
    if args.reload:
        child_args.append("--reload")
    if args.no_restart:
        child_args.append("--no-restart")

    with open(log, "ab") as fh:
        fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n".encode("utf-8"))
        fh.flush()
        proc = subprocess.Popen(
            child_args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=fh, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=_WIN_DETACHED if os.name == "nt" else 0,
            start_new_session=(os.name != "nt"),
        )

    # 以 pidfile 为准而不是 Popen 的返回值：解释器/启动器可能多一跳，
    # 两者会差一个 pid。pidfile 里那个才是 --stop 真正会去杀的进程。
    pid = proc.pid
    for _ in range(50):
        recorded = read_pid(args.port)
        if recorded and recorded != os.getpid() and pid_alive(recorded):
            pid = recorded
            break
        time.sleep(0.1)

    base = base_url(args.host, args.port)
    if wait_health(base, args.wait):
        print(f"[run_dev] 已在后台常驻 pid={pid}  页面={base}/ui")
        print(f"[run_dev] 日志：{log}")
        print("[run_dev] 关掉本终端不影响它；停止用 --stop，查状态用 --status")
        return 0
    print(f"[run_dev] 已拉起 pid={pid}，但 {args.wait:.0f}s 内 /health 未通。")
    print(f"[run_dev] 看日志：{log}")
    return 1


# ---------------------------------------------------------------- 状态与停止

def status(args: argparse.Namespace) -> int:
    base = base_url(args.host, args.port)
    pid = read_pid(args.port)
    alive = bool(pid and pid_alive(pid))
    healthy = health_ok(base)

    print(f"[run_dev] 端口 {args.port}：{'有监听' if port_busy(args.port, args.host) else '无监听'}")
    if pid and not alive:
        # 进程被强杀/断电时 finally 来不及跑，pid 文件会留下来。留着它
        # 只会让下次 --stop 说「没在跑」，不如这里直接说清并清掉。
        try:
            pid_path(args.port).unlink()
        except OSError:
            pass
    print(f"[run_dev] 进程：{('pid=' + str(pid)) if pid else '无 pid 记录'}"
          f"{'（存活）' if alive else '（已退出，pid 文件已清理）' if pid else ''}")
    print(f"[run_dev] 后端健康：{'OK' if healthy else '未通过 /health' }")
    if healthy:
        print(f"[run_dev] 页面：{base}/ui")
        return 0
    if port_busy(args.port, args.host):
        print("[run_dev] 端口被别的进程占着（不是本脚本起的）——"
              "典型是 IDE 的静态预览，它没有 /api/*")
    return 1


def stop(args: argparse.Namespace) -> int:
    pid = read_pid(args.port)
    if not pid or not pid_alive(pid):
        if pid:
            try:
                pid_path(args.port).unlink()
            except OSError:
                pass
        if port_busy(args.port, args.host):
            print(f"[run_dev] 端口 {args.port} 仍有监听，但没有本脚本的 pid 记录；"
                  "不敢乱杀，请自行确认进程")
            return 1
        print("[run_dev] 没在跑，无需停止" + ("（已清理过期 pid 文件）" if pid else ""))
        return 0
    kill_tree(pid)
    for _ in range(40):
        if not pid_alive(pid):
            break
        time.sleep(0.25)
    try:
        pid_path(args.port).unlink()
    except OSError:
        pass
    print(f"[run_dev] 已停止 pid={pid}")
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    parser = argparse.ArgumentParser(
        description="起 ai_butler 后端：前台/后台/查状态/停止")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AIBUTLER_PORT", DEFAULT_PORT)))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--env-file", default=".env.dev", help="四环境文件之一，默认 .env.dev")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="改代码自动重载（仅前台建议）")
    parser.add_argument("--no-restart", action="store_true", help="退出后不自动重启")
    parser.add_argument("--wait", type=float, default=60.0, help="等 /health 的秒数")
    parser.add_argument("--detach", action="store_true", help="后台常驻（关终端也不死）")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--status", action="store_true", help="查看是否在跑/是否健康")
    parser.add_argument("--stop", action="store_true", help="停止后台服务")
    args = parser.parse_args()

    if args.status:
        return status(args)
    if args.stop:
        return stop(args)

    if args.supervise:
        return supervise(args, open_browser=False)

    env_file = ROOT / args.env_file
    if not env_file.is_file():
        print(f"[run_dev] 找不到环境文件：{env_file}")
        return 2

    if port_busy(args.port, args.host):
        base = base_url(args.host, args.port)
        if health_ok(base):
            print(f"[run_dev] {base} 上已经有一个健康的后端在跑，直接用：{base}/ui")
            return 0
        print(f"[run_dev] 端口 {args.port} 被占用，但 /health 不通 —— "
              f"占它的很可能是 IDE 的静态预览。换一个：--port {args.port + 1}")
        return 2

    if args.detach:
        return detach(args)

    base = base_url(args.host, args.port)
    print(f"[run_dev] 环境={env_file.name}  地址={base}  页面={base}/ui")
    print("[run_dev] 前台运行，Ctrl+C 停止；要关掉终端还活着就用 --detach")
    return supervise(args, open_browser=True)


if __name__ == "__main__":
    raise SystemExit(main())
