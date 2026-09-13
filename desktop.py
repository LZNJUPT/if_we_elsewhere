# -*- coding: utf-8 -*-
"""
IfWe · 桌面启动器（Windows 开箱即用）

  python desktop.py            # 本地服务 + 原生窗口（pywebview；缺依赖则回退默认浏览器）
  python desktop.py --browser  # 直接用默认浏览器打开（不装 pywebview 也能用）
  python desktop.py --port 8088

行为约定：
  - 只监听 127.0.0.1；关窗即优雅停服务（进程树完全退出，不留孤儿）
  - 首选端口被占用时自动向上探测换口
  - 单实例锁（data/.ifwe.lock）：重复双击不会起第二个服务，
    而是把已运行实例的地址用浏览器打开；锁文件里的死进程（异常退出留下）会被清理
  - 打包后（PyInstaller）数据目录仍是 exe 同级目录下的 data/，绝不写进安装目录之外
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _root() -> Path:
    """项目根：源码运行 = 本文件所在目录；打包后 = exe 所在目录"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _root()
for _p in (str(ROOT), str(ROOT / "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LOCK_NAME = ".ifwe.lock"
PORT_TRIES = 24
_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW


# ---------------------------------------------------------------- 端口 / 健康检查
def find_free_port(preferred: int) -> int:
    for port in range(preferred, preferred + PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"[desktop] 连续 {PORT_TRIES} 个端口都被占用，请用 --port 指定一个空闲端口")


def wait_ready(port: int, timeout: float = 25.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


# ---------------------------------------------------------------- 单实例锁 / 孤儿清理
def lock_path() -> Path:
    try:
        import config as cfg_mod
        base = cfg_mod.base_data_dir()
    except Exception:
        base = ROOT / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=8, **_NO_WINDOW)
            return str(pid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_our_process(pid: int) -> bool:
    """只清理「看起来是我们自己」的进程（python / IfWe），避免误杀复用了 PID 的无关程序"""
    if os.name != "nt":
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=8, **_NO_WINDOW)
        txt = (out.stdout or "").lower()
    except Exception:
        return False
    return ("python" in txt) or ("ifwe" in txt)


def _kill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=10, **_NO_WINDOW)
    except Exception:
        pass


def read_lock() -> dict:
    try:
        parts = lock_path().read_text(encoding="utf-8").strip().split()
        return {"pid": int(parts[0]), "port": int(parts[1]) if len(parts) > 1 else 0}
    except Exception:
        return {}


def acquire_single_instance() -> dict:
    """返回 {"ok": bool, "port": int}；ok=False 表示已有实例在跑（port 为它的端口）"""
    p = lock_path()
    if p.exists():
        old = read_lock()
        pid_old = old.get("pid", 0)
        if pid_old and pid_old != os.getpid() and _pid_alive(pid_old) and _is_our_process(pid_old):
            return {"ok": False, "port": old.get("port", 0)}
        if pid_old and pid_old != os.getpid() and _pid_alive(pid_old):
            _kill(pid_old)                      # 异常退出留下的孤儿
        try:
            p.unlink()
        except OSError:
            pass
    return {"ok": True, "port": 0}


def write_lock(port: int) -> None:
    try:
        lock_path().write_text(f"{os.getpid()} {port} {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                               encoding="utf-8")
    except OSError as e:
        print(f"[desktop] 写入实例锁失败（不影响运行）：{e}")


def release_lock() -> None:
    try:
        p = lock_path()
        if read_lock().get("pid") == os.getpid():
            p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 服务 / 窗口
def start_server(port: int):
    import uvicorn
    os.environ["PORT"] = str(port)          # phase15_api.PORT 与实际监听端口保持一致
    from phase15_api import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    th = threading.Thread(target=server.run, name="ifwe-uvicorn", daemon=True)
    th.start()
    return server, th


def open_browser(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as e:
        print(f"[desktop] 打开浏览器失败（请手动访问 {url}）：{e}")


def launch_window(url: str) -> bool:
    """pywebview 原生窗口；缺依赖或 WebView2 缺失时回退默认浏览器"""
    try:
        import webview
    except Exception as e:
        print(f"[desktop] 未安装 pywebview（{str(e)[:80]}），改用默认浏览器；"
              f"想要原生窗口请执行: pip install pywebview")
        open_browser(url)
        return False
    try:
        webview.create_window("IfWe · 若我们在", url,
                              width=1180, height=840, min_size=(960, 620))
        webview.start()                     # 阻塞直到用户关窗
        return True
    except Exception as e:
        print(f"[desktop] 窗口启动失败（{str(e)[:120]}），改用默认浏览器")
        open_browser(url)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(prog="desktop.py", description="IfWe 桌面启动器")
    ap.add_argument("--port", type=int, default=0, help="端口（默认取 config defaults.port）")
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开，不建原生窗口")
    args = ap.parse_args()

    inst = acquire_single_instance()
    if not inst["ok"]:
        url = f"http://127.0.0.1:{inst['port']}" if inst["port"] else ""
        print("[desktop] IfWe 已经有一个窗口在运行" + (f"：{url}" if url else ""))
        if url:
            open_browser(url)
        return 1

    import config as cfg_mod
    preferred = args.port or int(cfg_mod.load()["defaults"]["port"])
    port = find_free_port(preferred)
    if port != preferred:
        print(f"[desktop] 端口 {preferred} 被占用，已改用 {port}")

    write_lock(port)
    server, th = start_server(port)
    try:
        if not wait_ready(port):
            print("[desktop] 本地服务启动失败（25 秒内未就绪），已退出")
            return 2
        url = f"http://127.0.0.1:{port}"
        print(f"[desktop] IfWe 已启动：{url}")
        print("[desktop] 数据目录：", cfg_mod.data_dir())
        print("[desktop] 关闭窗口（或 Ctrl+C）即退出；数据全部留在本机。")
        if args.browser:
            open_browser(url)
            try:
                while th.is_alive():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        else:
            if not launch_window(url):
                try:
                    while th.is_alive():
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    pass
    finally:
        server.should_exit = True
        for _ in range(30):                 # 等 uvicorn 收尾，避免留下孤儿进程
            if not th.is_alive():
                break
            time.sleep(0.1)
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
