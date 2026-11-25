# -*- coding: utf-8 -*-
"""微信半自动桥：窗口定位、输入框粘贴、剪贴板消息批次去重。

边界（重要）：
- 不读取微信进程内存，不提取/解密聊天数据库，不做后台实时监听。
- 只处理用户主动复制到剪贴板的内容；粘贴只写入输入框，绝不自动发送。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

# 微信 3.x 与 4.x 的窗口类名不同，这里按标题 + 类名前缀双重匹配。
_TITLES = {"微信", "weixin", "wechat"}
_CLASS_PREFIXES = ("WeChatMainWndForPC", "Qt5", "Qt6")
_MIN_W, _MIN_H = 700, 450


# ------------------------------------------------------------------ 窗口定位
def _enum_windows() -> list[dict]:
    import win32gui

    found: list[dict] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindow(hwnd):
                return
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            cls = (win32gui.GetClassName(hwnd) or "").strip()
            rect = win32gui.GetWindowRect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w < _MIN_W or h < _MIN_H:
                return
            found.append({
                "hwnd": hwnd,
                "title": title,
                "cls": cls,
                "rect": rect,
                "w": w,
                "h": h,
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
            })
        except Exception:
            return

    win32gui.EnumWindows(_cb, None)
    return found


def _title_match(title: str) -> bool:
    low = (title or "").strip().lower()
    return low in _TITLES or low.startswith("微信 -") or low.startswith("wechat -")


def _is_wechat(rec: dict) -> bool:
    cls = rec.get("cls") or ""
    if not _title_match(rec.get("title") or ""):
        return False
    return any(cls.startswith(p) for p in _CLASS_PREFIXES)


def is_wechat_window(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        import win32gui
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        cls = (win32gui.GetClassName(hwnd) or "").strip()
        return _title_match(title) and any(cls.startswith(p) for p in _CLASS_PREFIXES)
    except Exception:
        return False


def find_wechat_window(prefer: int = 0) -> int:
    """返回微信主窗口句柄；优先用调用方刚捕获到的前台窗口。"""
    if prefer and is_wechat_window(prefer):
        return int(prefer)
    candidates = [r for r in _enum_windows() if _is_wechat(r)]
    if not candidates:
        return 0
    # 可见窗口优先，其次面积大的（主窗口而不是浮层）。
    candidates.sort(key=lambda r: (r["visible"], r["w"] * r["h"]), reverse=True)
    return int(candidates[0]["hwnd"])


def window_rect(hwnd: int):
    import win32gui
    return win32gui.GetWindowRect(hwnd)


def _force_foreground(hwnd: int) -> None:
    """SetForegroundWindow 在后台进程里常被系统拦，附加输入线程再抢一次。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetFocus.argtypes = [wintypes.HWND]

    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return
    fg_tid = wintypes.DWORD(0)
    tgt_tid = wintypes.DWORD(0)
    if fg:
        user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_tid))
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tgt_tid))
    attached = False
    try:
        if fg_tid.value and fg_tid.value != tgt_tid.value:
            attached = bool(user32.AttachThreadInput(fg_tid.value, tgt_tid.value, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_tid.value, tgt_tid.value, False)


def activate(hwnd: int) -> bool:
    try:
        import win32con
        import win32gui
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        _force_foreground(hwnd)
        time.sleep(0.25)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


def focus_input(hwnd: int) -> bool:
    """点一下聊天窗口底部的输入区。微信没有公开控件句柄，只能用相对坐标。"""
    try:
        import win32api
        import win32con
        rect = window_rect(hwnd)
        left, top, right, bottom = rect
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return False
        x = left + int(w * 0.55)
        y = bottom - max(78, int(h * 0.095))
        old = win32api.GetCursorPos()
        win32api.SetCursorPos((x, y))
        time.sleep(0.03)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.08)
        try:
            win32api.SetCursorPos(old)
        except Exception:
            pass
        return True
    except Exception:
        return False


def paste_text(text: str, hwnd: int = 0, replace: bool = True, dry_run: bool = False):
    """把话术粘贴进微信输入框，只粘贴不回车。返回 (ok, 说明)。"""
    text = (text or "").strip()
    if not text:
        return False, "没有可粘贴的话术"
    hwnd = find_wechat_window(hwnd)
    if not hwnd:
        return False, "没找到微信窗口，先手动打开微信主界面"
    if dry_run:
        return True, f"dry-run：找到微信窗口 {hwnd}，未写入"

    try:
        import keyboard
        import pyperclip
        pyperclip.copy(text)
        if not activate(hwnd):
            return False, "微信窗口没能切到前台，已复制到剪贴板，手动 Ctrl+V"
        if not focus_input(hwnd):
            return False, "没定位到微信输入框，已复制到剪贴板，手动 Ctrl+V"
        time.sleep(0.12)
        if replace:
            keyboard.send("ctrl+a")
            time.sleep(0.05)
        keyboard.send("ctrl+v")
        time.sleep(0.12)
        return True, "已填入微信输入框（只粘贴，没有发送）"
    except Exception as e:  # noqa: BLE001
        return False, f"粘贴失败：{e}；已复制到剪贴板，手动 Ctrl+V"


# ------------------------------------------------------------------ 消息去重
class MessageCache:
    """按对象记录已处理过的消息批次，避免同一段记录反复入库/反复判定。"""

    def __init__(self, path: Path, max_batches: int = 40, max_msgs: int = 800):
        self.path = Path(path)
        self.max_batches = max_batches
        self.max_msgs = max_msgs
        self.data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    @staticmethod
    def _batch_key(messages: list[dict]) -> str:
        body = "\n".join(
            f"{m.get('ts', '')}|{m.get('speaker', '')}|{m.get('text', '')}"
            for m in messages or []
        )
        return hashlib.md5(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _msg_key(message: dict) -> str:
        body = f"{message.get('ts', '')}|{message.get('speaker', '')}|{message.get('text', '')}"
        return hashlib.md5(body.encode("utf-8")).hexdigest()

    def _bucket(self, name: str) -> dict:
        bucket = self.data.setdefault(name, {})
        bucket.setdefault("batches", [])
        bucket.setdefault("msgs", [])
        return bucket

    def is_duplicate(self, name: str, messages: list[dict]) -> bool:
        if not name or not messages:
            return False
        bucket = self._bucket(name)
        return self._batch_key(messages) in bucket["batches"]

    def mark(self, name: str, messages: list[dict]) -> None:
        if not name or not messages:
            return
        bucket = self._bucket(name)
        key = self._batch_key(messages)
        if key not in bucket["batches"]:
            bucket["batches"].append(key)
        for m in messages:
            k = self._msg_key(m)
            if k not in bucket["msgs"]:
                bucket["msgs"].append(k)
        bucket["batches"] = bucket["batches"][-self.max_batches:]
        bucket["msgs"] = bucket["msgs"][-self.max_msgs:]
        bucket["updated"] = time.strftime("%Y-%m-%d %H:%M")
        self._save()

    def count(self, name: str) -> int:
        return len(self._bucket(name)["msgs"]) if name else 0

    def stats(self) -> dict:
        return {
            name: {"batches": len(v.get("batches") or []), "msgs": len(v.get("msgs") or [])}
            for name, v in self.data.items()
        }
