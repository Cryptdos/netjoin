"""Launcher: single instance, admin elevation, start the local server, open the browser."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.request
import webbrowser

from . import APP_NAME, DEFAULT_PORT, __version__, paths


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.7) as r:
            return json.loads(r.read()).get("app") == APP_NAME
    except Exception:
        return False


def is_admin() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Ask Windows (UAC) to start us again elevated. True if the request went through."""
    import ctypes
    import subprocess
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, subprocess.list2cmdline(sys.argv[1:])
    else:
        exe, params = sys.executable, subprocess.list2cmdline(["-m", "netjoin", *sys.argv[1:]])
    # Elevated processes start in System32, so give the working folder explicitly.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, str(paths.app_dir()), 1)
    return rc > 32


def main() -> None:
    ap = argparse.ArgumentParser(prog="netjoin", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--fake", action="store_true", help="use the in-memory demo back-end (changes nothing)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-elevate", action="store_true", help="don't ask for admin (Join/DHCP will be disabled)")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--console", action="store_true", help="run in this window instead of the taskbar icon")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}/"

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} {__version__} – close this window to stop")
        except Exception:
            pass

    if already_running(args.port):
        print(f"{APP_NAME} is already running – opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    if not args.fake and not args.no_elevate and not is_admin():
        if relaunch_as_admin():
            return
        print("Admin rights were refused – starting anyway; Join and DHCP will be unavailable.")

    from .engine import Engine
    from .eventlog import EventLog
    from .netcfg import get_backend
    from .scan import ScanManager, get_prober
    from .server import build_app
    from .store import Store
    import uvicorn

    data = paths.data_dir()
    store = Store(data / ("netjoin-demo.db" if args.fake else "netjoin.db"))
    store.backup(data / "backups")
    log = EventLog(data / "netjoin.log")
    backend = get_backend(fake=args.fake)
    engine = Engine(store, backend, log)
    log.add(f"{APP_NAME} {__version__} started ({backend.kind} back-end, data in {data})")

    scans = ScanManager(engine, get_prober(backend.kind))
    app = build_app(engine, args.port, scans)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", log_config=None)
    server = uvicorn.Server(config)

    from . import tray
    use_tray = sys.platform == "win32" and not args.console and tray.available()
    if not use_tray:
        if not args.no_browser:
            threading.Timer(1.2, webbrowser.open, [url]).start()
        print(f"{APP_NAME} {__version__} running at {url}  (close this window to stop)")
        server.run()
        return

    # Taskbar mode: server in the background, tray icon in the foreground until Quit.
    t = threading.Thread(target=server.run, name="netjoin-server", daemon=True)
    t.start()
    for _ in range(50):                 # wait up to 5 s for the port to be ready
        if server.started or not t.is_alive():
            break
        threading.Event().wait(0.1)
    if not t.is_alive():
        raise RuntimeError(f"The local server couldn't start on port {args.port} (is another program using it?)")

    def stop_server():
        server.should_exit = True
        t.join(5)

    tray.run(url, engine, stop_server)


def run() -> None:
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        details = traceback.format_exc()
        log_file = None
        try:
            log_file = paths.data_dir() / "netjoin-error.log"
            log_file.write_text(details, encoding="utf-8")
        except Exception:
            pass
        if sys.stdout is not None:
            print(details)
        if sys.platform == "win32":
            if sys.stdout is None or getattr(sys, "frozen", False):   # no console to read: show a pop-up
                from .tray import MB_ICONERROR, message_box
                last = details.strip().splitlines()[-1]
                message_box(f"NetJoin couldn't start:\n\n{last}\n\nFull details: {log_file}",
                            f"{APP_NAME} error", MB_ICONERROR)
            else:
                input("\nNetJoin hit an error (details above). Press Enter to close...")
        raise SystemExit(1)


if __name__ == "__main__":
    run()
