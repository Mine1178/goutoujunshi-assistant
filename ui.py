# -*- coding: utf-8 -*-
"""狗头军师助手（主界面 + 联系人 + 记忆 + 风格）"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from contacts import Contacts
from llm_client import LLMClient, LLMError
from skill_engine import SkillEngine, split_sections
from store import Store
from system_integration import Tray, autostart_enabled, set_autostart
from version import APP_NAME, VERSION
import wechat_bridge as wb
from wechat_import import parse_wechat, to_annotated, keep_related

FONT = "Microsoft YaHei UI"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)).resolve()
CHAT_DIR = BASE_DIR / "chat"
BG = "#F4F6FA"
CARD = "#FFFFFF"
BORDER = "#E5E9F0"
TEXT = "#111827"
SUB = "#6B7280"
ACCENT = "#2F6BFF"
ACCENT_HOVER = "#4A7DFF"
WARM_BG = "#FFF4E8"
WARM_FG = "#A8560A"
WARM_BORDER = "#F2D2AF"
WARM_HOVER = "#FFE9D2"

PLACEHOLDER = "把聊天记录贴在这里。微信里多选消息 → 右键复制（会自动标 [我]/[对方]），也可以点「导入聊天」贴整段记录。"

DEFAULT_ASK = "帮我回这条，我需要能直接发出去的话"
ASK_HINT = "想达到的效果（可不填，例：约她周末出来 / 体面地拒绝）"

# 字体档位：第 1 档是“小”，第 2 档为“标准”，最后一档为“大”
FONT_SCALES = [0.90, 1.0, 1.03, 1.06, 1.09, 1.12, 1.15, 1.18]


class StepSlider(tk.Canvas):
    """带刻度的档位滑杆：白底、蓝色已选轨道、圆形滑块。"""

    def __init__(self, master, values, index=1, command=None, **kw):
        super().__init__(master, height=54, bg=CARD, highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=1, **kw)
        self.values = list(values)
        self.index = max(0, min(len(self.values) - 1, int(index)))
        self.command = command
        self._dragging = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Left>", lambda e: self._step(-1))
        self.bind("<Right>", lambda e: self._step(1))

    def _positions(self):
        pad = 14
        width = max(80, self.winfo_width())
        x0, x1 = pad, width - pad
        n = len(self.values)
        step = (x1 - x0) / max(1, n - 1)
        return x0, x1, step

    def _draw(self):
        self.delete("all")
        if self.winfo_width() < 40:
            return
        x0, x1, step = self._positions()
        y = 17
        xs = [x0 + step * i for i in range(len(self.values))]
        # 轨道 + 刻度
        self.create_line(x0, y, x1, y, width=6, fill="#D4DAE4", capstyle="round")
        for x in xs:
            self.create_line(x, y - 6, x, y + 6, width=2, fill="#AEB7C5")
        # 已选段
        xa = xs[self.index]
        self.create_line(x0, y, xa, y, width=6, fill=ACCENT, capstyle="round")
        # 滑块：浅阴影 + 白色圆钮
        self.create_oval(xa - 12, y - 11, xa + 12, y + 13, fill="#EEF1F6", outline="")
        self.create_oval(xa - 10, y - 11, xa + 10, y + 9, fill="#FFFFFF",
                         outline="#DCE2EC", width=1)
        # 下方文字：小 / 标准 / 大
        self.create_text(x0, 39, text="小", anchor="w", fill="#8A9099", font=(FONT, 8))
        self.create_text(xs[1], 39, text="标准", anchor="center", fill="#8A9099", font=(FONT, 8))
        self.create_text(x1, 39, text="大", anchor="e", fill="#8A9099", font=(FONT, 8))

    def _set_from_x(self, x, notify=False):
        x0, x1, step = self._positions()
        idx = int(round((max(x0, min(x1, x)) - x0) / max(1, step)))
        idx = max(0, min(len(self.values) - 1, idx))
        if idx != self.index:
            self.index = idx
            self._draw()
        if notify and self.command:
            self.command(self.index)

    def _on_press(self, e):
        self.focus_set()
        self._dragging = True
        self._set_from_x(e.x)

    def _on_drag(self, e):
        if self._dragging:
            self._set_from_x(e.x)

    def _on_release(self, e):
        if self._dragging:
            self._dragging = False
            self._set_from_x(e.x, notify=True)

    def _step(self, delta):
        self.index = max(0, min(len(self.values) - 1, self.index + delta))
        self._draw()
        if self.command:
            self.command(self.index)



def clean_names(names) -> list[str]:
    """昵称清洗：旧版导入 bug 可能把 python dict 字符串塞进 my_names，这里统一挡掉。"""
    out: list[str] = []
    for raw in names or []:
        s = str(raw).strip()
        if not s or len(s) > 24:
            continue
        if any(ch in s for ch in "{}[]:,'\"\n\t") or s.isdigit():
            continue
        if s not in out:
            out.append(s)
    return out


class App:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.store = Store(app_dir)
        self.contacts = Contacts(app_dir / "data")
        cfg = self.store.config
        self.engine = SkillEngine(self._skill_root())
        self.client = LLMClient(cfg["api"])
        self.my_names = clean_names(cfg.get("my_names", []))
        if self.my_names != list(cfg.get("my_names", [])):
            # 清掉历史遗留的脏昵称（旧解析 bug 会写进日期或 dict 字符串）
            fixed = dict(cfg)
            fixed["my_names"] = self.my_names
            self.store.apply_config(fixed)
        self.extra_style = cfg.get("extra_style", "")

        self.target_hwnd = 0
        self.wechat_hwnd = 0
        self.msg_cache = wb.MessageCache(app_dir / "data" / "wechat_cache.json")
        self.current_contact = ""
        self.last_draft = ""
        self.last_text = ""
        self.last_log = ""
        self.last_system = ""
        self.last_files: list[str] = []
        self.thread: list[dict] = []
        self.pending_image_b64 = ""
        self.placeholder_on = True
        self._busy = False
        self._hotkey_handle = None
        self._queue: queue.Queue = queue.Queue()
        self._cat_cache: dict[str, int] = {}
        self._tray_hint_once = False
        self._clip_hash = ""
        self._judge_busy = False
        self._rescue_mode = False
        self.main_py = app_dir / "main.py"
        self._font_scale = 1.0
        self._font_base: dict[tuple[str, str], tuple] = {}

        self._build_ui()
        self._register_hotkey(cfg.get("hotkey", "ctrl+alt+m"))
        self.tray = Tray(self, f"{APP_NAME} v{VERSION}")
        if self.behavior().get("tray", True):
            self.tray.start()
        self.root.after(60, self._pump)
        self._refresh_status()

    def behavior(self) -> dict:
        b = dict(self.store.config.get("behavior") or {})
        b.setdefault("tray", True)
        b.setdefault("close_to_tray", True)
        b.setdefault("autostart", autostart_enabled())
        b.setdefault("auto_copy", True)
        b.setdefault("hotkey_read_clip", True)
        b.setdefault("auto_judge", True)
        b.setdefault("auto_rescue", True)
        b.setdefault("auto_sync_clip", True)
        b.setdefault("one_key_flow", True)
        b.setdefault("use_history_context", True)
        b.setdefault("auto_import_watch", True)
        return b

    def call(self, fn, *a) -> None:
        """托盘在另一个线程里，统一回到主线程执行。"""
        try:
            self.root.after(0, lambda: fn(*a))
        except Exception:
            pass

    # ---------------------------------------------------------------- 基础
    def _skill_root(self) -> Path:
        cfg_path = (self.store.config.get("skill_root") or "").strip()
        if cfg_path:
            p = Path(cfg_path).expanduser()
            if not p.is_absolute():
                p = self.app_dir / p
            if p.exists():
                return p
        for local in (RESOURCE_DIR / "skill" / "goutoujunshi",
                      self.app_dir / "skill" / "goutoujunshi",
                      self.app_dir / "skill"):
            if self.is_valid_skill(local):
                return local
        return self.app_dir / "skill" / "goutoujunshi"

    @staticmethod
    def is_valid_skill(path: Path) -> bool:
        try:
            return path.is_dir() and (path / "SKILL.md").exists()
        except Exception:
            return False

    def skill_candidates(self) -> list[Path]:
        """优先找软件自带的本地技能，再找外置的原生技能副本。"""
        cands: list[Path] = [
            RESOURCE_DIR / "skill" / "goutoujunshi",
            self.app_dir / "skill" / "goutoujunshi",
            self.app_dir / "skill",
        ]
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            cands.append(Path(local) / "Doubao/User Data/Default/.doubao/agent_mode/workspace/.user_skills/goutoujunshi")
        cands.append(Path.home() / "AppData/Local/Doubao/User Data/Default/.doubao/agent_mode/workspace/.user_skills/goutoujunshi")
        return [c for c in cands if c not in (None, "")]

    def detect_skill_root(self) -> Path | None:
        for c in self.skill_candidates():
            if self.is_valid_skill(c):
                return c
        return None

    def reload_engine(self, root: Path) -> tuple[bool, str]:
        if not self.is_valid_skill(root):
            return False, "这个目录里没有 SKILL.md"
        try:
            eng = SkillEngine(root)
        except Exception as e:  # noqa: BLE001
            return False, f"加载失败：{e}"
        if len(eng.bundle.core) < 500:
            return False, "SKILL.md 内容太短，不像是狗头军师技能"
        self.engine = eng
        cfg = dict(self.store.config)
        cfg["skill_root"] = str(root)
        self.store.apply_config(cfg)
        self._refresh_status()
        return True, f"已接入原始技能：{len(eng.bundle.routes)} 条知识路由"

    def _apply_icon(self) -> None:
        """窗口/任务栏图标：与打包、快捷方式共用同一份 icon.ico。"""
        ico = self.app_dir / "assets" / "icon.ico"
        if not ico.exists():
            return
        try:
            self.root.iconbitmap(str(ico))
        except Exception:
            pass

    def set_win_icon(self, win) -> None:
        ico = self.app_dir / "assets" / "icon.ico"
        if not ico.exists():
            return
        try:
            win.iconbitmap(str(ico))
        except Exception:
            pass

    def open_sponsor(self):
        """赞助作者：支付宝 / 微信两列二维码，扫码即可。"""
        from PIL import Image, ImageTk, ImageOps
        dlg = tk.Toplevel(self.root)
        self._sponsor_win = dlg
        self.set_win_icon(dlg)
        dlg.title("赞助作者")
        dlg.configure(bg=BG)
        dlg.geometry("760x650")
        dlg.minsize(700, 600)
        dlg.transient(self.root)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"760x650+{max(10, (sw - 760) // 2)}+{max(10, (sh - 650) // 2)}")

        top = tk.Frame(dlg, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        top.pack(fill="x", padx=18, pady=(18, 0))
        tk.Label(top, text="☕", font=(FONT, 30), fg=WARM_FG, bg=CARD).pack(pady=(14, 0))
        tk.Label(top, text="如果你喜欢这个工具", font=(FONT, 16, "bold"), fg=TEXT, bg=CARD).pack(pady=(2, 0))
        tk.Label(top, text="可以请作者喝杯咖啡，感谢赞助。二维码只用于自愿打赏，不解锁功能。",
                 font=(FONT, 9), fg=SUB, bg=CARD, wraplength=660, justify="center").pack(pady=(5, 14))

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=14)
        body.columnconfigure(0, weight=1, uniform="qr")
        body.columnconfigure(1, weight=1, uniform="qr")
        refs = []
        for col, (name, caption, color) in enumerate((
            ("alipay.jpg", "📱 支付宝扫码", "#1677FF"),
            ("wechatpay.jpg", "💬 微信扫码", "#07C160"),
        )):
            card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
            tk.Label(card, text=caption, font=(FONT, 10, "bold"), fg=color, bg=CARD).pack(pady=(12, 6))
            p = self.app_dir / "assets" / name
            try:
                im = ImageOps.pad(Image.open(p).convert("RGB"), (300, 300),
                                  method=Image.LANCZOS, color="white")
                ph = ImageTk.PhotoImage(im)
                refs.append(ph)
                tk.Label(card, image=ph, bg="#FFFFFF", bd=0).pack(padx=16)
            except Exception:
                tk.Label(card, text=f"二维码未找到：{name}", font=(FONT, 9), fg=SUB,
                         bg=CARD, wraplength=260).pack(padx=16, pady=80)
            tk.Label(card, text="扫码后按提示支付即可", font=(FONT, 8), fg=SUB, bg=CARD
                     ).pack(pady=(8, 14))
        dlg._sponsor_refs = refs

        foot = tk.Frame(dlg, bg=BG)
        foot.pack(fill="x", padx=18, pady=(0, 16))
        tk.Label(foot, text="不打赏也完全不影响使用。", font=(FONT, 8), fg=SUB, bg=BG).pack(side="left")
        self._btn(foot, "关闭", dlg.destroy, kind="primary").pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.lift()
        dlg.focus_force()
        dlg.wait_window()

    def _build_ui(self) -> None:
        cfg = self.store.config["ui"]
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.configure(bg=BG)
        w, h = int(cfg.get("width", 560)), int(cfg.get("height", 840))
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        h = min(h, max(640, sh - 90))
        self.root.geometry(f"{w}x{h}+{max(20, sw - w - 36)}+32")
        self.root.minsize(500, 680)
        if cfg.get("always_on_top", True):
            self.root.attributes("-topmost", True)
        self._apply_icon()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda e: self.on_close())

        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=(12, 10))
        self.main_wrap = wrap

        # 顶栏：浅色卡片 + 左侧蓝色竖条，信息层级更清楚
        head = tk.Frame(wrap, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        head.pack(fill="x")
        accent = tk.Frame(head, bg=ACCENT, width=4)
        accent.pack(side="left", fill="y", padx=(0, 12))
        htext = tk.Frame(head, bg=CARD)
        htext.pack(side="left", fill="y", pady=(9, 8))
        hrow = tk.Frame(htext, bg=CARD)
        hrow.pack(anchor="w")
        logo = self.app_dir / "assets" / "logo_256.png"
        if logo.exists():
            try:
                img = tk.PhotoImage(file=str(logo))
                f = max(1, img.width() // 28)
                self._logo_img = img.subsample(f, f)
                tk.Label(hrow, image=self._logo_img, bg=CARD).pack(side="left", padx=(0, 8))
            except Exception:
                pass
        tk.Label(hrow, text="狗头军师助手", font=(FONT, 16, "bold"), fg=TEXT, bg=CARD).pack(side="left")
        tk.Label(htext, text="先接情绪，再给能发出去的话", font=(FONT, 8), fg=SUB, bg=CARD).pack(anchor="w")
        self._btn(head, "退出", self.quit_app, kind="ghost", small=True, tiny=True).pack(side="right", padx=(0, 8))
        self._btn(head, "托盘", self._minimize_to_tray, kind="ghost", small=True, tiny=True).pack(side="right", padx=(0, 6))
        self._btn(head, "话术库", self.open_library, kind="ghost", small=True, tiny=True).pack(side="right", padx=(0, 6))
        self._btn(head, "设置", self.open_settings, kind="ghost", small=True, tiny=True).pack(side="right", padx=(0, 6))
        self._btn(head, "☕ 赞助", self.open_sponsor, kind="warm", small=True, tiny=True).pack(side="right", padx=(0, 6))

        # 对象条：把对象、导入、最近记录收进一张卡片
        bar_card = tk.Frame(wrap, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        bar_card.pack(fill="x", pady=(10, 0))
        bar = tk.Frame(bar_card, bg=CARD)
        bar.pack(fill="x", padx=12, pady=(9, 3))
        tk.Label(bar, text="当前对象", font=(FONT, 9, "bold"), fg=TEXT, bg=CARD).pack(side="left")
        self.cbo_contact = ttk.Combobox(bar, state="readonly", width=13, font=(FONT, 9))
        self.cbo_contact.pack(side="left", padx=(8, 10))
        self.cbo_contact.bind("<<ComboboxSelected>>", self._on_contact_selected)
        self._btn(bar, "管理", self.open_contacts, kind="ghost", small=True).pack(side="left")
        self._btn(bar, "导入聊天", self.open_import, kind="ghost", small=True).pack(side="left", padx=(6, 0))
        self._btn(bar, "取最近记录", self.fill_recent, kind="ghost", small=True).pack(side="left", padx=(6, 0))

        self.status = tk.Label(bar_card, text="", font=(FONT, 8), fg=SUB, bg=CARD, anchor="w",
                               justify="left", wraplength=520)
        self.status.pack(fill="x", padx=12, pady=(0, 9))

        # 输入卡
        in_card = self._card(wrap)
        in_card.pack(fill="x", pady=(10, 0))
        top = tk.Frame(in_card, bg=CARD)
        top.pack(fill="x", padx=14, pady=(11, 5))
        tk.Label(top, text="聊天记录", font=(FONT, 10, "bold"), fg=TEXT, bg=CARD).pack(side="left")
        tk.Label(top, text="把微信里刚发生的对话贴进来", font=(FONT, 8), fg=SUB, bg=CARD
                 ).pack(side="left", padx=(8, 0))
        self.lbl_img = tk.Label(top, text="", font=(FONT, 9), fg=ACCENT, bg=CARD)
        self.lbl_img.pack(side="left", padx=(8, 0))
        self._btn(top, "读剪贴板", self.read_clipboard, kind="ghost", small=True).pack(side="right")

        self.txt_in = tk.Text(in_card, height=5, wrap="word", font=(FONT, 10),
                              bg="#FBFCFE", fg=TEXT, relief="flat", bd=0,
                              highlightthickness=1, highlightbackground=BORDER,
                              highlightcolor=ACCENT, padx=10, pady=8, insertbackground=TEXT)
        self.txt_in.pack(fill="x", padx=14)
        self.txt_in.insert("1.0", PLACEHOLDER)
        self.txt_in.configure(fg=SUB)
        self.txt_in.bind("<FocusIn>", self._ph_out)
        self.txt_in.bind("<FocusOut>", self._ph_in)
        self.txt_in.bind("<Control-Return>", lambda e: (self.generate(), "break")[1])
        self.txt_in.bind("<<Paste>>", lambda e: self.root.after_idle(lambda: self.txt_in.see("end")))

        ask_row = tk.Frame(in_card, bg=CARD)
        ask_row.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(ask_row, text="诉求", font=(FONT, 9), fg=SUB, bg=CARD).pack(side="left")
        self.ent_ask = tk.Entry(ask_row, font=(FONT, 9), relief="flat", bg="#FBFCFE", fg=SUB,
                                highlightthickness=1, highlightbackground=BORDER,
                                highlightcolor=ACCENT, insertbackground=TEXT)
        self.ent_ask.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=4)
        self.ent_ask.insert(0, ASK_HINT)
        self.ent_ask.bind("<FocusIn>", self._ask_in)
        self.ent_ask.bind("<FocusOut>", self._ask_out)
        self.ent_ask.bind("<Return>", lambda e: (self.generate(), "break")[1])
        self.ask_on = True

        tk.Label(in_card, text="记录可留空；只想问一句话时，把你要的效果写进「诉求」即可。",
                 font=(FONT, 8), fg=SUB, bg=CARD, anchor="w", wraplength=510, justify="left"
                 ).pack(fill="x", padx=14, pady=(6, 11))

        # 操作行
        act = tk.Frame(wrap, bg=BG)
        act.pack(fill="x", pady=(10, 8))
        self.btn_gen = self._btn(act, "生成回复  Ctrl+Alt+M", self.generate, kind="primary")
        self.btn_gen.pack(side="left", fill="x", expand=True, ipady=3)
        self._btn(act, "换一版", self.regenerate, kind="ghost").pack(side="left", padx=(8, 0), ipady=3)
        self._btn(act, "追问", self._focus_followup, kind="ghost").pack(side="left", padx=(8, 0), ipady=3)

        # 底部（先占位，避免被输出区挤掉）
        self.toast = tk.Label(wrap, text="", font=(FONT, 9), fg=SUB, bg=BG, anchor="w")
        self.toast.pack(side="bottom", fill="x", pady=(2, 0))

        bottom = tk.Frame(wrap, bg=BG)
        bottom.pack(side="bottom", fill="x", pady=(6, 2))
        bottom.columnconfigure(0, weight=3)
        bottom.columnconfigure(1, weight=2)
        self._btn(bottom, "填入聊天窗口", self.insert_to_window, kind="primary").grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        self._btn(bottom, "存入库", self.save_library, kind="ghost").grid(
            row=0, column=1, sticky="ew", padx=(6, 0))

        # 效果回填（生成前按钮置灰）
        self.fb = tk.Frame(wrap, bg=BG)
        self.fb.pack(side="bottom", fill="x", pady=(0, 2))
        self.lbl_fb = tk.Label(self.fb, text="这条之后她怎么回的", font=(FONT, 9), fg=SUB, bg=BG)
        self.lbl_fb.pack(side="left")
        self.fb_btns = []
        for label, val in (("好用", "good"), ("一般", "so-so"), ("没效果", "bad")):
            b = self._btn(self.fb, label, lambda v=val: self.rate(v), kind="ghost", small=True)
            b.pack(side="left", padx=(4, 0))
            b.configure(state="disabled")
            self.fb_btns.append(b)
        b_reply = self._btn(self.fb, "记她的回复", self.capture_reply, kind="ghost", small=True)
        b_reply.pack(side="left", padx=(4, 0))
        b_reply.configure(state="disabled")
        self.fb_btns.append(b_reply)

        # 追问条（默认隐藏）
        self.fu = tk.Frame(wrap, bg=BG)
        tk.Label(self.fu, text="追问", font=(FONT, 9), fg=SUB, bg=BG).pack(side="left")
        self.ent_fu = tk.Entry(self.fu, font=(FONT, 10), relief="flat", bg="#FBFCFE", fg=TEXT,
                               highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.ent_fu.pack(side="left", fill="x", expand=True, padx=6, ipady=4)
        self.ent_fu.bind("<Return>", lambda e: self.follow_up())
        self._btn(self.fu, "问", self.follow_up, kind="primary", small=True).pack(side="left")

        # 输出卡
        out_card = self._card(wrap)
        out_card.pack(fill="both", expand=True, pady=(8, 0))
        ohead = tk.Frame(out_card, bg=CARD)
        ohead.pack(fill="x", padx=14, pady=(11, 4))
        tk.Label(ohead, text="回复建议", font=(FONT, 10, "bold"), fg=TEXT, bg=CARD).pack(side="left")
        self.btn_more = self._btn(ohead, "⋯", self._open_more_actions, kind="ghost", small=True)
        self.btn_more.pack(side="right", padx=(4, 0))
        self._btn(ohead, "复制", self.copy_draft, kind="ghost", small=True).pack(side="right", padx=(8, 0))
        self.lbl_route = tk.Label(ohead, text="", font=(FONT, 8), fg=SUB, bg=CARD)
        self.lbl_route.pack(side="right", padx=(8, 0))

        body = tk.Frame(out_card, bg=CARD)
        body.pack(fill="both", expand=True, padx=(14, 10), pady=(0, 10))
        self.out = tk.Text(body, wrap="word", font=(FONT, 10), bg=CARD, fg=TEXT,
                           relief="flat", bd=0, highlightthickness=0, padx=0, pady=0,
                           spacing1=2, spacing3=6, cursor="arrow")
        self.out.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.out.yview)
        sb.pack(side="right", fill="y")
        self.out.configure(yscrollcommand=sb.set, state="disabled")
        self.out.tag_configure("h", font=(FONT, 9, "bold"), foreground=ACCENT, spacing1=10)
        self.out.tag_configure("draft", font=(FONT, 12), foreground=TEXT,
                               lmargin1=8, lmargin2=8, rmargin=8, spacing1=8, spacing3=8, background="#F5F8FF")
        self.out.tag_configure("body", font=(FONT, 9), foreground=SUB, spacing3=6)
        self.out.tag_configure("empty", font=(FONT, 9), foreground="#A0A6AE")
        self.out.tag_configure("divider", font=(FONT, 9), foreground="#9AA1AA", spacing1=14)
        self._render("")
        self._apply_content_font_scale(self.store.config.get("ui", {}).get("font_scale", 1.0))

    def _card(self, parent) -> tk.Frame:
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)

    def _btn(self, parent, text, cmd, kind="ghost", small=False, tiny=False):
        if kind == "primary":
            bg, fg, hb, hover, fs = ACCENT, "#FFFFFF", ACCENT, ACCENT_HOVER, 10
        elif kind == "warm":
            bg, fg, hb, hover, fs = WARM_BG, WARM_FG, WARM_BORDER, WARM_HOVER, (9 if tiny else (9 if small else 10))
        else:
            bg, fg, hb, hover, fs = CARD, TEXT, BORDER, "#EEF3FF", (9 if tiny else (9 if small else 10))
        b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                      bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                      font=(FONT, fs), cursor="hand2", highlightthickness=1,
                      highlightbackground=hb, padx=(5 if tiny else (8 if small else 12)),
                      pady=(3 if tiny else (3 if small else 6)))
        b.bind("<Enter>", lambda e, w=b, c=hover: w.configure(bg=c) if str(w["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e, w=b, c=bg: w.configure(bg=c) if str(w["state"]) != "disabled" else None)
        return b

    # ---------------------------------------------------------------- 字号
    def _apply_content_font_scale(self, scale: float) -> None:
        """按档位缩放主界面文字的字体，按钮、标签和输入区一起联动。"""
        self._font_scale = max(0.8, min(1.45, float(scale or 1.0)))
        if hasattr(self, "main_wrap"):
            self._scale_font_tree(self.main_wrap)

    def _scale_font_tree(self, parent) -> None:
        for wid in parent.winfo_children():
            self._scale_widget_font(wid, self._font_scale)
            if isinstance(wid, tk.Text):
                for tag in wid.tag_names():
                    spec = wid.tag_cget(tag, "font")
                    if spec:
                        self._scale_tag_font(wid, tag, spec, self._font_scale)
            self._scale_font_tree(wid)

    def _font_parts(self, widget, spec: str):
        try:
            parts = widget.tk.splitlist(spec)
            if len(parts) < 2:
                return None
            size = int(float(parts[1]))
            return parts[0], size, tuple(parts[2:])
        except Exception:
            return None

    def _scale_widget_font(self, widget, scale: float) -> None:
        try:
            spec = widget.cget("font")
        except Exception:
            return
        parsed = self._font_parts(widget, spec)
        if not parsed:
            return
        key = (str(widget), "font")
        base = self._font_base.setdefault(key, parsed)
        family, size, styles = base
        new_size = max(6, int(round(size * scale)))
        try:
            widget.configure(font=(family, new_size) + styles)
        except Exception:
            pass

    def _scale_tag_font(self, widget: tk.Text, tag: str, spec: str, scale: float) -> None:
        parsed = self._font_parts(widget, spec)
        if not parsed:
            return
        key = (str(widget), "tag:" + tag)
        base = self._font_base.setdefault(key, parsed)
        family, size, styles = base
        new_size = max(6, int(round(size * scale)))
        try:
            widget.tag_configure(tag, font=(family, new_size) + styles)
        except Exception:
            pass

    # ---------------------------------------------------------------- 占位符
    def _ph_out(self, _=None):
        if self.placeholder_on:
            self.txt_in.delete("1.0", "end")
            self.txt_in.configure(fg=TEXT)
            self.placeholder_on = False

    def _ph_in(self, _=None):
        if not self.txt_in.get("1.0", "end").strip():
            self.txt_in.insert("1.0", PLACEHOLDER)
            self.txt_in.configure(fg=SUB)
            self.placeholder_on = True

    def get_log(self) -> str:
        if self.placeholder_on:
            return ""
        return self.txt_in.get("1.0", "end").strip()

    def set_log(self, text: str) -> None:
        self.txt_in.delete("1.0", "end")
        self.txt_in.insert("1.0", text)
        self.txt_in.configure(fg=TEXT)
        self.placeholder_on = False
        # 默认停在最新一条：刚复制/同步进来的记录直接看到结尾，历史往上滚
        self.txt_in.see("end")
        self.root.after_idle(lambda: self.txt_in.see("end") if self.txt_in.winfo_exists() else None)

    # ---------------------------------------------------------------- 诉求框
    def _ask_in(self, _=None):
        if self.ask_on:
            self.ent_ask.delete(0, "end")
            self.ent_ask.configure(fg=TEXT)
            self.ask_on = False

    def _ask_out(self, _=None):
        if not self.ent_ask.get().strip():
            self.ent_ask.delete(0, "end")
            self.ent_ask.insert(0, ASK_HINT)
            self.ent_ask.configure(fg=SUB)
            self.ask_on = True

    def get_ask(self) -> str:
        return "" if self.ask_on else self.ent_ask.get().strip()

    # ---------------------------------------------------------------- 热键
    def _register_hotkey(self, hotkey: str) -> None:
        try:
            import keyboard
        except Exception:
            self._toast("未安装 keyboard，热键不可用")
            return
        try:
            if self._hotkey_handle is not None:
                keyboard.remove_hotkey(self._hotkey_handle)
            self._hotkey_handle = keyboard.add_hotkey(hotkey, lambda: self.root.after(0, self._on_hotkey))
        except Exception as e:
            self._toast(f"热键注册失败：{e}")

    def _on_hotkey(self):
        if self.root.state() == "normal" and self.root.focus_displayof():
            self.on_close()
            return
        self.show(capture=True)
        self.wechat_hwnd = wb.find_wechat_window(self.target_hwnd)
        if self.behavior().get("hotkey_read_clip", True):
            self.root.after(180, self._hotkey_read_then_generate)

    def _hotkey_read_then_generate(self):
        """热键一键流程：剪贴板有有效记录 -> 自动收进对象库并生成。"""
        ok = self.read_clipboard(silent=True)
        if ok and self.behavior().get("one_key_flow", True):
            self.root.after(260, self.generate)

    def show(self, capture: bool = False):
        if capture:
            try:
                import win32gui
                hwnd = win32gui.GetForegroundWindow()
                if hwnd and hwnd != self.root.winfo_id():
                    self.target_hwnd = hwnd
            except Exception:
                pass
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.focus_force()
        self.txt_in.focus_set()

    def hide(self):
        self.root.withdraw()

    def _minimize_to_tray(self):
        if not self.tray.ok:
            self._toast("托盘没起来，先看设置里是否开了托盘常驻")
            return
        self.hide()
        self._toast("已收进托盘：点图标或按热键叫我")

    def on_close(self):
        """关闭按钮：按设置收进托盘，还是真退出。"""
        if self.behavior().get("close_to_tray", True) and self.tray.ok:
            self.hide()
            if not self._tray_hint_once:
                self._tray_hint_once = True
                self.tray.notify("已收进托盘，按 Ctrl+Alt+M 或点图标再叫我")
            return
        self.quit_app()

    def quick_generate(self):
        """托盘点『读剪贴板并生成』：一句到底，不用手点。"""
        self.show(capture=False)
        self.root.after(120, lambda: (self.read_clipboard(silent=True), self.generate()))

    def quit_app(self):
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            import keyboard
            if self._hotkey_handle is not None:
                keyboard.remove_hotkey(self._hotkey_handle)
        except Exception:
            pass
        self.root.destroy()

    # ---------------------------------------------------------------- 联系人
    def _refresh_contacts(self) -> None:
        names = self.contacts.names()
        self.cbo_contact.configure(values=names)
        if self.current_contact and self.current_contact in names:
            self.cbo_contact.set(self.current_contact)
        elif names and (not self.current_contact or self.current_contact not in names):
            # 优先选聊天记录最多的对象；避免误建的空白/路径对象抢到默认位
            self.current_contact = max(names, key=lambda n: len(self.contacts.messages(n)))
            self.cbo_contact.set(self.current_contact)
        self._refresh_status()

    def _on_contact_selected(self, _=None):
        old = self.current_contact
        self.current_contact = self.cbo_contact.get()
        self._refresh_status()
        if not self.current_contact:
            return
        n = len(self.contacts.messages(self.current_contact))
        # 换了对象还留着上一个人的记录 → 自动换成这个对象的最近记录
        if old != self.current_contact:
            log = self.get_log().strip()
            if log:
                try:
                    p = parse_wechat(log, self.my_names)
                    msgs = p.get("messages") or []
                    aliases = self._aliases_for(self.current_contact, p.get("speakers") or {})
                    clean, _ = keep_related(msgs, self.current_contact, self.my_names, aliases)
                    has_her = any(m.get("role") == "them" for m in clean)
                except Exception:
                    has_her = False
                if not has_her and n:
                    self.fill_recent()
        self._toast(f"已切到 {self.current_contact}：{n} 条记录，"
                    f"{len(self.contacts.events(self.current_contact))} 个事件")

    def _set_contact(self, name: str) -> None:
        self.current_contact = name
        self._refresh_contacts()

    def _contact_choices(self, extra=None) -> list[str]:
        """对象下拉候选：已有对象 + 它们的别名 + 记录里新出现的叫法。"""
        vals: list[str] = []
        for n in self.contacts.names():
            if n not in vals:
                vals.append(n)
            for a in (self.contacts.get(n).get("aliases") or []):
                if a and a not in vals:
                    vals.append(a)
        for n in (extra or []):
            if n and n not in vals:
                vals.append(n)
        return vals

    def _aliases_for(self, name: str, speakers=None) -> list[str]:
        """这个对象能认的所有叫法：代号 + 已记别名 + 记录里能匹配上的说话人。"""
        vals: list[str] = []
        for x in [name] + list((self.contacts.get(name).get("aliases") or [])):
            if x and x not in vals:
                vals.append(x)
        for s in (speakers or []):
            s = str(s or "").strip()
            if not s or s in vals:
                continue
            try:
                if self.contacts.match(s) == name:
                    vals.append(s)
            except Exception:
                continue
        return vals

    def _auto_contact(self, target: str) -> str:
        if not target:
            return self.current_contact
        matched = self.contacts.match(target)
        if matched:
            return matched
        name = self.contacts.ensure(target)
        self._toast(f"新对象「{target}」已自动加入对象管理")
        return name

    def fill_recent(self):
        if not self.current_contact:
            self._toast("先选一个对象")
            return
        msgs = self.contacts.messages(self.current_contact, limit=16)
        if not msgs:
            self._toast("这个对象还没有历史记录，先「导入聊天」")
            return
        self.set_log(to_annotated(msgs))
        self._toast(f"已取最近 {len(msgs)} 条")

    def _recent_log(self) -> str:
        if not self.current_contact:
            return ""
        msgs = self.contacts.messages(self.current_contact, limit=14)
        return to_annotated(msgs) if msgs else ""

    def _context_n(self) -> int:
        try:
            return max(4, min(50, int(self.store.config.get("context_n", 16))))
        except Exception:
            return 16

    def _context_with_history(self, clean: list[dict]) -> list[dict]:
        """把刚读到的记录补成该对象最近 N 条，避免只拿半截对话去生成。"""
        n = self._context_n()
        stored = self.contacts.messages(self.current_contact, limit=n)
        merged: list[dict] = []
        seen = set()
        for m in stored + list(clean or []):
            key = f"{m.get('ts', '')}|{m.get('speaker', '')}|{m.get('text', '')}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(m)
        return merged[-n:]

    # ---------------------------------------------------------------- 剪贴板
    def read_clipboard(self, silent: bool = False) -> bool:
        raw = ""
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            raw = ""
        if not raw.strip():
            img = self._grab_clipboard_image()
            if img is not None:
                self._attach_image(img)
                return True
            if not silent:
                self._toast("剪贴板里没有文本或图片")
            return False
        parsed = parse_wechat(raw, self.my_names)
        st = parsed.get("stats", {})
        target = parsed.get("target") or ""
        if st.get("total", 0) < 2 and not target:
            # 热键唤起时剪贴板里常常是别的东西，不要硬塞进来
            if not silent:
                self._toast("剪贴板里没找到像聊天记录的内容，手动贴一段吧")
            return False
        if target:
            self._set_contact(self._auto_contact(target))
        name = self.current_contact
        clean, dropped = parsed["messages"], 0
        if name:
            aliases = self._aliases_for(name, (parsed.get("speakers") or {}).keys())
            clean, dropped = keep_related(parsed["messages"], name, self.my_names, aliases)
            if not clean:
                self._toast(f"这段记录里没有「{name}」的话，已忽略（{dropped} 条属于别人）")
                return False
        if not clean:
            return False

        added = 0
        if name:
            # 同一段记录重复复制时不重复入库、不重复触发判定
            if not self.msg_cache.is_duplicate(name, clean):
                added = self.contacts.append_messages(name, clean)
                self.msg_cache.mark(name, clean)
                if added:
                    self.contacts.learn_style(name, self.my_names)
                    self._refresh_status()

        self.set_log(to_annotated(clean))
        self._toast(f"读入 {len(clean)} 条（对方 {sum(1 for m in clean if m.get('role') == 'them')} / 我 {sum(1 for m in clean if m.get('role') == 'me')}）"
                    + (f"，新入库 {added} 条" if added else "，已去重")
                    + (f"，对象：{name}" if name else "")
                    + (f"，已剔掉 {dropped} 条别人" if dropped else ""))
        return True

    def _grab_clipboard_image(self):
        try:
            from PIL import ImageGrab
            data = ImageGrab.grabclipboard()
        except Exception:
            return None
        if data is None or isinstance(data, list):
            return None
        return data

    def _attach_image(self, img) -> None:
        try:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            self.pending_image_b64 = base64.b64encode(buf.getvalue()).decode()
            self.lbl_img.configure(text=f"已附加截图 {img.width}×{img.height}  ✕")
            self.lbl_img.bind("<Button-1>", lambda e: self._clear_image())
            self._toast("已附加截图（需在设置里配视觉模型）")
        except Exception as e:
            self._toast(f"图像处理失败：{e}")

    def _clear_image(self):
        self.pending_image_b64 = ""
        self.lbl_img.configure(text="")

    # ---------------------------------------------------------------- 生成
    def generate(self, again: bool = False, rescue: bool = False,
                 ask_override: str = "", style_override: str = ""):
        if self._busy:
            if not rescue:
                self._toast("还在生成，稍等一下")
            return
        log = self.get_log() if not rescue else ""

        ask = ask_override or self.get_ask()
        if not log and self.current_contact:
            log = self._recent_log()
            if log and not rescue:
                self.set_log(log)
                self._toast("记录是空的，已用该对象最近的历史记录")
        # 记录里混了别人的话 → 只留当前对象和我的
        if log and self.current_contact and not rescue:
            p = parse_wechat(log, self.my_names)
            msgs = p.get("messages") or []
            if len(msgs) >= 2 and any(m.get("role") == "them" for m in msgs):
                aliases = self._aliases_for(self.current_contact, p.get("speakers") or {})
                clean, dropped = keep_related(msgs, self.current_contact, self.my_names, aliases)
                has_her = any(m.get("role") == "them" for m in clean)
                if dropped and clean and has_her:
                    log = to_annotated(clean)
                    self.set_log(log)
                    self._toast(f"记录里混了别人，只保留「{self.current_contact}」和你的话（剔掉 {dropped} 条）")
                elif dropped and not has_her:
                    self._toast(f"这段记录里没有「{self.current_contact}」的话，先切对象或换一段")
                    return
                if has_her and self.behavior().get("use_history_context", True):
                    merged = self._context_with_history(clean)
                    if len(merged) > len(clean):
                        log = to_annotated(merged)
                        self.set_log(log)
        if not log and not ask and not self.pending_image_b64:
            self._toast("贴一段记录，或在「诉求」里写一句你想要的效果")
            return

        self.last_log = log
        self.thread = []
        self.last_system = ""
        self.last_files = []
        self._rescue_mode = rescue
        self._pack_followup(False)
        self._enable_feedback(False)
        self._busy = True
        self.btn_gen.configure(state="disabled", text="补救中…" if rescue else "生成中…")
        self._render("", streaming=True)
        if not rescue:
            self.lbl_route.configure(text="")

        style = style_override
        if again:
            style = "\n这次换一种风格重写：保持同样的事实判断，但语气更低压力、更具体，不要重复上一次的句子。"
        if self.extra_style:
            style += f"\n用户长期风格要求：{self.extra_style}"

        threading.Thread(target=self._worker, args=(log, ask, again, style), daemon=True).start()

    def _worker(self, log: str, ask: str, again: bool, style: str):
        cfg = self.store.config["api"]
        client = self.client
        ask = ask or DEFAULT_ASK
        log = log or "（没有聊天记录，用户只给了诉求。请直接按诉求给一条能发出去的话，不要编造对方的原话。）"
        try:
            # 1) 两段式路由：先让模型判断问题类型
            idx = 0
            if client.mock:
                idx = 1
            else:
                self._queue.put(("stage", "判断问题类型…"))
                key = hashlib.md5((log[:600] + "|" + ask).encode()).hexdigest()
                idx = self._cat_cache.get(key, 0)
                if not idx:
                    raw = client.stream_chat(self.engine.classify_prompt(log, ask),
                                             temperature=0.0, max_tokens=24)
                    idx = self.engine.parse_category(raw)
                    if idx:
                        self._cat_cache[key] = idx
            files = self.engine.route_by_category(idx, log + ask) if idx else None

            # 2) 组装提示词：档案 + 长期记忆 + 效果回收 + 说话风格 + 知识库
            name = self.current_contact
            profile_ctx = self.contacts.memory_block(name, self.my_names, with_style=False) if name else ""
            style_ctx = self.contacts.style_block(name, self.my_names) if name else ""
            pack = self.engine.build_prompt(log, ask, profile_ctx, style, style_ctx, files)
            self.last_system = pack.system
            self.last_files = pack.refs

            # 3) 主请求
            self._queue.put(("stage", "生成回复…"))
            messages = [{"role": "system", "content": pack.system},
                        {"role": "user", "content": self._user_content(pack.user)}]
            if self.pending_image_b64 and cfg.get("vision_model"):
                client = LLMClient({**cfg, "model": cfg["vision_model"]})
            text = client.stream_chat(
                messages,
                temperature=1.0 if again else None,
                on_delta=lambda p: self._queue.put(("delta", p)),
            )
            self._queue.put(("done", text))
        except LLMError as e:
            self._queue.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", f"{type(e).__name__}: {e}"))

    def _user_content(self, text: str):
        if not self.pending_image_b64:
            return text
        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{self.pending_image_b64}"}},
        ]

    def regenerate(self):
        if not self.last_text:
            self._toast("先点一次『生成回复』")
            return
        self.generate(again=True)

    def _focus_followup(self):
        if not self.last_system:
            self._toast("先生成一条，再追问")
            return
        self._pack_followup(True)
        self.ent_fu.focus_set()

    def _pack_followup(self, show: bool) -> None:
        if show:
            if not self.fu.winfo_ismapped():
                self.fu.pack(side="bottom", fill="x", pady=(0, 4))
        else:
            if self.fu.winfo_ismapped():
                self.fu.pack_forget()

    def follow_up(self):
        if not self.last_system:
            self._toast("先生成一条，再追问")
            return
        if self._busy:
            self._toast("还在生成")
            return
        q = self.ent_fu.get().strip()
        if not q:
            self._toast("输入你想追问的点，比如：她这句是不是在逼我表态")
            return
        self.ent_fu.delete(0, "end")
        self.thread.append({"role": "user", "content": q})
        if len(self.thread) > 6:      # 最多 3 轮
            self.thread = self.thread[-6:]
        self._busy = True
        self.btn_gen.configure(state="disabled", text="生成中…")
        self.out.configure(state="normal")
        self.out.insert("end", f"\n追问：{q}\n", "divider")
        self.out.configure(state="disabled")
        self._render_divider_pending = True
        threading.Thread(target=self._fu_worker, daemon=True).start()

    def _fu_worker(self):
        try:
            messages = [{"role": "system", "content": self.last_system}] + self.thread
            text = self.client.stream_chat(
                messages, temperature=0.6,
                on_delta=lambda p: self._queue.put(("delta", p)),
            )
            self.thread.append({"role": "assistant", "content": text})
            self._queue.put(("followup_done", text))
        except LLMError as e:
            self._queue.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", f"{type(e).__name__}: {e}"))

    def _pump(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "stage":
                    self._toast(payload)
                elif kind == "delta":
                    self._append_stream(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "followup_done":
                    self._finish(payload, followup=True)
                elif kind == "judge_done":
                    self._apply_auto_judge(*payload)
                elif kind == "error":
                    self._fail(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _append_stream(self, piece: str):
        if not getattr(self, "_streaming", False):
            self._render("", streaming=True)
        self.out.configure(state="normal")
        self.out.insert("end", piece, "body")
        self.out.see("end")
        self.out.configure(state="disabled")

    def _finish(self, text: str, followup: bool = False):
        self._busy = False
        self.btn_gen.configure(state="normal", text="生成回复  Ctrl+Alt+M")
        self.last_text = text
        sections, draft = split_sections(text)
        self.last_draft = draft
        if draft and self.current_contact:
            self.contacts.add_draft(self.current_contact, draft,
                                    "补救" if self._rescue_mode else ("追问" if followup else "生成"))
            if self.behavior().get("auto_judge", True):
                self.contacts.set_pending(self.current_contact, draft, self.last_log,
                                          rescue=self._rescue_mode)
        if followup:
            self._render(text, clearing=False)
        else:
            self._render(text)
            self._set_route_hint(self.last_files)
            if self._rescue_mode:
                self.lbl_route.configure(text="补救轮 · 自动生成", fg="#C53030")
                try:
                    self.out.configure(state="normal")
                    self.out.insert("1.0", "补救轮 · 上一条被判反效果\n这条只处理她刚回的那句，不重复上一条。\n\n")
                    self.out.configure(state="disabled")
                except tk.TclError:
                    pass
        self._pack_followup(True)
        self._enable_feedback(True)

        copied = False
        if draft and self.behavior().get("auto_copy", True):
            copied = self._copy(draft)
        if self._rescue_mode:
            self._rescue_mode = False
            self._toast("补救话术已自动生成" + ("并复制到剪贴板，直接 Ctrl+V" if copied else "，点『复制话术』取用"))
            self.tray.notify("补救话术已就绪" + ("，已复制到剪贴板" if copied else "") + f"：{draft[:24]}")
        elif copied:
            self._toast("已生成并复制到剪贴板，切回微信 Ctrl+V 就能发")
        else:
            self._toast("完成。『填入聊天窗口』只粘贴，不会替你发送。")
        if not self.root.winfo_ismapped() or self.root.state() != "normal":
            if not self._rescue_mode:
                self.tray.notify("话术已生成" + ("，已复制到剪贴板" if copied else ""))

    def _enable_feedback(self, on: bool) -> None:
        for b in getattr(self, "fb_btns", []):
            b.configure(state="normal" if on else "disabled")
        try:
            self.lbl_fb.configure(fg=SUB if on else "#B7BCC4")
        except Exception:
            pass

    # ---------------------------------------------------------------- 效果回填
    def rate(self, rating: str):
        if not self.last_draft:
            self._toast("先生成一条再评")
            return
        if not self.current_contact:
            self._toast("这条没记到对象上：先选一个对象，或下次用「读剪贴板」自动认人")
            return
        self.contacts.add_feedback(self.current_contact, rating, self.last_draft)
        self.contacts.clear_pending(self.current_contact)
        self._refresh_status()
        tip = {"good": "记下了：这类写法她吃，下次沿用",
               "so-so": "记下了：中性，下次会换更具体的写法",
               "bad": "记下了：这类别再来一遍"}.get(rating, "已记录")
        self._toast(tip)
        self._enable_feedback(False)
        if rating == "bad":
            self._auto_rescue(self.current_contact, "")

    def capture_reply(self):
        """把她的真实回复贴回来：既更新聊天记录，也沉淀成效果样本。"""
        if not self.last_draft or not self.current_contact:
            self._toast("先生成一条，并且确定这条是发给哪个对象的")
            return
        win = tk.Toplevel(self.root)
        self.set_win_icon(win)
        win.title("她怎么回的")
        win.configure(bg=CARD)
        win.geometry(f"420x260+{self.root.winfo_x() + 40}+{self.root.winfo_y() + 90}")
        win.attributes("-topmost", True)
        tk.Label(win, text=f"粘贴「{self.current_contact}」的回复（只回一句也行），会写进她的记录并作为效果样本。",
                 font=(FONT, 9), fg=SUB, bg=CARD, wraplength=380, justify="left", anchor="w").pack(fill="x", padx=14, pady=(12, 6))
        txt = tk.Text(win, height=5, wrap="word", font=(FONT, 10), relief="flat", bg="#FBFCFE",
                      highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        txt.pack(fill="both", expand=True, padx=14)
        bar = tk.Frame(win, bg=CARD)
        bar.pack(fill="x", padx=14, pady=10)
        tk.Label(bar, text="效果", font=(FONT, 9), fg=SUB, bg=CARD).pack(side="left")
        rating = {"v": "so-so"}
        for label, val in (("推动了一步", "good"), ("一般", "so-so"), ("没推动", "bad")):
            b = self._btn(bar, label, lambda v=val: (rating.update({"v": v}), info.configure(text=f"已选：{label}")),
                          kind="ghost", small=True)
            b.pack(side="left", padx=(4, 0))
        info = tk.Label(bar, text="已选：一般", font=(FONT, 9), fg=SUB, bg=CARD)
        info.pack(side="left", padx=8)

        def save():
            reply = txt.get("1.0", "end").strip()
            self.contacts.add_feedback(self.current_contact, rating["v"], self.last_draft, reply)
            if reply:
                self.contacts.append_messages(self.current_contact, [{
                    "ts": time.strftime("%Y-%m-%d %H:%M"), "speaker": self.current_contact,
                    "role": "them", "text": reply,
                }])
                self.contacts.learn_style(self.current_contact, self.my_names)
            win.destroy()
            self.contacts.clear_pending(self.current_contact)
            self._enable_feedback(False)
            self._refresh_status()
            self._toast("已回收这次效果，下次生成会参考她真实反应")
            if rating["v"] == "bad":
                self._auto_rescue(self.current_contact, reply)

        self._btn(bar, "保存", save, kind="primary").pack(side="right")
        self._btn(bar, "取消", win.destroy, kind="ghost").pack(side="right", padx=(0, 6))
        txt.focus_set()

    # ---------------------------------------------------------------- 自动判定
    def _clip_text(self) -> str:
        try:
            return self.root.clipboard_get() or ""
        except tk.TclError:
            return ""

    def _watch_clip(self):
        try:
            b = self.behavior()
            if (b.get("auto_judge", True) or b.get("auto_sync_clip", True)) \
                    and not self._busy and not self._judge_busy:
                self._watch_once()
        except Exception:
            pass
        self.root.after(1600, self._watch_clip)

    def _watch_once(self) -> None:
        if not self.my_names:
            return            # 分不清谁说的，就不自动判
        text = self._clip_text()
        if len(text) < 6:
            return
        h = hashlib.md5(text.encode()).hexdigest()
        if h == self._clip_hash:
            return
        self._clip_hash = h
        parsed = parse_wechat(text, self.my_names)
        msgs = parsed.get("messages") or []
        if len(msgs) < 2:
            return
        target = parsed.get("target") or ""
        name = self._auto_contact(target) if target else self.current_contact
        if not name:
            return
        if not target and name not in (parsed.get("speakers") or []):
            return            # 剪贴板里不是这个人的聊天，别乱收
        aliases = self._aliases_for(name, parsed.get("speakers") or [])
        clean, dropped = keep_related(msgs, name, self.my_names, aliases)
        if not clean:
            return
        if self.msg_cache.is_duplicate(name, clean):
            return
        added = self.contacts.append_messages(name, clean)
        self.msg_cache.mark(name, clean)
        if not added:
            return
        self.contacts.learn_style(name, self.my_names)
        pend = self.contacts.pending(name)
        if not pend:
            self._toast(f"已把「{name}」的新聊天收进记录（{added} 条）"
                        + (f"，剔掉 {dropped} 条别人" if dropped else ""))
            return
        if time.time() - float(pend.get("epoch", 0)) > 3 * 86400:
            self.contacts.clear_pending(name)
            return
        her = [m.get("text", "") for m in clean if m.get("role") == "them" and m.get("text")]
        if not her:
            return
        self._judge_busy = True
        threading.Thread(target=self._judge_worker,
                         args=(name, pend, her[-2:]), daemon=True).start()

    def _judge_worker(self, name: str, pend: dict, her: list[str]):
        reply = " / ".join(her)[-300:]
        try:
            if self.client.mock:
                rating, reason = "good", "（演示模式判定）"
            else:
                verdict = self.client.stream_chat(
                    self.engine.judge_prompt(pend.get("draft", ""), reply, pend.get("context", "")),
                    temperature=0.0, max_tokens=40)
                rating, reason = self.engine.parse_verdict(verdict)
        except Exception:
            self._queue.put(("judge_done", (name, pend, reply, "", "")))
            return
        self._queue.put(("judge_done", (name, pend, reply, rating, reason)))

    def _apply_auto_judge(self, name: str, pend: dict, reply: str, rating: str, reason: str):
        self._judge_busy = False
        if not rating:
            tries = self.contacts.bump_pending(name)
            if tries >= 2:
                self.contacts.clear_pending(name)
            return
        self.contacts.add_feedback(name, rating, pend.get("draft", ""), f"{reply[:60]}｜{reason}")
        self.contacts.clear_pending(name)
        label = {"good": "有效", "so-so": "一般", "bad": "反效果"}.get(rating, rating)
        self._toast(f"自动判定 {name}：这条{label}｜她回了：{reply[:26]}")
        self.tray.notify(f"{name}：这条话术{label}（{reply[:20]}）")
        self._refresh_status()
        color = {"good": "#1F9D55", "so-so": "#B7791F", "bad": "#C53030"}.get(rating, SUB)
        if self.root.winfo_ismapped():
            self.lbl_fb.configure(text=f"上次自动判定：{label}", fg=color)
        if rating == "bad" and not pend.get("rescue"):
            self._auto_rescue(name, reply)

    def _auto_rescue(self, name: str, reply: str):
        """反效果 → 直接出一条补救话术，不用用户再点生成。"""
        if not self.behavior().get("auto_rescue", True) or self._rescue_mode:
            return
        if self._busy:
            self.tray.notify(f"{name}：这条是反效果，但上一次还在生成，补救已跳过")
            return
        if self.current_contact != name:
            self._set_contact(name)
        her = (reply or "（她没回，或只回了很短一句）")[:150]
        ask = (f"我上一条发出去的话术起了反效果。她刚回的是：{her}。\n"
               "请给一条补救话术，只一条、能直接发：先接住她这句话，不要辩解、不要连问、"
               "不要重复上一条的句式和用词，给她退路和选择权，宁短不长。")
        style = ("\n这是补救轮：只处理眼前这一句，不重新表忠心、不追加邀约；"
                 "一条话术只承载一个动作，结尾留出她不回的余地。")
        self._toast(f"判定反效果，正在自动出补救话术…")
        self.tray.notify(f"{name}：这条是反效果，正在自动补救")
        self.generate(rescue=True, ask_override=ask, style_override=style)

    def _fail(self, msg: str):
        self._busy = False
        self.last_draft = ""
        self.btn_gen.configure(state="normal", text="生成回复  Ctrl+Alt+M")
        self._show_message("这次没跑通", f"{msg}\n\n可以点右下角『复制给豆包』，把完整提示词拿去手动问一次。")
        self._toast("生成失败，原因见上")

    def _show_message(self, title: str, body: str) -> None:
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("end", title + "\n", "h")
        self.out.insert("end", body, "body")
        self.out.configure(state="disabled")

    # ---------------------------------------------------------------- 渲染
    def _render(self, text: str, streaming: bool = False, clearing: bool = True) -> None:
        self._streaming = streaming
        self.out.configure(state="normal")
        if clearing:
            self.out.delete("1.0", "end")
        if streaming:
            self.out.configure(state="disabled")
            return
        if not text.strip():
            if clearing:
                self.out.insert("end", "还没有建议。\n贴好记录后按 Ctrl+Alt+M，或点上面的『生成回复』。", "empty")
            self.out.configure(state="disabled")
            return
        sections, _ = split_sections(text)
        if not sections:
            self.out.insert("end", text, "body")
            self.out.configure(state="disabled")
            return
        if not clearing:
            self.out.insert("end", "", "body")
        order = ["一句判断", "可直接发送", "为什么", "观察点"]
        for name in order:
            if name not in sections:
                continue
            self.out.insert("end", f"【{name}】\n", "h")
            self.out.insert("end", sections[name] + "\n", "draft" if "发送" in name else "body")
        for name, val in sections.items():
            if name in order:
                continue
            self.out.insert("end", f"【{name}】\n", "h")
            self.out.insert("end", val + "\n", "body")
        self.out.configure(state="disabled")
        self.out.see("1.0")   # 完整渲染后回到顶部，先看判断和可直接发送的那条

    def _set_route_hint(self, refs: list[str]) -> None:
        if not refs:
            self.lbl_route.configure(text="走内核规则")
            return
        short = [Path(r).stem.split("：")[0].split("、")[0][:8] for r in refs[:3]]
        self.lbl_route.configure(text="参考 " + "·".join(short))

    # ---------------------------------------------------------------- 输出
    def _copy(self, text: str) -> bool:
        try:
            import pyperclip
            pyperclip.copy(text)
            return True
        except Exception:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.root.update()
                return True
            except Exception:
                return False

    def _open_more_actions(self):
        """回复建议右上角「⋯」：把低频兜底功能收进来。"""
        menu = tk.Menu(self.root, tearoff=0, font=(FONT, 9), bg=CARD, fg=TEXT,
                       activebackground="#EEF3FF", activeforeground=TEXT,
                       bd=1, relief="solid")
        menu.add_command(label="复制给豆包（完整提示词）", command=self.copy_prompt)
        menu.add_separator()
        menu.add_command(label="打开话术库", command=self.open_library)
        menu.add_command(label="打开数据目录", command=lambda: os.startfile(str(self.store.dir)))
        self._more_menu = menu
        btn = self.btn_more
        try:
            menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
        finally:
            menu.grab_release()

    def copy_draft(self):
        if not self.last_draft:
            self._toast("还没有可复制的话术")
            return
        self._copy(self.last_draft)
        self._toast("已复制，直接 Ctrl+V 就能发")

    def insert_to_window(self):
        if not self.last_draft:
            self._toast("还没有可填入的话术")
            return
        if not self._copy(self.last_draft):
            self._toast("复制失败，手动选中话术复制吧")
            return
        hwnd = self.wechat_hwnd or wb.find_wechat_window(self.target_hwnd)
        if not hwnd or not wb.is_wechat_window(hwnd):
            hwnd = wb.find_wechat_window(0)
        if not hwnd:
            self._toast("没找到微信窗口：先打开微信主界面，再点一次")
            return
        self.wechat_hwnd = hwnd
        self.hide()
        ok, msg = wb.paste_text(self.last_draft, hwnd)
        if ok:
            self.tray.notify("已填入微信输入框，确认后自己按 Enter 发送")
        else:
            self.show()
            self._toast(msg)

    def copy_prompt(self):
        log = self.get_log() or self._recent_log()
        if not log:
            self._toast("先读入聊天记录")
            return
        name = self.current_contact
        pack = self.engine.build_prompt(
            log, DEFAULT_ASK,
            self.contacts.memory_block(name, self.my_names, with_style=False) if name else "",
            "", self.contacts.style_block(name, self.my_names) if name else "")
        full = f"{pack.system}\n\n---\n\n{pack.user}"
        self._copy(full)
        self._toast(f"已复制完整提示词（{len(full)} 字），粘给豆包即可")

    def save_library(self):
        if not self.last_draft:
            self._toast("还没有可保存的话术")
            return
        self.store.add_library(self.current_contact, self.last_draft, self.last_log)
        if self.current_contact:
            self.contacts.add_draft(self.current_contact, self.last_draft, "存库")
        self._toast(f"已存入话术库（共 {len(self.store.library)} 条）")

    # ---------------------------------------------------------------- 状态
    def _refresh_status(self):
        model = self.client.model if self.client.configured else "未配置 Key · 提示词模式"
        parts = [f"● {model}", f"热键 {self.store.config.get('hotkey')}"]
        if self.current_contact:
            n = len(self.contacts.messages(self.current_contact))
            sc = ""
            try:
                e = self.contacts.effective_score(self.current_contact)
                if e.get("total"):
                    sc = f"｜{e.get('grade')}{e.get('total')}" + ("" if e.get("source") == "auto" else "·手动")
            except Exception:
                sc = ""
            parts.append(f"{self.current_contact}（{n} 条{sc}）")
        if not self.my_names:
            parts.append("未设我的昵称")
        if not self.wechat_hwnd:
            self.wechat_hwnd = wb.find_wechat_window(0)
        parts.append("微信已就绪" if self.wechat_hwnd else "微信未打开")
        self.status.configure(text="  ·  ".join(parts))

    def _toast(self, msg: str, ms: int = 4000):
        self.toast.configure(text=msg)
        self.root.after(ms, lambda: self.toast.configure(text="") if self.toast.cget("text") == msg else None)

    # ---------------------------------------------------------------- 记录文件夹导入
    TEXT_EXTS = {".txt", ".log", ".md"}

    @staticmethod
    def _looks_like_path_dump(parsed: dict) -> bool:
        """整段都是文件路径（常见于误把资源管理器路径粘进导入框），不是聊天。"""
        msgs = parsed.get("messages") or []
        if len(msgs) < 2:
            return False
        def is_path(t: str) -> bool:
            t = (t or "").strip()
            return bool(t) and (t[1:3] in (":\\", ":/") or t.startswith("\\\\") or t.startswith("\\") or t.startswith("/"))
        return sum(1 for m in msgs if is_path(m.get("text", ""))) >= max(1, int(len(msgs) * 0.6))

    def _import_cache_path(self) -> Path:
        return self.store.dir / "import_cache.json"

    def _load_import_cache(self) -> dict:
        try:
            raw = json.loads(self._import_cache_path().read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_import_cache(self, cache: dict) -> None:
        try:
            p = self._import_cache_path()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def _file_sig(self, path: Path) -> str:
        st = path.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"

    @staticmethod
    def _clean_stem(stem: str) -> str:
        s = stem or ""
        for junk in ("微信聊天记录", "聊天记录", "微信导出", "微信", "记录"):
            s = s.replace(junk, "")
        return s.strip(" -_·")

    def import_text_file(self, path: Path, me: str = "", target_hint: str = "",
                         quiet: bool = True) -> dict:
        """导入单个导出的文本记录。只读 txt/log/md，不碰 .db 加密库。"""
        path = Path(path)
        preferred = (me or (self.my_names[0] if self.my_names else "")).strip()
        mine = []
        for n in ([preferred] + list(self.my_names)):
            if n and n not in mine:
                mine.append(n)
        if not mine:
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": "未设置『我的微信昵称』，先在设置或导入窗里确认我是谁"}
        try:
            sig = self._file_sig(path)
        except Exception as e:  # noqa: BLE001
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": f"读不到文件信息：{e}"}
        cache = self._load_import_cache()
        rec = cache.get(str(path.resolve()))
        if rec and rec.get("sig") == sig:
            return {"file": path.name, "target": rec.get("target", ""),
                    "added": 0, "skipped": 1, "error": ""}
        try:
            try:
                raw = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raw = path.read_text(encoding="gbk", errors="ignore")
        except Exception as e:  # noqa: BLE001
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": f"读取失败：{e}"}
        if not raw.strip():
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": "文件是空的"}
        if self._looks_like_path_dump(parse_wechat(raw, [])):
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": "这看起来是文件路径，不是聊天内容"}
        parsed = None
        used_me = ""
        for cand in mine:
            trial = parse_wechat(raw, [cand])
            if trial.get("stats", {}).get("me_count"):
                parsed, used_me = trial, cand
                break
        if parsed is None:
            parsed = parse_wechat(raw, mine)
            if not parsed.get("stats", {}).get("me_count"):
                return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                        "error": f"记录里没找到你的昵称（{'、'.join(mine)}），先在身份里加上记录里的名字"}
        if self._looks_like_path_dump(parsed):
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": "这看起来是文件路径，不是聊天内容"}
        st = parsed.get("stats", {})
        target = (parsed.get("target") or "").strip()
        if not target:
            target = self._clean_stem(target_hint or path.stem)
        if not target:
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": "认不出对象，文件名里带上对方昵称试试"}
        name = self.contacts.match(target) or target
        if name == used_me:
            return {"file": path.name, "target": "", "added": 0, "skipped": 0,
                    "error": "对象和『我』同名，检查文件里的昵称"}
        self.contacts.ensure(name)
        aliases = self._aliases_for(name, list((parsed.get("speakers") or {}).keys()))
        if target not in aliases:
            aliases.append(target)
        current = list(self.contacts.get(name).get("aliases") or [])
        if aliases != current:
            self.contacts.update(name, {"aliases": aliases})
        clean, dropped = keep_related(parsed["messages"], name, [used_me] + list(self.my_names), aliases)
        if not clean:
            return {"file": path.name, "target": name, "added": 0, "skipped": 0,
                    "error": "文件里没有这个对象说的话"}
        added = self.contacts.append_messages(name, clean)
        self.contacts.set_stats(name, {"avg_reply_min": st.get("avg_reply_min")})
        if added:
            self.contacts.learn_style(name, self.my_names)
        cache[str(path.resolve())] = {
            "sig": sig, "target": name,
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "added": int(rec.get("added", 0)) + added if rec else added,
        }
        self._save_import_cache(cache)
        return {"file": path.name, "target": name, "added": added,
                "dropped": dropped, "skipped": 0, "error": ""}

    def import_folder(self, folder, quiet: bool = False, limit: int = 60) -> dict:
        """批量导入一个文件夹里的导出文本；自动同步也走这里。"""
        folder = Path(folder)
        if not folder.is_dir():
            result = {"files": 0, "added": 0, "targets": [], "errors": [],
                      "db": 0, "skipped": 0, "error": "文件夹不存在"}
            if not quiet:
                messagebox.showwarning("导入文件夹", result["error"])
            return result
        texts, db_count, total, empty_count = [], 0, 0, 0
        for p in folder.rglob("*"):
            try:
                if not p.is_file():
                    continue
                total += 1
                suf = p.suffix.lower()
                if suf in (".db", ".sqlite", ".db-wal", ".db-shm"):
                    db_count += 1
                    continue
                if suf in self.TEXT_EXTS:
                    if p.stat().st_size == 0:
                        empty_count += 1
                        continue
                    texts.append(p)
            except Exception:
                continue
        texts.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        cache = self._load_import_cache()
        fresh, cached_count = [], 0
        for p in texts:
            try:
                sig = self._file_sig(p)
            except Exception:
                continue
            rec = cache.get(str(p.resolve()))
            if rec and rec.get("sig") == sig:
                cached_count += 1
                continue
            fresh.append(p)
            if len(fresh) >= limit:
                break
        texts = fresh
        added_total, targets, errors, skipped = 0, [], [], cached_count
        for p in texts:
            res = self.import_text_file(p, target_hint=p.stem, quiet=True)
            added_total += int(res.get("added", 0))
            skipped += int(res.get("skipped", 0))
            if res.get("target") and res["target"] not in targets:
                targets.append(res["target"])
            if res.get("error"):
                errors.append(f"{res['file']}：{res['error']}")
        result = {"files": len(texts), "added": added_total, "targets": targets,
                  "errors": errors, "db": db_count, "empty": empty_count,
                  "skipped": skipped, "error": ""}
        if not quiet:
            msg = f"扫描 {total} 个文件，待更新文本 {len(texts)} 个，新入库 {added_total} 条"
            if cached_count:
                msg += f"\n已有 {cached_count} 个文件没变化，跳过"
            if targets:
                msg += "\n对象：" + "、".join(targets[:8]) + ("…" if len(targets) > 8 else "")
            if db_count:
                msg += f"\n另有 {db_count} 个 .db/.sqlite 加密库，未读取（需要你先导出成 txt）"
            if empty_count:
                msg += f"\n空文件 {empty_count} 个，已跳过"
            if errors:
                msg += "\n未导入：\n" + "\n".join(errors[:6])
            messagebox.showinfo("导入文件夹", msg)
        elif added_total:
            self._toast(f"记录文件夹自动同步：新入库 {added_total} 条（{'、'.join(targets[:4])}）")
            try:
                self.tray.notify(f"记录文件夹同步：新增 {added_total} 条")
            except Exception:
                pass
        return result

    def _watch_import_dir(self):
        try:
            d = (self.store.config.get("import_dir") or "").strip()
            if not d and CHAT_DIR.is_dir():
                d = str(CHAT_DIR)
            if self.behavior().get("auto_import_watch", True) and d and Path(d).is_dir():
                self.import_folder(d, quiet=True, limit=30)
        except Exception:
            pass
        self.root.after(5000, self._watch_import_dir)

    # ---------------------------------------------------------------- 导入
    def open_import(self):
        win = tk.Toplevel(self.root)
        self.set_win_icon(win)
        win.title("导入微信聊天")
        win.configure(bg=CARD)
        win.geometry(f"780x620+{max(10, self.root.winfo_x() - 140)}+{self.root.winfo_y() + 20}")
        win.attributes("-topmost", True)

        tk.Label(win, text="把微信聊天粘到左边（多选消息右键复制，或导出的 txt），点『解析』看右边预览。",
                 font=(FONT, 9), fg=SUB, bg=CARD, anchor="w").pack(fill="x", padx=14, pady=(12, 3))
        d0 = (self.store.config.get("import_dir") or "").strip()
        if d0:
            dir_text = f"同步文件夹：{d0}　只读 txt/log，.db 加密库不读"
        elif CHAT_DIR.is_dir():
            dir_text = "同步文件夹：未设置 · 已检测到项目 chat 文件夹，点『用 chat』一键启用"
        else:
            dir_text = "同步文件夹：未设置（点右下角『同步文件夹…』选一个）　只读 txt/log，.db 加密库不读"
        lbl_dir = tk.Label(win, text=dir_text,
                           font=(FONT, 8), fg="#8A9099", bg=CARD, anchor="w")
        lbl_dir.pack(fill="x", padx=14, pady=(0, 6))

        body = tk.Frame(win, bg=CARD)
        body.columnconfigure(0, weight=1, uniform="half")
        body.columnconfigure(1, weight=1, uniform="half")
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=CARD)
        left.grid(row=0, column=0, sticky="nsew")
        txt = tk.Text(left, width=1, wrap="word", font=(FONT, 10), relief="flat", bg="#FBFCFE",
                      highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        txt.pack(fill="both", expand=True)
        right = tk.Frame(body, bg=CARD)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        prev = tk.Text(right, width=1, wrap="word", font=(FONT, 9), relief="flat", bg="#FBFCFE",
                       highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6, state="disabled")
        prev.pack(fill="both", expand=True)

        bar = tk.Frame(win, bg=CARD)
        bar.pack(side="bottom", fill="x", padx=14, pady=8)
        body.pack(fill="both", expand=True, padx=14)

        r1 = tk.Frame(bar, bg=CARD)
        r1.pack(fill="x")
        tk.Label(r1, text="这段记录里，我是", font=(FONT, 9), fg=SUB, bg=CARD).pack(side="left")
        cbo_me = ttk.Combobox(r1, font=(FONT, 9), width=10, state="normal",
                              values=([n for n in self.my_names if n] + ([] if "我" in self.my_names else ["我"])))
        cbo_me.pack(side="left", padx=6)
        if len(self.my_names) == 1:
            cbo_me.set(self.my_names[0])
        self._btn(r1, "身份…", lambda: manage_me(), kind="ghost", small=True).pack(side="left")
        tk.Label(r1, text="对象叫（解析后自动填）", font=(FONT, 9), fg=SUB, bg=CARD).pack(side="left", padx=(10, 0))
        cbo = ttk.Combobox(r1, values=self._contact_choices(), font=(FONT, 9), width=12, state="normal")
        cbo.pack(side="left", padx=6)
        cbo.set("")  # 不再先塞当前对象；以左栏记录解析出的对象名为准
        info = tk.Label(r1, text="", font=(FONT, 9), fg=SUB, bg=CARD, anchor="w")
        info.pack(side="left", padx=8, fill="x", expand=True)

        r2 = tk.Frame(bar, bg=CARD)
        r2.pack(fill="x", pady=(8, 0))
        state = {"raw": "", "target": "", "matched": ""}

        def refresh_me_values(speakers=None):
            vals = [n for n in self.my_names if n]
            if "我" not in vals:
                vals.append("我")
            for n in (speakers or []):
                if n and n not in vals:
                    vals.append(n)
            cbo_me.configure(values=vals)

        def manage_me():
            """我的身份：一人可能有多个微信昵称，这里统一管理。"""
            dlg = tk.Toplevel(win)
            self.set_win_icon(dlg)
            dlg.title("我的身份（微信昵称）")
            dlg.configure(bg=CARD)
            dlg.geometry(f"360x300+{win.winfo_x() + 200}+{win.winfo_y() + 120}")
            dlg.attributes("-topmost", True)
            tk.Label(dlg, text="这些昵称都会被认成『我』：导出的记录里叫哪个都行。",
                     font=(FONT, 9), fg=SUB, bg=CARD, anchor="w", justify="left",
                     wraplength=320).pack(fill="x", padx=12, pady=(12, 6))
            box = tk.Listbox(dlg, font=(FONT, 10), relief="flat", bg="#FBFCFE",
                             highlightthickness=1, highlightbackground=BORDER)
            box.pack(fill="both", expand=True, padx=12)
            for n in self.my_names:
                box.insert("end", n)

            def add_one():
                v = entry.get().strip()
                if not v or v in self.my_names:
                    return
                self.my_names = clean_names(list(self.my_names) + [v])
                box.insert("end", v)
                entry.delete(0, "end")
                refresh_me_values()

            def del_one():
                sel = box.curselection()
                if not sel:
                    return
                v = box.get(sel[0])
                self.my_names = [n for n in self.my_names if n != v]
                box.delete(sel[0])

            rowm = tk.Frame(dlg, bg=CARD)
            rowm.pack(fill="x", padx=12, pady=6)
            entry = tk.Entry(rowm, font=(FONT, 10), relief="flat", bg="#FBFCFE", fg=TEXT,
                             highlightthickness=1, highlightbackground=BORDER)
            entry.pack(side="left", fill="x", expand=True, ipady=4)
            self._btn(rowm, "添加", add_one, kind="ghost", small=True).pack(side="left", padx=(6, 0))
            self._btn(rowm, "删除选中", del_one, kind="ghost", small=True).pack(side="left", padx=(6, 0))

            def done():
                cfg = dict(self.store.config)
                cfg["my_names"] = clean_names(list(self.my_names))
                self.store.apply_config(cfg)
                self.my_names = cfg["my_names"]
                refresh_me_values()
                info.configure(text="身份已更新：" + ("、".join(self.my_names) or "（空）"))
                dlg.destroy()

            brow = tk.Frame(dlg, bg=CARD)
            brow.pack(fill="x", padx=12, pady=(0, 12))
            self._btn(brow, "保存", done, kind="primary").pack(side="right")
            self._btn(brow, "取消", dlg.destroy, kind="ghost").pack(side="right", padx=(0, 6))

        def show_parse():
            raw = txt.get("1.0", "end").strip()
            state["raw"] = raw
            if not raw:
                state["target"] = ""
                state["matched"] = ""
                cbo.configure(values=self._contact_choices())
                cbo.set("")
                prev.configure(state="normal")
                prev.delete("1.0", "end")
                prev.insert("1.0", "左栏没有内容：先粘贴聊天记录，或点『选择文件…』/『用 chat』载入。")
                prev.configure(state="disabled")
                info.configure(text="左边还没有内容")
                dnow = (self.store.config.get("import_dir") or "").strip()
                if dnow:
                    lbl_dir.configure(text=f"同步文件夹：{dnow}　左栏已清空，点『用 chat』可重新载入")
                return
            base = parse_wechat(raw, [])
            speakers = list((base.get("speakers") or {}).keys())
            refresh_me_values(speakers)
            auto_me = ""
            if cbo_me.get().strip() not in speakers:
                hit = next((n for n in self.my_names if n in speakers), "")
                if not hit and "我" in speakers:
                    hit = "我"
                if hit:
                    cbo_me.set(hit)
                    auto_me = hit
            me = cbo_me.get().strip()
            parsed = parse_wechat(raw, [me]) if me else base
            target = (parsed.get("target") or "").strip()
            state["target"] = target
            st = parsed.get("stats", {})
            lines = [
                f"总 {st.get('total', 0)} 条｜对方 {st.get('them_count', 0)} / 我 {st.get('me_count', 0)}",
                f"字数：对方 {st.get('them_chars', 0)} / 我 {st.get('me_chars', 0)}"
                + (f"｜我平均回她 {st.get('avg_reply_min')} 分钟" if st.get("avg_reply_min") else ""),
                f"识别到的说话人：{'、'.join(speakers) or '（无）'}",
                f"推断对象：{target or '（未识别）'}",
                "",
            ]
            for m in parsed["messages"][:24]:
                tag = "[我]" if m.get("role") == "me" else f"[对方·{m.get('speaker') or '未知'}]"
                tm = (m.get("ts") or "")[-5:]
                lines.append(f"{tm} {tag} {(m.get('text') or '')[:34]}")
            if len(parsed["messages"]) > 24:
                lines.append(f"…共 {len(parsed['messages'])} 条")

            obj_msg = ""
            if target:
                matched = self.contacts.match(target)
                state["matched"] = matched or ""
                cbo.configure(values=self._contact_choices([target]))
                if matched:
                    cbo.set(matched)
                    aliases = self.contacts.get(matched).get("aliases") or []
                    obj_msg = f"✅ 对象已在管理里：「{matched}」" + (
                        f"（别名 {('、'.join(aliases))}）" if aliases else "")
                else:
                    cbo.set(target)
                    obj_msg = f"＋ 新对象「{target}」：保存时自动入库"
                lines.append("")
                lines.append(obj_msg)
                if auto_me:
                    lines.append(f"已自动认定你是「{auto_me}」")

            prev.configure(state="normal")
            prev.delete("1.0", "end")
            prev.insert("1.0", "\n".join(lines))
            prev.configure(state="disabled")

            if me and target:
                info.configure(text=obj_msg)
            elif me:
                info.configure(text="解析完成，记得给对象填个代号")
            else:
                info.configure(text="⚠ 先选/填『我是』哪一位，否则分不清谁说的")

        def pick_file():
            path = filedialog.askopenfilename(filetypes=[("文本文件", "*.txt *.log *.md"), ("所有文件", "*.*")])
            if not path:
                return
            try:
                path_obj = Path(path)
                try:
                    raw = path_obj.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    raw = path_obj.read_text(encoding="gbk", errors="ignore")
            except Exception as e:
                info.configure(text=f"读取失败：{e}")
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", raw)
            show_parse()

        def apply_dir(path: str):
            cfg = dict(self.store.config)
            cfg["import_dir"] = path
            beh = dict(self.behavior())
            beh["auto_import_watch"] = True
            cfg["behavior"] = beh
            self.store.apply_config(cfg)
            lbl_dir.configure(text=f"同步文件夹：{path}　每 5 秒增量读取 txt/log")
            self.import_folder(path, quiet=False)
            load_folder_preview(path, prefer=self.current_contact)

        def file_matches_prefer(p: Path, prefer: str) -> bool:
            if not prefer:
                return True
            aliases = [prefer] + list(self.contacts.get(prefer).get("aliases") or [])
            stem = p.stem.lower()
            if any(a and a.lower() in stem for a in aliases):
                return True
            try:
                try:
                    raw = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    raw = p.read_text(encoding="gbk", errors="ignore")
                parsed = parse_wechat(raw, self.my_names)
                target = (parsed.get("target") or "").strip()
                return bool(target) and (target == prefer or self.contacts.match(target) == prefer)
            except Exception:
                return False

        def load_folder_preview(path: str, prefer: str = "", auto: bool = False):
            """把文件夹里与当前对象匹配的文本载入左栏，方便立刻核对/解析。"""
            try:
                folder = Path(path)
                files, empty_files = [], []
                for p in folder.rglob("*"):
                    try:
                        if p.is_file() and p.suffix.lower() in self.TEXT_EXTS:
                            size = p.stat().st_size
                            if size == 0:
                                empty_files.append(p)
                            elif size <= 2_000_000:
                                files.append(p)
                    except Exception:
                        continue

                def show_existing(reason: str):
                    """没有可用文件时，载入当前对象已有的记录，避免左栏空白。"""
                    msgs = self.contacts.messages(prefer) if prefer else []
                    if not msgs:
                        info.configure(text=f"{reason}；当前对象也没有已有记录，左栏可直接粘贴新聊天")
                        return
                    txt.delete("1.0", "end")
                    txt.insert("1.0", to_annotated(msgs, max_chars=20000))
                    show_parse()
                    lbl_dir.configure(text=f"同步文件夹：{path}　{reason}；已载入「{prefer}」已有 {len(msgs)} 条记录")
                    info.configure(text=f"{reason}；左栏是当前对象已有记录，可直接查看/核对")

                if not files:
                    if prefer:
                        show_existing("chat 里没有可用 txt（空文件或加密库不算）")
                    elif not auto:
                        info.configure(text="文件夹里没有可预览的 txt/log（空文件或加密库不算）")
                    return
                prefer = (prefer or "").strip()
                if prefer:
                    matched = [p for p in files if file_matches_prefer(p, prefer)]
                    if not matched:
                        aliases = [prefer] + list(self.contacts.get(prefer).get("aliases") or [])
                        empty_hit = next((p for p in empty_files
                                          if any(a and a.lower() in p.stem.lower() for a in aliases)), None)
                        reason = (f"chat 里的「{empty_hit.name}」是 0 字节，导出没写进内容"
                                  if empty_hit else f"chat 里没有「{prefer}」对应的 txt")
                        show_existing(reason)
                        return
                    files = matched
                pick = max(files, key=lambda p: p.stat().st_size)
                try:
                    raw = pick.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    raw = pick.read_text(encoding="gbk", errors="ignore")
                txt.delete("1.0", "end")
                txt.insert("1.0", raw)
                show_parse()
                lbl_dir.configure(text=f"同步文件夹：{path}　预览：{pick.name}（可直接点『解析』）")
            except Exception as e:  # noqa: BLE001
                if not auto:
                    info.configure(text=f"预览载入失败：{e}")

        def sync_folder():
            path = filedialog.askdirectory(title="选择微信导出记录文件夹（只放 txt/log）")
            if path:
                apply_dir(path)

        def use_chat():
            if CHAT_DIR.is_dir():
                apply_dir(str(CHAT_DIR))

        def save():
            raw = txt.get("1.0", "end").strip()
            me = cbo_me.get().strip()
            if not raw:
                info.configure(text="先粘贴聊天记录")
                return
            if not me:
                info.configure(text="先在『我是』里选/填你自己，不然分不清谁说的")
                return
            parsed = parse_wechat(raw, [me])
            if self._looks_like_path_dump(parsed):
                info.configure(text="这看起来是文件路径列表，不是聊天内容；请粘贴消息正文或选导出的 txt")
                return
            target = (parsed.get("target") or state.get("target") or "").strip()
            typed = (cbo.get() or "").strip()
            name = self.contacts.match(typed) if typed else ""
            if not name:
                name = typed or target
            if not name:
                info.configure(text="给对象填一个代号（和聊天里显示的名字一致最好）")
                return
            if name == me:
                info.configure(text="『我』和对象不能是同一个昵称，检查一下")
                return
            is_new = name not in self.contacts.names()
            al_now = self._aliases_for(name, list((parsed.get("speakers") or {}).keys()))
            target_match = self.contacts.match(target) if target else ""
            if target and (not target_match or target_match == name) and target not in al_now:
                al_now.append(target)
            mine_names = [me] + [n for n in self.my_names if n != me]
            clean_msgs, dropped = keep_related(parsed["messages"], name, mine_names, al_now)
            has_them = any(m.get("role") == "them" for m in clean_msgs)
            if not clean_msgs or not has_them:
                guessed = self.contacts.match(target) or target or "未识别"
                msg = (f"这段记录里没有「{name}」说的话。\n"
                       f"解析到的对象是「{guessed}」，请把『对象叫』改成它再保存。")
                info.configure(text=f"⚠ {msg.replace(chr(10), ' ')}")
                messagebox.showwarning("对象不匹配", msg, parent=win)
                return
            self.contacts.ensure(name)
            if al_now != list(self.contacts.get(name).get("aliases") or []):
                self.contacts.update(name, {"aliases": al_now})
            added = self.contacts.append_messages(name, clean_msgs)
            self.contacts.set_stats(name, {"avg_reply_min": parsed.get("stats", {}).get("avg_reply_min")})
            self.contacts.auto_score(name)
            st = self.contacts.learn_style(name, [me])
            score = self.contacts.effective_score(name)
            if me not in self.my_names:    # 记住"我"，以后从剪贴板复制就能自动分角色
                names = clean_names(list(self.my_names) + [me])
                cfg = dict(self.store.config)
                cfg["my_names"] = names
                self.store.apply_config(cfg)
                self.my_names = names
            self._set_contact(name)
            win.destroy()
            self._refresh_status()
            md = Path(st.get("md_path") or self.contacts.style_md_path(name))
            prefix = "新建对象「%s」并导入" % name if is_new else "导入"
            tail = f"，另剔掉 {dropped} 条其他人的消息" if dropped else ""
            if added:
                self._toast(f"{prefix} {added} 条到「{name}」，评分 {score.get('total')}（{score.get('grade')}）"
                            f"，已学风格并生成 {md.name}{tail}")
            else:
                self._toast(f"「{name}」这段没有新增（当前 {len(self.contacts.messages(name))} 条，可能已导入过）{tail}")

        cbo_me.bind("<<ComboboxSelected>>", lambda e: show_parse())
        cbo.bind("<<ComboboxSelected>>", lambda e: show_parse())
        self._btn(r2, "保存导入", save, kind="primary").pack(side="right")
        self._btn(r2, "解析", show_parse, kind="ghost").pack(side="right", padx=(0, 6))
        self._btn(r2, "选择文件…", pick_file, kind="ghost").pack(side="right", padx=(0, 6))
        self._btn(r2, "同步文件夹…", sync_folder, kind="ghost").pack(side="right", padx=(0, 6))
        if CHAT_DIR.is_dir():
            self._btn(r2, "用 chat", use_chat, kind="ghost").pack(side="right", padx=(0, 6))
        self._btn(r2, "取消", win.destroy, kind="ghost").pack(side="left")
        if d0 and Path(d0).is_dir():
            def auto_load():
                if txt.get("1.0", "end").strip():
                    return
                load_folder_preview(d0, prefer=self.current_contact, auto=True)
            self.root.after(120, auto_load)

    # ---------------------------------------------------------------- 对象管理
    def open_contacts(self):
        SELF_ITEM = "★ 我（自己）"
        win = tk.Toplevel(self.root)
        self.set_win_icon(win)
        win.title("对象管理")
        win.configure(bg=CARD)
        win.geometry(f"820x720+{max(10, self.root.winfo_x() - 160)}+{max(10, self.root.winfo_y() + 6)}")
        win.attributes("-topmost", True)

        win.rowconfigure(0, weight=1)
        win.columnconfigure(1, weight=1)

        left = tk.Frame(win, bg=CARD, width=170)
        left.grid(row=0, column=0, sticky="ns", padx=(12, 0), pady=12)
        lbar = tk.Frame(left, bg=CARD)
        lbar.pack(side="bottom", fill="x", pady=(6, 0))
        lb = tk.Listbox(left, font=(FONT, 10), relief="flat", bg="#FBFCFE",
                        highlightthickness=1, highlightbackground=BORDER, activestyle="none")
        lb.pack(fill="both", expand=True)

        right = tk.Frame(win, bg=CARD)
        right.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)

        tab1 = tk.Frame(nb, bg=CARD)
        nb.add(tab1, text="  档案  ")
        f = {}
        for key, label in [("name", "代号（对应聊天里的昵称）"), ("aliases", "别名（逗号分隔，用于自动匹配）"),
                           ("mbti", "MBTI"), ("score", "主观评分 0–100（留空 = 用下面的自动分）"),
                           ("relation", "当前关系")]:
            tk.Label(tab1, text=label, font=(FONT, 9), fg=SUB, bg=CARD, anchor="w").pack(fill="x", pady=(8, 2))
            e = tk.Entry(tab1, font=(FONT, 10), relief="flat", bg="#FBFCFE", fg=TEXT,
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
            e.pack(fill="x", ipady=4)
            f[key] = e
        tk.Label(tab1, text="背景 / 三件关键事", font=(FONT, 9), fg=SUB, bg=CARD, anchor="w").pack(fill="x", pady=(8, 2))
        note = tk.Text(tab1, height=3, wrap="word", font=(FONT, 10), relief="flat", bg="#FBFCFE",
                       highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        note.pack(fill="x")

        # ---- 主干评分：自动分 + 六维明细，手填分优先 ----
        srow = tk.Frame(tab1, bg=CARD)
        srow.pack(fill="x", pady=(8, 0))
        lbl_auto = tk.Label(srow, text="自动评分：—", font=(FONT, 10, "bold"), fg=ACCENT, bg=CARD, anchor="w")
        lbl_auto.pack(side="left")
        lbl_dims = tk.Label(tab1, text="", font=(FONT, 8), fg=SUB, bg=CARD, anchor="w",
                            justify="left", wraplength=760)
        lbl_dims.pack(fill="x", pady=(2, 0))
        lbl_advice = tk.Label(tab1, text="", font=(FONT, 8), fg="#8A9099", bg=CARD, anchor="w",
                              justify="left", wraplength=760)
        lbl_advice.pack(fill="x")

        def paint_score(name: str):
            if not name:
                lbl_auto.configure(text="自动评分：—")
                lbl_dims.configure(text="")
                lbl_advice.configure(text="")
                return
            if name == SELF_ITEM:
                lbl_auto.configure(text="本人档案：不参与对象评分")
                lbl_dims.configure(text=f"已汇总 {len(self.contacts.all_my_messages())} 条我方消息")
                lbl_advice.configure(text="这里看的是你自己的说话风格；对象评分在各自对象里。")
                return
            auto = self.contacts.auto_score(name)
            man = str(self.contacts.get(name).get("score") or "").strip()
            if auto.get("total"):
                txt = f"自动评分：{auto['total']} · {auto['grade']}"
                if man:
                    txt += f"　｜　手填：{man}（以手填为准）"
                lbl_auto.configure(text=txt)
                lbl_dims.configure(text="｜".join(f"{d['name']} {d['v']}" for d in auto["dims"]))
                lbl_advice.configure(text="建议：" + auto.get("advice", ""))
            else:
                lbl_auto.configure(text="自动评分：样本不足")
                lbl_dims.configure(text="")
                lbl_advice.configure(text=auto.get("advice", ""))

        sbtn = tk.Frame(tab1, bg=CARD)
        sbtn.pack(fill="x", pady=(4, 0))
        self._btn(sbtn, "重新打分", lambda: (paint_score(current_name()), self._refresh_status()),
                  kind="ghost", small=True).pack(side="left")

        def use_auto():
            f["score"].delete(0, "end")
            paint_score(current_name())
            self._toast("已清空手填分，改用自动分（记得点保存）")

        self._btn(sbtn, "用自动分", use_auto, kind="ghost", small=True).pack(side="left", padx=(6, 0))

        tab2 = tk.Frame(nb, bg=CARD)
        nb.add(tab2, text="  记忆  ")
        sum_head = tk.Frame(tab2, bg=CARD)
        sum_head.pack(fill="x", pady=(10, 2))
        tk.Label(sum_head, text="现状摘要（自动更新；手改后锁定）", font=(FONT, 9), fg=SUB,
                 bg=CARD, anchor="w").pack(side="left")

        def back_to_auto():
            name = current_name()
            if not name:
                return
            txt_auto = self.contacts.unlock_summary(name)
            summ.delete("1.0", "end")
            summ.insert("1.0", txt_auto)
            self._toast("已恢复自动摘要，之后每收一次新聊天都会自动更新")

        self._btn(sum_head, "恢复自动摘要", back_to_auto, kind="ghost", small=True).pack(side="right")
        summ = tk.Text(tab2, height=2, wrap="word", font=(FONT, 10), relief="flat", bg="#FBFCFE",
                       highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        summ.pack(fill="x")
        tk.Label(tab2, text="关键事件", font=(FONT, 9), fg=SUB, bg=CARD, anchor="w").pack(fill="x", pady=(10, 2))
        ev = tk.Listbox(tab2, font=(FONT, 9), height=3, relief="flat", bg="#FBFCFE",
                        highlightthickness=1, highlightbackground=BORDER)
        ev.pack(fill="x")
        row = tk.Frame(tab2, bg=CARD)
        row.pack(fill="x", pady=6)
        ent = tk.Entry(row, font=(FONT, 9), relief="flat", bg="#FBFCFE", fg=TEXT,
                       highlightthickness=1, highlightbackground=BORDER)
        ent.pack(side="left", fill="x", expand=True, ipady=4)
        tk.Label(tab2, text="已发过的话术", font=(FONT, 9), fg=SUB, bg=CARD, anchor="w").pack(fill="x", pady=(8, 2))
        dr = tk.Listbox(tab2, font=(FONT, 9), height=2, relief="flat", bg="#FBFCFE",
                        highlightthickness=1, highlightbackground=BORDER)
        dr.pack(fill="x")

        md_row = tk.Frame(tab2, bg=CARD)
        md_row.pack(side="bottom", fill="x", pady=(6, 0))
        md_hint = tk.Label(md_row, text="", font=(FONT, 8), fg="#8A9099", bg=CARD, anchor="w",
                           justify="left", wraplength=740)
        md_hint.pack(side="left", fill="x", expand=True)

        tab3 = tk.Frame(nb, bg=CARD)
        nb.add(tab3, text="  风格  ")
        sty = tk.Text(tab3, wrap="word", font=(FONT, 9), relief="flat", bg="#FBFCFE",
                      highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        sty.pack(fill="both", expand=True, pady=(10, 6))

        def current_name():
            sel = lb.curselection()
            return lb.get(sel[0]) if sel else ""

        def load(name):
            if name == SELF_ITEM:
                for key, e in f.items():
                    e.delete(0, "end")
                f["name"].insert(0, "我（自己）")
                f["aliases"].insert(0, "、".join(self.my_names))
                f["relation"].insert(0, "本人")
                note.delete("1.0", "end")
                note.insert("1.0", "这里不填对象档案；风格由所有聊天里『我』的发言汇总。")
                summ.delete("1.0", "end")
                alln = len(self.contacts.all_my_messages())
                summ.insert("1.0", f"已汇总 {alln} 条我方消息，覆盖 {len(self.contacts.names())} 个对象；"
                                    "导入任意对象聊天后会自动累积。")
                ev.delete(0, "end")
                dr.delete(0, "end")
                st = self.contacts.learn_self_style(self.my_names, write_md=True)
                md = self.contacts.self_style_path()
                md_hint.configure(text=f"风格文件：{md.name}" + ("" if md.exists() else "（还没生成，点『重新学习风格』）")
                                  + f"　→　{md.parent}")
                block = self.contacts.self_style_block(self.my_names)
                sty.delete("1.0", "end")
                sty.insert("1.0", f"我的风格汇总｜{alln} 条发言\n\n"
                                  + block
                                  + f"\n\n风格文件：{md}")
                nb.select(tab3)
                paint_score(name)
                return
            rec = self.contacts.get(name)
            for key, e in f.items():
                e.delete(0, "end")
                val = "，".join(rec.get("aliases", [])) if key == "aliases" else rec.get(key, "")
                e.insert(0, val or "")
            note.delete("1.0", "end")
            note.insert("1.0", rec.get("note", ""))
            summ.delete("1.0", "end")
            summ.insert("1.0", self.contacts.summary(name))
            ev.delete(0, "end")
            for it in self.contacts.events(name):
                tag = "[自动] " if it.get("source") == "auto" else ""
                ev.insert("end", f"{it['ts']}  {tag}{it['text']}")
            dr.delete(0, "end")
            for it in self.contacts.drafts(name, 20)[::-1]:
                dr.insert("end", f"{it['ts']}  {it['draft'][:40]}")
            sty.delete("1.0", "end")
            st = self.contacts.get(name).get("stats", {})
            block = self.contacts.style_block(name, self.my_names)
            md = self.contacts.style_md_path(name)
            md_hint.configure(text=f"风格文件：{md.name}" + ("" if md.exists() else "（还没生成，点右边『重新学习风格』）")
                              + f"　→　{md.parent}")
            tail = f"\n\n风格文件：{md}" + ("" if md.exists() else "（还没生成，点『重新学习风格』）")
            sty.insert("1.0", f"消息 {st.get('msg_count', 0)} 条｜上次导入 {st.get('last_import', '—')}\n\n"
                              + (block or "还没有历史记录。导入聊天后点『重新学习风格』。") + tail)
            paint_score(name)

        def refresh_list(select=""):
            lb.delete(0, "end")
            lb.insert("end", SELF_ITEM)
            for n in self.contacts.names():
                lb.insert("end", n)
            names = list(lb.get(0, "end"))
            if select in names:
                lb.selection_set(names.index(select))
                lb.see(names.index(select))

        def on_select(_=None):
            name = current_name()
            if name:
                load(name)

        def save_all():
            name = current_name()
            if not name:
                return
            if name == SELF_ITEM:
                self.contacts.learn_self_style(self.my_names, write_md=True)
                load(name)
                self._toast("已重新统计你的风格；本人档案不参与对象评分")
                return
            aliases = [x.strip() for x in f["aliases"].get().replace(",", "，").split("，") if x.strip()]
            raw_score = f["score"].get().strip()
            if raw_score:
                try:
                    v = float(raw_score)
                    if not 0 <= v <= 100:
                        raise ValueError
                    raw_score = str(int(v))
                except ValueError:
                    self._toast("主观评分要填 0–100 的数字，留空则用自动分")
                    return
            self.contacts.update(name, {
                "aliases": aliases,
                "mbti": f["mbti"].get().strip().upper(),
                "score": raw_score,
                "relation": f["relation"].get().strip(),
                "note": note.get("1.0", "end").strip(),
            })
            self.contacts.set_summary(name, summ.get("1.0", "end").strip())
            self.contacts.auto_score(name)
            f["score"].delete(0, "end")
            f["score"].insert(0, raw_score)
            paint_score(name)
            self._refresh_status()
            eff = self.contacts.effective_score(name)
            self._toast(f"「{name}」已保存，评分 {eff.get('total')}（{eff.get('grade')}"
                        + ("·手动" if eff.get("source") == "manual" else "·自动") + "）")

        def add_event():
            name = current_name()
            text = ent.get().strip()
            if name == SELF_ITEM:
                self._toast("『我（自己）』只看风格，不记对象事件；事件请在具体对象里加")
                return
            if not name or not text:
                return
            self.contacts.add_event(name, text)
            ent.delete(0, "end")
            load(name)

        def del_event():
            name = current_name()
            sel = ev.curselection()
            if name == SELF_ITEM:
                return
            if name and sel:
                self.contacts.remove_event(name, sel[0])
                load(name)

        def learn():
            name = current_name()
            if name == SELF_ITEM:
                st = self.contacts.learn_self_style(self.my_names, write_md=True)
                load(name)
                md = st.get("md_path")
                self._toast(f"已汇总你的说话风格：{Path(md).name}" if md else "还没有你的发言样本")
                return
            if name:
                st = self.contacts.learn_style(name, self.my_names)
                load(name)
                md = st.get("md_path")
                self._toast(f"已重新学习，风格文件已更新：{Path(md).name}" if md else "已按真实聊天重新学习说话风格")

        def open_style():
            name = current_name()
            if not name:
                return
            if name == SELF_ITEM:
                p = self.contacts.self_style_path()
                if not p.exists():
                    self.contacts.learn_self_style(self.my_names, write_md=True)
                try:
                    os.startfile(str(p))
                except Exception as e:  # noqa: BLE001
                    self._toast(f"打不开：{e}")
                return
            p = self.contacts.style_md_path(name)
            if not p.exists():
                st = self.contacts.learn_style(name, self.my_names)
                p = Path(st.get("md_path") or p)
            try:
                os.startfile(str(p))
            except Exception as e:  # noqa: BLE001
                self._toast(f"打不开：{e}")

        def new_one():
            from tkinter import simpledialog
            name = simpledialog.askstring("新建对象", "给这个对象起个代号（最好和聊天里显示的名字一致）：", parent=win)
            if not name:
                return
            self.contacts.ensure(name.strip())
            refresh_list(name.strip())
            on_select()
            self._refresh_contacts()

        def remove_one():
            name = current_name()
            if name == SELF_ITEM:
                self._toast("『我（自己）』是汇总档案，不能删除")
                return
            if name and messagebox.askyesno("删除对象", f"删除「{name}」的档案？历史消息文件仍保留在 data/contacts 下。"):
                self.contacts.delete(name)
                refresh_list()
                self._refresh_contacts()

        row2 = tk.Frame(tab2, bg=CARD)
        row2.pack(side="bottom", fill="x", pady=(10, 0))
        self._btn(row2, "添加事件", add_event, kind="primary", small=True).pack(side="left")
        self._btn(row2, "删除选中", del_event, kind="ghost", small=True).pack(side="left", padx=6)
        self._btn(row2, "重新学习风格", learn, kind="ghost", small=True).pack(side="right")
        self._btn(md_row, "打开文件", open_style, kind="ghost", small=True).pack(side="right", padx=(6, 0))

        lb.bind("<<ListboxSelect>>", on_select)

        self._btn(lbar, "新建", new_one, kind="ghost", small=True).pack(side="left")
        self._btn(lbar, "删除", remove_one, kind="ghost", small=True).pack(side="left", padx=4)

        def set_current_obj():
            name = current_name()
            if name == SELF_ITEM:
                self._toast("『我（自己）』是本人风格档案，不能当作聊天对象")
                return
            if name:
                self._set_contact(name)
                win.destroy()

        bbar = tk.Frame(win, bg=CARD)
        bbar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        self._btn(bbar, "保存", save_all, kind="primary").pack(side="right")
        self._btn(bbar, "关闭", win.destroy, kind="ghost").pack(side="left")
        self._btn(bbar, "设为当前对象", set_current_obj,
                  kind="ghost").pack(side="left", padx=(8, 0))

        refresh_list(self.current_contact)
        names = list(lb.get(0, "end"))
        pick = self.current_contact if self.current_contact in names else SELF_ITEM
        if pick in names:
            lb.selection_set(names.index(pick))
            on_select()

    # ---------------------------------------------------------------- 设置 / 话术库
    def open_settings(self):
        win = tk.Toplevel(self.root)
        self.set_win_icon(win)
        win.title(f"设置 · v{VERSION}")
        win.configure(bg=CARD)
        scr_w = ctypes.windll.user32.GetSystemMetrics(0)
        scr_h = ctypes.windll.user32.GetSystemMetrics(1)
        w = 660
        h = min(760, max(560, scr_h - 150))
        x = max(20, min(scr_w - w - 24, self.root.winfo_x() + 24))
        y = max(20, (scr_h - h - 40) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.minsize(500, 560)

        def _fit_on_screen():
            if not win.winfo_exists():
                return
            ww, hh = win.winfo_width(), win.winfo_height()
            px, py = win.winfo_x(), win.winfo_y()
            if py + hh > scr_h - 42:
                py = max(12, scr_h - hh - 70)
            if px + ww > scr_w - 12:
                px = max(12, scr_w - ww - 20)
            win.geometry(f"+{px}+{py}")

        win.after(90, _fit_on_screen)
        win.attributes("-topmost", True)
        cfg = self.store.config
        api = cfg["api"]
        ui_cfg = dict(cfg.get("ui") or {})
        font_state = {"scale": float(ui_cfg.get("font_scale", 1.0))}
        fields: dict[str, tk.Entry] = {}

        win.grid_rowconfigure(0, weight=1)
        win.grid_columnconfigure(0, weight=1)
        shell = tk.Frame(win, bg=CARD)
        shell.grid(row=0, column=0, sticky="nsew")
        canvas = tk.Canvas(shell, bg=CARD, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", padx=(0, 4))
        canvas.pack(side="left", fill="both", expand=True)

        outer = tk.Frame(canvas, bg=CARD)
        win_id = canvas.create_window((0, 0), window=outer, anchor="nw")
        content = tk.Frame(outer, bg=CARD)
        content.pack(fill="x", padx=18, pady=(12, 0))

        def _sync_scroll(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win_id, width=canvas.winfo_width())

        outer.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))
        win.after(80, lambda: canvas.yview_moveto(0))

        def _wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        win.bind_all("<MouseWheel>", _wheel)
        win.bind("<Destroy>", lambda e: win.unbind_all("<MouseWheel>") if e.widget is win else None)

        def section(title: str, top: int = 12):
            tk.Frame(content, bg="#EDEFF2", height=1).pack(fill="x", pady=(top, 7))
            tk.Label(content, text=title, font=(FONT, 10, "bold"), fg=TEXT, bg=CARD, anchor="w").pack(fill="x")

        def hint(text: str, top: int = 4):
            lbl = tk.Label(content, text=text, font=(FONT, 8), fg="#8A9099", bg=CARD,
                           anchor="w", justify="left", wraplength=590)
            lbl.pack(fill="x", pady=(top, 0))

            def _fit(e, w=lbl):
                if e.width > 40:
                    w.configure(wraplength=e.width - 8)

            lbl.bind("<Configure>", _fit)

        section("接口", top=0)
        form = tk.Frame(content, bg=CARD)
        form.pack(fill="x", pady=(6, 0))
        form.columnconfigure(0, weight=1, uniform="f")
        form.columnconfigure(1, weight=1, uniform="f")

        def add_field(key: str, label: str, row: int, col: int = 0, span: int = 1,
                      value: str = "", secret: bool = False) -> None:
            cell = tk.Frame(form, bg=CARD)
            cell.grid(row=row, column=col, columnspan=span, sticky="ew",
                      padx=(0 if col == 0 or span == 2 else 10, 0), pady=(0, 4))
            tk.Label(cell, text=label, font=(FONT, 9), fg=SUB, bg=CARD, anchor="w").pack(fill="x")
            e = tk.Entry(cell, font=(FONT, 10), relief="flat", bg="#FBFCFE", fg=TEXT,
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                         insertbackground=TEXT)
            e.pack(fill="x", ipady=2)
            e.insert(0, value)
            if secret:
                e.configure(show="•")
            fields[key] = e

        add_field("base_url", "接口地址", 0, span=2, value=api.get("base_url", ""))
        add_field("api_key", "API Key", 1, span=2, value=api.get("api_key", ""), secret=True)
        add_field("model", "模型", 2, value=api.get("model", ""))
        add_field("temperature", "温度", 2, col=1, value=str(api.get("temperature", 0.7)))
        add_field("vision_model", "视觉模型（可选）", 3, span=2, value=api.get("vision_model", ""))

        cell = tk.Frame(form, bg=CARD)
        cell.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        head = tk.Frame(cell, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="技能目录（SKILL.md 所在文件夹）", font=(FONT, 9), fg=SUB, bg=CARD, anchor="w").pack(side="left")
        self._btn(head, "检测原始技能", lambda: detect_skill(), kind="ghost", small=True).pack(side="right")
        row = tk.Frame(cell, bg=CARD)
        row.pack(fill="x", pady=(2, 0))
        e = tk.Entry(row, font=(FONT, 9), relief="flat", bg="#FBFCFE", fg=TEXT,
                     highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                     insertbackground=TEXT)
        e.pack(side="left", fill="x", expand=True, ipady=4)
        e.insert(0, cfg.get("skill_root", ""))
        fields["skill_root"] = e
        self._btn(row, "浏览…", lambda: browse_skill(), kind="ghost", small=True).pack(side="left", padx=(6, 0))
        hint(f"留空用安装包自带的 skill/goutoujunshi；当前已接入 {self.engine.bundle.root.name}（{len(self.engine.bundle.routes)} 条知识路由）")

        section("使用")
        form2 = tk.Frame(content, bg=CARD)
        form2.pack(fill="x", pady=(6, 0))
        form2.columnconfigure(0, weight=2, uniform="g")
        form2.columnconfigure(1, weight=3, uniform="g")
        form = form2   # 复用 add_field 的网格
        add_field("hotkey", "全局热键", 0, value=cfg.get("hotkey", "ctrl+alt+m"))
        add_field("my_names", "我的微信昵称", 0, col=1, value="，".join(cfg.get("my_names", [])))
        add_field("extra_style", "长期语气要求（可留空）", 1, span=2, value=cfg.get("extra_style", ""))
        add_field("context_n", "生成时取最近几条", 2, value=str(cfg.get("context_n", 16)))

        def preview_font(i: int):
            scale = FONT_SCALES[max(0, min(len(FONT_SCALES) - 1, i))]
            font_state["scale"] = scale
            self._apply_content_font_scale(scale)

        look = tk.Frame(form2, bg=CARD)
        look.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=(0, 3))
        tk.Label(look, text="字体大小", font=(FONT, 9, "bold"), fg=TEXT, bg=CARD,
                 anchor="w").pack(side="left", padx=(0, 8))
        start_idx = min(range(len(FONT_SCALES)),
                        key=lambda i: abs(FONT_SCALES[i] - font_state["scale"]))
        font_slider = StepSlider(look, FONT_SCALES, index=start_idx, command=preview_font)
        font_slider.pack(side="left", fill="x", expand=True)
        hint("昵称用逗号分隔；热键被占用就换一个；字体档位预览全局字号，保存后记住。")

        section("行为")
        b = self.behavior()
        chk: dict[str, tk.BooleanVar] = {}
        box = tk.Frame(content, bg=CARD)
        box.pack(fill="x", pady=(4, 0))
        for c in range(3):
            box.columnconfigure(c, weight=1, uniform="h")
        labels = [
            ("tray", "托盘常驻"),
            ("close_to_tray", "关闭收进托盘"),
            ("autostart", "开机自启"),
            ("auto_copy", "生成后自动复制"),
            ("hotkey_read_clip", "热键读剪贴板"),
            ("auto_judge", "自动判定效果"),
            ("auto_rescue", "反效果自动补救"),
            ("auto_sync_clip", "复制即同步记录"),
            ("one_key_flow", "热键一键生成"),
            ("use_history_context", "自动补足最近上下文"),
            ("auto_import_watch", "自动同步记录文件夹"),
        ]
        for i, (key, label) in enumerate(labels):
            chk[key] = tk.BooleanVar(value=bool(b.get(key, False)))
            cb = tk.Checkbutton(box, text=label, variable=chk[key], font=(FONT, 9),
                                bg=CARD, fg=TEXT, activebackground=CARD, selectcolor="#FFFFFF",
                                anchor="w", padx=1, highlightthickness=0, cursor="hand2")
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 6), pady=1)
        tk.Label(box, text="复制即同步只处理你主动复制的聊天内容；反效果自动补救会直接出一条新话术",
                 font=(FONT, 8), fg="#8A9099", bg=CARD, anchor="w", justify="left", wraplength=590).grid(
                     row=(len(labels) + 2) // 3, column=0, columnspan=3, sticky="w", padx=(2, 0), pady=(3, 0))
        hint("托盘常驻=右下角图标；关闭收进托盘=点 X 不退出。\n"
             "自启只写当前用户注册表；记录文件夹在「导入聊天」窗点「同步文件夹…」设置，只读 txt/log。")

        foot = tk.Frame(content, bg=CARD)
        foot.pack(fill="x", pady=(12, 16))
        state_txt = f"托盘：{'运行中' if self.tray.ok else '未启动'} · 自启：{'已开' if autostart_enabled() else '未开'} · v{VERSION}"
        tk.Label(foot, text=state_txt, font=(FONT, 8), fg="#8A9099", bg=CARD, anchor="w").pack(side="left")
        self._btn(foot, "打开数据目录", lambda: os.startfile(str(self.store.dir)),
                  kind="ghost", small=True).pack(side="right")

        bar = tk.Frame(win, bg=CARD)
        bar.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 12))

        def detect_skill():
            found = self.detect_skill_root()
            if not found:
                messagebox.showinfo("检测原始技能", "没在常见位置找到原生的 goutoujunshi 技能目录。\n可以手动选：一般是 .doubao\\agent_mode\\workspace\\.user_skills\\goutoujunshi")
                return
            fields["skill_root"].delete(0, "end")
            fields["skill_root"].insert(0, str(found))

        def browse_skill():
            path = filedialog.askdirectory(title="选择 goutoujunshi 技能目录（里面有 SKILL.md）")
            if not path:
                return
            if not self.is_valid_skill(Path(path)):
                messagebox.showwarning("技能目录", "这个文件夹里没有 SKILL.md，换一个试试")
                return
            fields["skill_root"].delete(0, "end")
            fields["skill_root"].insert(0, path)

        def test():
            c = LLMClient(self._collect(fields, chk)["api"])
            try:
                messagebox.showinfo("测试结果", f"连接正常：{c.ping()[:40]}")
            except LLMError as e:
                messagebox.showerror("测试失败", str(e))

        old_scale = self._font_scale

        def cancel():
            self._apply_content_font_scale(old_scale)
            win.destroy()

        def save():
            new = self._collect(fields, chk)
            new_ui = dict(new.get("ui") or {})
            new_ui["font_scale"] = font_state["scale"]
            new["ui"] = new_ui
            old_beh = self.behavior()
            root_raw = (new.get("skill_root") or "").strip()
            if root_raw and root_raw != (self.store.config.get("skill_root") or ""):
                ok, msg = self.reload_engine(Path(root_raw))
                if not ok:
                    messagebox.showwarning("技能目录", f"{msg}，仍沿用原来的技能目录")
                    new["skill_root"] = self.store.config.get("skill_root", "")
                else:
                    self._toast(msg)
            elif not root_raw:
                new["skill_root"] = ""
            self.store.apply_config(new)
            self._apply_content_font_scale(font_state["scale"])
            self.client = LLMClient(new["api"])
            self.my_names = new.get("my_names", [])
            self.extra_style = new.get("extra_style", "")
            self._register_hotkey(new.get("hotkey", "ctrl+alt+m"))

            beh = new["behavior"]
            # 开机自启：只在开关变化时动注册表
            if bool(beh.get("autostart")) != bool(old_beh.get("autostart")):
                ok, msg = set_autostart(bool(beh.get("autostart")), self.main_py)
                if not ok:
                    messagebox.showwarning("开机自启", msg)
            # 托盘：按开关启停
            if beh.get("tray") and not self.tray.ok:
                self.tray.start()
            elif not beh.get("tray") and self.tray.ok:
                self.tray.stop()

            self._refresh_status()
            win.destroy()
            self._toast("设置已保存")

        self._btn(bar, "保存", save, kind="primary").pack(side="right")
        self._btn(bar, "取消", cancel, kind="ghost").pack(side="right", padx=(0, 8))
        self._btn(bar, "测试连接", test, kind="ghost").pack(side="left")
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.bind("<Escape>", lambda e: cancel())
        win.wait_window()

    def _collect(self, fields, chk=None) -> dict:
        raw = {k: e.get().strip() for k, e in fields.items()}
        api = dict(self.store.config["api"])
        api.update({
            "base_url": raw["base_url"], "api_key": raw["api_key"],
            "model": raw["model"], "vision_model": raw["vision_model"],
            "temperature": self._num(raw["temperature"], 0.7),
        })
        beh = dict(self.behavior())
        if chk:
            for k, v in chk.items():
                beh[k] = bool(v.get())
        return {
            "api": api,
            "hotkey": raw["hotkey"] or "ctrl+alt+m",
            "my_names": clean_names([x.strip() for x in raw["my_names"].replace(",", "，").split("，") if x.strip()]),
            "extra_style": raw["extra_style"],
            "context_n": max(4, min(50, int(self._num(raw.get("context_n", "16"), 16)))),
            "import_dir": self.store.config.get("import_dir", ""),
            "skill_root": raw.get("skill_root", ""),
            "ui": self.store.config.get("ui", {}),
            "behavior": beh,
        }

    @staticmethod
    def _num(s: str, default: float) -> float:
        try:
            return float(s)
        except Exception:
            return default

    def open_library(self):
        win = tk.Toplevel(self.root)
        self.set_win_icon(win)
        win.title("话术库")
        win.configure(bg=CARD)
        win.geometry(f"480x520+{self.root.winfo_x() + 30}+{self.root.winfo_y() + 40}")
        win.attributes("-topmost", True)
        left = tk.Frame(win, bg=CARD)
        left.pack(fill="both", expand=True, padx=16, pady=(14, 6))
        lb = tk.Listbox(left, font=(FONT, 9), relief="flat", bg="#FBFCFE",
                        highlightthickness=1, highlightbackground=BORDER, activestyle="none")
        lb.pack(side="left", fill="both", expand=True)
        prev = tk.Text(win, height=7, wrap="word", font=(FONT, 10), relief="flat",
                       bg="#FBFCFE", highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        prev.pack(fill="x", padx=16, pady=(0, 6))

        def rebuild():
            lb.delete(0, "end")
            for it in self.store.library:
                lb.insert("end", f"{it['ts']}  {it['target']}  {it['draft'][:22]}")
            count.configure(text=f"共 {len(self.store.library)} 条")

        def show(_=None):
            sel = lb.curselection()
            if sel and sel[0] < len(self.store.library):
                prev.delete("1.0", "end")
                prev.insert("1.0", self.store.library[sel[0]]["draft"])

        lb.bind("<<ListboxSelect>>", show)
        bar = tk.Frame(win, bg=CARD)
        bar.pack(fill="x", padx=16, pady=(0, 14))
        count = tk.Label(bar, text="", font=(FONT, 9), fg=SUB, bg=CARD)
        count.pack(side="left")

        def copy_sel():
            sel = lb.curselection()
            if sel and sel[0] < len(self.store.library):
                self._copy(self.store.library[sel[0]]["draft"])
                self._toast("已复制")

        def delete_sel():
            sel = lb.curselection()
            if not sel:
                self._toast("先在列表里选一条")
                return
            it = self.store.library[sel[0]]
            if not messagebox.askyesno("删除话术", f"删除这一条？\n\n{it.get('draft', '')[:80]}"):
                return
            self.store.delete_library(sel[0])
            prev.delete("1.0", "end")
            rebuild()
            self._toast("已从话术库删除")

        self._btn(bar, "删除选中", delete_sel, kind="ghost").pack(side="left", padx=(10, 0))
        self._btn(bar, "复制选中", copy_sel, kind="primary").pack(side="right")
        self._btn(bar, "关闭", win.destroy, kind="ghost").pack(side="right", padx=(0, 6))
        rebuild()

    # ---------------------------------------------------------------- 收尾
    def _quit(self):
        self.quit_app()

    def run(self):
        self._refresh_contacts()
        self.root.after(1500, self._watch_clip)
        self.root.after(2000, self._watch_import_dir)
        self.root.mainloop()
