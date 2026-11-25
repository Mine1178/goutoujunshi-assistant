# -*- coding: utf-8 -*-
"""入口：Ctrl+Alt+M 唤起，读剪贴板 -> 生成可直接发的话术。"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
from version import APP_NAME, VERSION  # noqa: E402

WINDOW_TITLE = f"{APP_NAME} v{VERSION}"
_MUTEX = None


def setup_logging() -> None:
    log_dir = APP_DIR / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def ensure_single_instance() -> None:
    """同一时间只允许一个实例；再次启动时把已有窗口叫到前面。

    否则会出现两个窗口抢同一个全局热键、写同一份 data/ 的情况。
    """
    global _MUTEX
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        _MUTEX = kernel32.CreateMutexW(None, False, "Local\\GoutouJunshiPC_single")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            user32 = ctypes.WinDLL("user32")
            user32.FindWindowW.restype = wintypes.HWND
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                user32.ShowWindow(hwnd, 9)      # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            sys.exit(0)
    except Exception:
        pass  # 拿不到互斥量就照常启动，不阻塞使用


def main() -> int:
    setup_logging()
    ensure_single_instance()
    try:
        from ui import App
        app = App(APP_DIR)
        logging.info("启动完成，技能目录=%s", app.engine.bundle.root)
        app.run()
        return 0
    except Exception:
        logging.exception("启动失败")
        try:
            import tkinter.messagebox as mb
            mb.showerror("启动失败", "详情见 data\\app.log")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
