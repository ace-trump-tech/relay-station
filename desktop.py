"""Windows desktop entry point; all persistent state lives outside the bundle."""
import argparse
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import __version__

TITLE = "Relay · 信息中转站"
RESOURCES = Path(__file__).resolve().parent


def user_data_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "RelayStation"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "relay-station"


class InstanceLock:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / "desktop.lock").open("a+b")
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("Relay 已在运行。请从任务栏或系统托盘打开现有窗口。") from None

    def close(self):
        self.stream.close()


def inform(text):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x40)
    elif sys.stderr:
        print(text, file=sys.stderr)


def foreground_existing():
    if os.name == "nt":
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)


class DesktopBridge:
    """Only fixed local actions. No arbitrary paths, URLs, commands or secrets."""
    def __init__(self, runtime):
        self._runtime = runtime

    def open_in_browser(self):
        webbrowser.open(self._runtime.origin + "/#connections")
        return True

    def open_data_folder(self):
        if os.name == "nt":
            os.startfile(str(self._runtime.directory))
        return True

    def notification_status(self):
        return {"available": bool(self._runtime.tray), "version": __version__}


class Runtime:
    def __init__(self, directory, port=8765):
        self.directory = Path(directory).resolve()
        self.origin = f"http://localhost:{port}"
        self.lock = InstanceLock(self.directory)
        self.stop_event = threading.Event()
        self.window = None
        self.tray = None
        self.server = None
        self.thread = None
        self.listener = None
        self.notice_ids = set()
        try:
            # Reserve the actual socket, preventing a port-selection race or accidental
            # opening of another local service when the default port is occupied.
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name == "nt":
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.listener.bind(("127.0.0.1", port))
            self.listener.listen(128)
            from app.main import create_app
            import uvicorn
            self.app = create_app(data_dir=self.directory, origin=self.origin)
            config = uvicorn.Config(self.app, host="127.0.0.1", port=port,
                log_config=None, access_log=False, log_level="critical", loop="asyncio", http="h11", ws="none")
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True)
            self.thread.start()
            deadline = time.monotonic() + 30
            while not self.server.started:
                if not self.thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("本地服务启动失败，请检查数据目录权限。")
                time.sleep(0.05)
        except Exception:
            self.shutdown()
            raise

    def shutdown(self):
        self.stop_event.set()
        if self.tray:
            self.tray.stop()
        if self.server:
            self.server.should_exit = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
        if self.listener:
            self.listener.close()
        self.lock.close()

    def show(self, *_):
        if self.window:
            self.window.show()
            self.window.restore()

    def quit(self, *_):
        self.stop_event.set()
        if self.window:
            self.window.destroy()
        if self.tray:
            self.tray.stop()

    def start_tray(self):
        try:
            import pystray
            from PIL import Image
            menu = pystray.Menu(
                pystray.MenuItem("打开 Relay", self.show, default=True),
                pystray.MenuItem("在浏览器中打开", lambda *_: webbrowser.open(self.origin)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出并停止提醒", self.quit),
            )
            self.tray = pystray.Icon("RelayStation", Image.open(RESOURCES / "assets" / "relay.ico"), TITLE, menu)
            self.tray.run_detached()
            threading.Thread(target=self.native_notifications, daemon=True).start()
        except Exception:
            self.tray = None

    def native_notifications(self):
        from app.engine import DEFAULTS, quiet_now
        while not self.stop_event.wait(5):
            try:
                settings = {**DEFAULTS, **self.app.state.store.get("settings", {})}
                current = datetime.now(ZoneInfo(os.getenv("RELAY_TIMEZONE", "Asia/Shanghai")))
                if not settings.get("desktop_notifications") or quiet_now(settings, current):
                    continue
                with self.app.state.store.connect() as db:
                    rows = db.execute("SELECT id,payload FROM reminders WHERE acknowledged=0").fetchall()
                for row in rows:
                    value = self.app.state.store.decrypt(row["payload"])
                    if row["id"] not in self.notice_ids and datetime.fromisoformat(value["due"]) <= current:
                        self.tray.notify(value["title"], TITLE)
                        self.notice_ids.add(row["id"])
            except Exception:
                # A notification failure must not stop email sync or leak message text.
                continue

    def smoke(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path):
            with opener.open(self.origin + path, timeout=10) as response:
                return response.read()
        result = {"health": json.loads(get("/health")), "session": json.loads(get("/api/session")),
                  "frontend": "Relay" in get("/").decode(),
                  "script": len(get("/static/app.js")) > 1000,
                  "organizer_script": "memoProfile" in get("/static/organizer.js").decode(),
                  "persistent_data": str(self.directory), "frozen": bool(getattr(sys, "frozen", False))}
        result["ok"] = all((result["health"]["ok"], result["frontend"], result["script"], result["organizer_script"]))
        return result

    def webview(self, gui_test=False):
        import webview
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        self.window = webview.create_window(TITLE, self.origin, width=1380, height=920,
            min_size=(960, 680), background_color="#f6f7f9", js_api=DesktopBridge(self))
        self.start_tray()

        def closing():
            if self.tray and not self.stop_event.is_set():
                self.window.hide()
                return False
            return True
        self.window.events.closing += closing
        result = {}

        def probe():
            try:
                if not self.window.events.loaded.wait(30):
                    raise RuntimeError("Desktop page did not load")
                deadline = time.monotonic()+25
                while time.monotonic() < deadline:
                    result.update(self.window.evaluate_js("({title:document.title, cards:document.querySelectorAll('.stat-card').length, bridge:!!window.pywebview?.api, heading:document.querySelector('h1')?.textContent})") or {})
                    if result.get("cards") == 4 and result.get("bridge"):
                        break
                    time.sleep(0.2)
                result.update(self.smoke())
                result["renderer"] = "edgechromium"
                result["gui_ok"] = result.get("cards") == 4 and result.get("bridge")
                def wait_js(script):
                    until = time.monotonic()+15
                    while time.monotonic() < until:
                        if self.window.evaluate_js(script):
                            return
                        time.sleep(0.1)
                    raise RuntimeError("Desktop interaction timed out")
                wait_js("document.querySelector('#mode-banner')?.textContent.includes('演示工作区')")
                self.window.evaluate_js("document.querySelector('#modal [data-act=unlock]')?.click(); document.querySelector('#modal[open]') || document.querySelector('#mode-banner [data-act=unlock]').click(); true")
                wait_js("!!document.querySelector('#login-form')")
                self.window.evaluate_js("document.querySelector('#login-form [name=password]').value='relay-desktop-smoke-passphrase'; document.querySelector('#login-form').requestSubmit(); true")
                wait_js("document.querySelector('#workspace-mode')?.textContent.includes('已解锁')")
                result["initialized"] = bool(self.app.state.store.get("password"))
                self.window.evaluate_js("location.hash='settings'; true")
                wait_js("!!document.querySelector('#main [data-act=services]')")
                self.window.evaluate_js("document.querySelector('#main [data-act=services]').click(); true")
                wait_js("!!document.querySelector('#services-form')")
                self.window.evaluate_js("document.querySelector('#services-form [name=google_client_id]').value='desktop-smoke-client'; document.querySelector('#services-form [name=google_client_secret]').value='synthetic-test-secret'; document.querySelector('#services-form').requestSubmit(); true")
                wait_js("!document.querySelector('#modal[open]')")
                result["service_saved"] = self.app.state.store.get("services", {}).get("google_client_id") == "desktop-smoke-client"
                self.window.evaluate_js("location.hash='items'; true")
                wait_js("!!document.querySelector('#main [data-act=memo-new]')")
                self.window.evaluate_js("document.querySelector('#main [data-act=memo-new]').click(); true")
                wait_js("!!document.querySelector('#memo-item-form')")
                self.window.evaluate_js("document.querySelector('#memo-item-form [name=title]').value='Desktop memo smoke'; document.querySelector('#memo-item-form [name=importance_override]').value='critical'; document.querySelector('#memo-item-form').requestSubmit(); true")
                wait_js("document.querySelector('#main .memo-title')?.textContent.includes('Desktop memo smoke')")
                result["memo_saved"] = any(i["title"]=="Desktop memo smoke" and i["importance_override"]=="critical" for i in self.app.state.store.items())
                result["tray"] = bool(self.tray)
                if self.tray:
                    # Fire the same closing event as the window's X button. It must
                    # cancel destruction, hide the window, and keep the service alive.
                    result["close_to_tray"] = self.window.events.closing.set()
                    result["background_alive"] = self.smoke()["ok"]
                    self.window.show()
                result["gui_ok"] = all((result["gui_ok"], result["initialized"], result["service_saved"], result["memo_saved"], result["tray"], result.get("close_to_tray"), result.get("background_alive")))
            except Exception as error:
                result.update(ok=False, gui_ok=False, error=type(error).__name__)
            finally:
                self.stop_event.set()
                self.window.destroy()

        webview.start(probe if gui_test else None, gui="edgechromium", debug=False,
            private_mode=True, storage_path=str(self.directory / "webview"))
        return result

    def browser_fallback(self):
        import tkinter as tk
        root = tk.Tk()
        root.title(TITLE)
        root.geometry("500x335")
        root.configure(bg="#f5f8f2")
        root.resizable(False, False)
        icon_path = RESOURCES / "assets" / "relay.ico"
        if icon_path.exists():
            root.iconbitmap(str(icon_path))
        tk.Label(root, text="Relay", bg="#f5f8f2", fg="#245b4b", font=("Segoe UI", 30, "bold")).pack(pady=(28, 10))
        tk.Label(root, text="本地服务已启动 · 浏览器模式", bg="#f5f8f2", fg="#516950", font=("Microsoft YaHei", 12)).pack()
        tk.Label(root, text="此设备未能启动内嵌窗口，已使用默认浏览器。\n保持此窗口打开，即可继续同步和提醒。", bg="#f5f8f2", fg="#8b9883", font=("Microsoft YaHei", 10), justify="center").pack(pady=18)
        tk.Button(root, text="打开工作台", command=lambda: webbrowser.open(self.origin), bg="#245b4b", fg="white", relief="flat", padx=28, pady=8).pack()
        tk.Button(root, text="退出并停止提醒", command=root.destroy, bg="#f5f8f2", fg="#87977f", relief="flat").pack(pady=12)
        webbrowser.open(self.origin)
        root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Relay desktop")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--gui-smoke", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    testing = args.smoke_test or args.gui_smoke
    if testing and (not args.data_dir or not args.report):
        parser.error("Smoke tests require an explicit --data-dir and --report")
    logging.basicConfig(handlers=[logging.NullHandler()], force=True)
    runtime, result = None, {"ok": False}
    try:
        runtime = Runtime(args.data_dir or user_data_dir(), args.port)
        if args.smoke_test:
            result = runtime.smoke()
        else:
            try:
                result = runtime.webview(args.gui_smoke)
            except Exception:
                if testing:
                    raise
                runtime.browser_fallback()
        return 0 if not testing or (result.get("ok") and result.get("gui_ok", True)) else 1
    except Exception as error:
        result = {"ok": False, "error": type(error).__name__}
        if not testing:
            if isinstance(error, OSError):
                inform("Relay 无法启动。端口 8765 可能已被使用，或数据目录不可写。请关闭原有 Relay 后重试。")
            elif isinstance(error, RuntimeError):
                foreground_existing()
                inform(str(error))
            else:
                inform("Relay 启动未完成。请确认文件完整，或尝试重新下载。")
        return 1
    finally:
        if runtime:
            runtime.shutdown()
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
