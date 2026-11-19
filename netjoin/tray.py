"""Notification-area (system tray) icon: left-click opens NetJoin, right-click for the menu.

Windows only. Uses pystray + Pillow. The web server runs in a background thread;
the tray icon owns the main thread until Quit.
"""

from __future__ import annotations

import ctypes
import os
import threading
import webbrowser
from typing import Callable

from . import APP_NAME, __version__, paths

MB_YESNOCANCEL, MB_OK = 0x03, 0x00
MB_ICONQUESTION, MB_ICONERROR = 0x20, 0x10
MB_TOPMOST, MB_SETFOREGROUND = 0x40000, 0x10000
IDYES, IDNO, IDCANCEL = 6, 7, 2


def message_box(text: str, title: str = APP_NAME, flags: int = MB_OK) -> int:
    try:
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags | MB_TOPMOST | MB_SETFOREGROUND)
    except Exception:
        return IDNO


def available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def run(url: str, engine, stop_server: Callable[[], None]) -> None:
    """Show the tray icon and block until the user quits."""
    import pystray
    from PIL import Image

    image = Image.open(paths.static_dir() / "icon-256.png")

    def open_ui(icon=None, item=None):
        webbrowser.open(url)

    def open_data(icon=None, item=None):
        try:
            os.startfile(str(paths.data_dir()))
        except Exception:
            pass

    quitting = threading.Lock()

    def quit_app(icon, item):
        # Return straight away: the dialog and shutdown run in their own thread, so the tray
        # icon's message handling is never blocked while the question is on screen.
        if quitting.acquire(blocking=False):
            threading.Thread(target=_quit_flow, args=(icon,), name="netjoin-quit", daemon=True).start()

    def _quit_flow(icon):
        try:
            applied = None
            try:
                applied = engine.still_applied()
            except Exception as e:
                engine.log.add(f"Quit: couldn't check adapter state: {e}", "warn")
            if applied:
                answer = message_box(
                    f"NetJoin set {applied['adapter']} to {applied['ip']}/{applied['prefix']} "
                    f"({applied.get('network', 'a site network')}).\n\n"
                    "Switch it back to DHCP before quitting?\n\n"
                    "Yes – back to DHCP and quit\nNo – leave the IP as it is and quit\nCancel – keep NetJoin running",
                    f"Quit {APP_NAME}", MB_YESNOCANCEL | MB_ICONQUESTION)
                engine.log.add(f"Quit: answer {({IDYES: 'Yes', IDNO: 'No', IDCANCEL: 'Cancel'}).get(answer, answer)}")
                if answer == IDCANCEL:
                    quitting.release()
                    return
                if answer == IDYES:
                    try:
                        engine.dhcp(applied["adapter"])
                    except Exception as e:
                        engine.log.add(f"Quit: DHCP failed: {e}", "error")
                        if message_box(f"Couldn't switch back to DHCP:\n{e}\n\nQuit anyway?", f"Quit {APP_NAME}",
                                       0x04 | MB_ICONERROR) != IDYES:   # MB_YESNO
                            quitting.release()
                            return
            engine.log.add(f"{APP_NAME} quitting")
        except Exception as e:
            engine.log.add(f"Quit: unexpected error {e}", "error")
        # Safety net: whatever happens below, the process is gone within a few seconds.
        threading.Timer(6.0, lambda: os._exit(0)).start()
        try:
            stop_server()
        finally:
            icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open NetJoin", open_ui, default=True),   # default = left-click / double-click
        pystray.MenuItem("Open data folder", open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"{APP_NAME} {__version__}  ·  {url}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} {__version__} – click to open", menu)

    def setup(ic):
        ic.visible = True
        try:
            ic.notify("NetJoin is running. Click the icon in the taskbar to open it.", APP_NAME)
        except Exception:
            pass
        threading.Timer(1.2, open_ui).start()

    icon.run(setup=setup)
