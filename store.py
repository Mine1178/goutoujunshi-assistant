# -*- coding: utf-8 -*-
"""本地存储：配置、对象档案、话术库、历史。全部落在程序目录 data/ 下，纯本地不上传。"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_CONFIG = {
    "api": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.7,
        "max_tokens": 900,
        "timeout": 60,
    },
    "hotkey": "ctrl+alt+m",
    "my_names": [],
    "extra_style": "",
    "context_n": 16,
    "import_dir": "",
    "skill_root": "",  # 留空 = 用安装包内 skill/goutoujunshi
    "behavior": {
        "tray": True,
        "close_to_tray": True,
        "autostart": False,
        "auto_copy": True,
        "hotkey_read_clip": True,
        "auto_judge": True,
        "auto_rescue": True,
        "auto_sync_clip": True,
        "one_key_flow": True,
        "use_history_context": True,
        "auto_import_watch": True,
    },
    "ui": {"width": 560, "height": 840, "always_on_top": True,
           "font_size": 10, "font_scale": 1.0},
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Store:
    def __init__(self, app_dir: Path):
        self.dir = app_dir / "data"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.profiles_path = self.dir / "profiles.json"
        self.library_path = self.dir / "library.json"
        self.history_path = self.dir / "history.jsonl"
        self.config = _merge(DEFAULT_CONFIG, self._load_json(self.config_path, DEFAULT_CONFIG))
        self.profiles = self._load_json(self.profiles_path, {})
        self.library = self._load_json(self.library_path, [])

    # ---------- 基础 IO ----------
    @staticmethod
    def _load_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return json.loads(json.dumps(default))  # deep copy

    @staticmethod
    def _save_json(path: Path, obj) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def save_config(self) -> None:
        self._save_json(self.config_path, self.config)

    def apply_config(self, new_cfg: dict) -> None:
        self.config = _merge(DEFAULT_CONFIG, new_cfg)
        self.save_config()

    # ---------- 档案 ----------
    def profile(self, target: str) -> dict:
        if not target:
            return {}
        return self.profiles.get(target, {})

    def profile_text(self, target: str) -> str:
        p = self.profile(target)
        if not p:
            return ""
        parts = [f"对象代号：{target}"]
        if p.get("mbti"):
            parts.append(f"MBTI：{p['mbti']}")
        if p.get("score") not in (None, ""):
            parts.append(f"主观综合评分：{p['score']}")
        if p.get("relation"):
            parts.append(f"当前关系：{p['relation']}")
        if p.get("note"):
            parts.append(f"关键背景：{p['note']}")
        if p.get("updated"):
            parts.append(f"（更新于 {p['updated']}）")
        return "；".join(parts)

    def save_profile(self, target: str, data: dict) -> None:
        if not target:
            return
        data = {k: v for k, v in data.items() if v not in (None, "")}
        data["updated"] = time.strftime("%Y-%m-%d %H:%M")
        self.profiles[target] = data
        self._save_json(self.profiles_path, self.profiles)

    def targets(self) -> list[str]:
        return sorted(self.profiles.keys())

    # ---------- 话术库 ----------
    def add_library(self, target: str, draft: str, log: str, note: str = "") -> None:
        self.library.insert(0, {
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "target": target or "未命名",
            "draft": draft,
            "log": log[:600],
            "note": note,
        })
        self.library = self.library[:200]
        self._save_json(self.library_path, self.library)

    def delete_library(self, index: int) -> bool:
        """删除话术库里的第 index 条；返回是否真的删了。"""
        if 0 <= index < len(self.library):
            self.library.pop(index)
            self._save_json(self.library_path, self.library)
            return True
        return False

    def history(self, target: str = "", limit: int = 6) -> list[str]:
        """该对象最近几轮已发送话术，用于"别重复同一句"。"""
        if not self.history_path.exists():
            return []
        out = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if target and rec.get("target") != target:
                continue
            out.append(rec.get("draft", ""))
        return out[-limit:]

    def add_history(self, target: str, draft: str) -> None:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "target": target or "", "draft": draft}
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
