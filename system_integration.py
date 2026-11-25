# -*- coding: utf-8 -*-
"""系统集成：开机自启（HKCU Run 项）与托盘图标。

所有改动都只落在当前用户范围：注册表 HKCU\\...\\Run 一个值，卸载时删掉即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "GoutouJunshiPC"


# ------------------------------------------------------------------ 开机自启
def _pythonw() -> str:
    exe = sys.executable or "pythonw.exe"
    if exe.lower().endswith("python.exe"):
        cand = Path(exe).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return exe


def startup_command(main_py: Path) -> str:
    if getattr(sys, "frozen", False):
        # 打包后 sys.executable 就是本软件 exe，不能再写成 pythonw main.py
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{_pythonw()}" "{Path(main_py).resolve()}"'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return bool(value)
    except Exception:
        return False


def set_autostart(enabled: bool, main_py: Path) -> tuple[bool, str]:
    """返回 (是否成功, 说明)。"""
    try:
        import winreg
        if enabled:
            cmd = startup_command(main_py)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
            return True, f"已设为开机自启：{cmd}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        return True, "已取消开机自启"
    except FileNotFoundError:
        return True, "已取消开机自启"
    except Exception as e:  # noqa: BLE001
        return False, f"改注册表失败：{e}"


# ------------------------------------------------------------------ 托盘
def _icon_image(size: int = 64, path: Path | None = None):
    if path:
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            return im.resize((size, size), Image.LANCZOS)
        except Exception:
            pass
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 底板：圆角方块 + 上深下浅
    top, bottom = (26, 96, 220), (56, 140, 255)
    for y in range(size):
        t = y / max(1, size - 1)
        d.line([(0, y), (size, y)],
               fill=(int(top[0] + (bottom[0] - top[0]) * t),
                     int(top[1] + (bottom[1] - top[1]) * t),
                     int(top[2] + (bottom[2] - top[2]) * t), 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=14, fill=255)
    img.putalpha(mask)
    # 对话气泡
    d.rounded_rectangle([12, 16, size - 12, size - 20], radius=10, fill=(255, 255, 255, 255))
    d.polygon([(24, size - 20), (26, size - 8), (36, size - 20)], fill=(255, 255, 255, 255))
    for x in (24, 32, 40):
        d.ellipse([x - 3, size // 2 - 6, x + 3, size // 2], fill=(40, 110, 230, 255))
    return img


class Tray:
    """包装 pystray：不可用时静默退化，不影响主程序。"""

    def __init__(self, app, title: str):
        self.app = app
        self.title = title
        self.icon = None
        self.ok = False

    def start(self) -> bool:
        try:
            import pystray
            from pystray import Menu, MenuItem
        except Exception:
            return False
        try:
            menu = Menu(
                MenuItem("打开主窗口", lambda *a: self.app.call(self.app.show), default=True),
                MenuItem("读剪贴板并生成", lambda *a: self.app.call(self.app.quick_generate)),
                MenuItem("生成回复（手动输入）", lambda *a: self.app.call(self.app.show)),
                MenuItem("设置…", lambda *a: self.app.call(self.app.open_settings)),
                MenuItem("话术库", lambda *a: self.app.call(self.app.open_library)),
                Menu.SEPARATOR,
                MenuItem("退出", lambda *a: self.app.call(self.app.quit_app)),
            )
            logo = Path(getattr(self.app, "app_dir", ".")) / "assets" / "logo_256.png"
            self.icon = pystray.Icon("goutou", _icon_image(64, logo), self.title, menu)
            self.icon.run_detached()
            self.ok = True
            return True
        except Exception:
            self.icon = None
            self.ok = False
            return False

    def notify(self, text: str) -> None:
        if not self.ok or self.icon is None:
            return
        try:
            self.icon.notify(text, self.title)
        except Exception:
            pass

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
            self.ok = False
