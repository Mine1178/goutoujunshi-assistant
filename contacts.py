# -*- coding: utf-8 -*-
"""联系人 + 记忆 + 话术风格的本地存储。

每个联系人一份目录：data/contacts/<slug>/
  messages.jsonl   导入的历史聊天（去重，上限 3000 条）
  memory.json      关键事件、已发话术、可编辑的现状摘要
contacts.json      所有人的档案（昵称/别名/MBTI/评分/关系/风格统计）
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

MAX_MESSAGES = 3000
MAX_EVENTS = 60
MAX_DRAFTS = 60
MAX_FEEDBACK = 40

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
SPLIT_RE = re.compile(r"[，。！？,\.\!\?；;：:\s\n…~]+")
STOP = set("的了是我你他她它们在有和就都而及与着或一个不没很也还要会说去来到让给对从被把为以于之其这那个吗呢吧啊呀哦嗯好想感觉觉得")

# ---- 风格分析（本地规则，不靠模型，便于随时复算）----
COLD_WORDS = ["算了", "不想", "不用", "没心情", "不知道", "忙", "睡了", "改天", "再说"]
TOPICS = [
    ("工作", ("上班", "下班", "加班", "工作", "开会", "出差", "忙")),
    ("赚钱", ("赚钱", "工资", "钱", "花销", "房租", "借")),
    ("身体", ("累", "困", "失眠", "睡不着", "生病", "感冒", "发烧", "医院", "疼")),
    ("家里", ("妈", "爸", "家", "孩子", "老公", "老婆", "婆婆")),
    ("安排", ("周末", "明天", "后天", "放假", "约", "见面", "回去", "旅行", "生日")),
    ("情绪", ("烦", "难受", "开心", "委屈", "生气", "难过", "压力")),
]
LAUGH_RE = re.compile(r"哈|嘿嘿|呵呵|笑死|😂|🤣|😄|😊")
ELLIPSIS_RE = re.compile(r"[.…。]{2,}|……|。。")


def _scale(v: float, lo: int = 5, hi: int = 98) -> int:
    return max(lo, min(hi, int(round(v))))


def _reply_score(minutes: float) -> int:
    if not minutes:
        return 65
    if minutes <= 10:
        return 95
    if minutes <= 30:
        return 75
    if minutes <= 60:
        return 55
    return 35


def _reply_gap(msgs: list[dict]) -> float:
    """她回我一条，平均隔多少分钟（按消息时间戳算）。"""
    from datetime import datetime
    gaps = []
    prev_me = None
    for m in msgs:
        ts = str(m.get("ts") or "")
        try:
            t = datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if m.get("role") == "me":
            prev_me = t
        elif m.get("role") == "them" and prev_me:
            gaps.append((t - prev_me).total_seconds() / 60)
            prev_me = None
    if not gaps:
        return 0.0
    return round(sum(gaps) / len(gaps), 1)


def _median(nums: list[int]) -> float:
    if not nums:
        return 0
    s = sorted(nums)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _initiative(msgs: list[dict], role: str) -> int:
    """谁先开话头：该角色在「空白 15 分钟以上」之后发的第一条，或整段第一条。"""
    n = 0
    prev_t = None
    for m in msgs:
        t = None
        ts = (m.get("ts") or "").strip()
        if ts:
            try:
                t = datetime.strptime(ts, "%Y-%m-%d %H:%M")
            except ValueError:
                try:
                    t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    t = None
        if m.get("role") == role and (prev_t is None or (t and (t - prev_t).total_seconds() >= 900)):
            n += 1
        if t:
            prev_t = t
    return n


def _deep(msgs: list[dict], role: str = "them") -> dict:
    """比 _profile 更细的画像：句长分布、提问/表情/冷词、连发、开场、口头禅。

    msgs 传整段对话；role 指定看谁，连发才统计得准。
    """
    mine = [m for m in msgs if m.get("role") == role]
    if not mine:
        return {}
    texts = [(m.get("text") or "").strip() for m in mine if (m.get("text") or "").strip()]
    if not texts:
        return {}
    lens = [len(t) for t in texts]
    total = sum(lens) or 1
    q = sum(1 for t in texts if "?" in t or "？" in t)
    excl = sum(1 for t in texts if "!" in t or "！" in t)
    ell = sum(1 for t in texts if ELLIPSIS_RE.search(t))
    emo = sum(len(EMOJI_RE.findall(t)) for t in texts)
    laugh = sum(1 for t in texts if LAUGH_RE.search(t))
    short = sum(1 for n in lens if n <= 4)
    freq: dict[str, int] = {}
    for t in texts:
        for seg in SPLIT_RE.split(t):
            seg = seg.strip().strip("。，！？…、~ ")
            if 2 <= len(seg) <= 8 and not all(c in STOP for c in seg):
                freq[seg] = freq.get(seg, 0) + 1
    common = [w for w, c in sorted(freq.items(), key=lambda x: -x[1]) if c >= 2][:8]
    openers: dict[str, int] = {}
    for t in texts:
        head = t[:2]
        if len(head) == 2:
            openers[head] = openers.get(head, 0) + 1
    cold = [w for w in COLD_WORDS if any(w in t for t in texts)]
    cold_hits = sum(1 for t in texts if any(w in t for w in COLD_WORDS))
    # 连发：同一人连续多条算一波
    bursts: list[int] = []
    prev_role = None
    run = 0
    for m in msgs:
        role = m.get("role")
        if role == prev_role:
            run += 1
        else:
            if run:
                bursts.append(run)
            run = 1
        prev_role = role
    if run:
        bursts.append(run)
    return {
        "count": len(texts),
        "avg": round(total / len(texts), 1),
        "median": _median(lens),
        "max": max(lens),
        "short_pct": round(short * 100 / len(texts)),
        "q_pct": round(q * 100 / len(texts)),
        "q_n": q,
        "excl": excl,
        "ellipsis": ell,
        "emoji": emo,
        "laugh": laugh,
        "common": common,
        "openers": [w for w, c in sorted(openers.items(), key=lambda x: -x[1]) if c >= 2][:4],
        "cold": cold[:4],
        "cold_n": cold_hits,
        "initiative": _initiative(msgs, role),
        "burst": round(sum(bursts) / len(bursts), 1) if bursts else 1,
        "burst_max": max(bursts) if bursts else 1,
    }


def _bullets(p: dict, who: str) -> list[str]:
    if not p:
        return ["语料不够，先导入这个人的聊天记录。"]
    rows = []
    rows.append(f"句长：平均 {p['avg']} 字/条，中位 {p['median']} 字，最长 {p['max']} 字；"
                f"≤4 字的短句占 {p['short_pct']}%。")
    bits = [f"问句 {p['q_pct']}%（{p['q_n']} 条）"]
    if p["excl"]:
        bits.append(f"感叹 {p['excl']} 条")
    if p["ellipsis"]:
        bits.append(f"省略号/点点 {p['ellipsis']} 条")
    if p["emoji"]:
        bits.append(f"表情符号 {p['emoji']} 个")
    if p["laugh"]:
        bits.append(f"带笑（哈/表情）{p['laugh']} 条")
    rows.append("语气标记：" + "；".join(bits) + "。")
    if p["common"]:
        rows.append("高频用语（口头禅候选）：" + "、".join(p["common"][:6]) + "。")
    if p["openers"] and who != "她":
        rows.append("常用开头：" + "、".join(p["openers"]) + "。")
    if p["cold"]:
        rows.append(f"偏冷/收线用语：{'、'.join(p['cold'])}（出现在 {p['cold_n']} 条里）。")
    rows.append(f"主动开话头：{p['initiative']} 次（隔 15 分钟以上由{who}先说）。")
    rows.append(f"连发：平均每轮 {p['burst']} 条，最多一次连发 {p['burst_max']} 条。")
    return rows


def _summary_line(chat: dict, own: dict) -> str:
    if not chat:
        return "还没有她的语料，先导入聊天记录。"
    parts = []
    if chat["avg"] <= 8 and chat["short_pct"] >= 45:
        parts.append(f"她是短促回应型（平均 {chat['avg']} 字、短句占 {chat['short_pct']}%），话题多半由你发起")
    elif chat["avg"] <= 15:
        parts.append(f"她句子不长（平均 {chat['avg']} 字），愿意接话但不主动铺展")
    else:
        parts.append(f"她愿意展开说（平均 {chat['avg']} 字/条）")
    if chat.get("initiative", 0) <= 1:
        parts.append("整段里她几乎不先开口，基本等你起话题")
    else:
        parts.append(f"她主动开过 {chat['initiative']} 次话头")
    if chat["q_pct"] <= 20:
        parts.append(f"她很少反问（问句仅 {chat['q_pct']}%）")
    if chat["cold_n"] >= chat["count"] * 0.2:
        parts.append("收线/婉拒的信号不少，推进要一次只走半步")
    if own and own.get("avg", 0) > chat["avg"] * 2:
        parts.append("你发得比她长不少，容易被读成压力")
    return "；".join(parts) + "。"


def _advice(chat: dict, own: dict) -> list[str]:
    if not chat:
        return ["先导入聊天记录，才有可用的口径。"]
    cap = max(10, int(round(chat["avg"] * 1.5)))
    rows = [f"回复长度：一条控制在 {cap} 字以内（她平均 {chat['avg']} 字，取 1.5 倍）。"]
    if chat["short_pct"] >= 40:
        rows.append("她短你就短：一条只做一件事（接话，或一个具体问题，或一个具体邀约），别三件事塞一段。")
    if chat["q_pct"] <= 20:
        rows.append("别连续追问：她本来就不反问，一次只留一个问题，问完等她回。")
    if chat["cold_n"] >= chat["count"] * 0.2:
        rows.append("读到「算了/不用/忙/睡了」这类词就先收：给一句体面的收线，隔天再起新话题。")
    warm = (chat["emoji"] + chat["laugh"]) / max(1, chat["count"])
    if warm <= 0.1:
        rows.append(f"她几乎不用表情和玩笑（{chat['emoji']} 个表情、{chat['laugh']} 条带笑）：别堆 emoji 和网络梗，正经口语就够。")
    elif warm >= 0.35:
        rows.append("她带表情/玩笑比较多：可以轻一点接，但一条最多一个 emoji。")
    else:
        rows.append(f"她表情/玩笑不多（{chat['emoji']} 个表情、{chat['laugh']} 条带笑）：偶尔一个就够，别刷屏。")
    if own and own.get("burst", 1) >= 2:
        rows.append(f"你平均一轮连发 {own['burst']} 条：改成一条等她回，减少压迫感。")
    if own and own.get("q_pct", 0) >= 50:
        rows.append(f"你问句占到 {own['q_pct']}%：掺一些陈述句（自己的状态/具体安排），别像访谈。")
    rows.append("称呼与语气：沿用她听过的叫法，不要突然换亲密称呼；不承诺做不到的事。")
    return rows


def _samples(msgs: list[dict], n: int) -> list[str]:
    picked = []
    for m in reversed(msgs):
        t = (m.get("text") or "").replace("\n", " / ").strip()
        if not t or not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", t):
            continue          # 纯符号/空白，跳过
        if re.fullmatch(r"[\[【（(][^\]】）)]{1,8}[\]】）)]", t):
            continue          # [动画表情] [图片] 这类占位符，跳过
        if len(t) > 60:
            t = t[:58] + "…"
        if t in picked:
            continue
        picked.append(t)
        if len(picked) >= n:
            break
    return picked[::-1]



def slugify(name: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", (name or "").strip())
    s = s.strip("_")[:24]
    if not s:
        s = "unnamed"
    return s + "_" + hashlib.md5((name or "").encode("utf-8")).hexdigest()[:6]


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(default))


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Contacts:
    def __init__(self, data_dir: Path):
        self.base = data_dir / "contacts"
        self.base.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "contacts.json"
        self.data: dict = _read_json(self.path, {})

    # ---------- 增删改查 ----------
    def all(self) -> dict:
        return self.data

    def names(self) -> list[str]:
        return sorted(self.data.keys())

    def get(self, name: str) -> dict:
        return self.data.get(name, {})

    def ensure(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return ""
        if name not in self.data:
            self.data[name] = {
                "name": name, "aliases": [name], "mbti": "", "score": "",
                "relation": "", "note": "", "created": time.strftime("%Y-%m-%d %H:%M"),
                "stats": {}, "style": {},
            }
            self._flush()
        return name

    def update(self, name: str, patch: dict) -> None:
        self.ensure(name)
        rec = self.data[name]
        for k, v in (patch or {}).items():
            rec[k] = v
        rec["updated"] = time.strftime("%Y-%m-%d %H:%M")
        self._flush()

    def delete(self, name: str) -> None:
        self.data.pop(name, None)
        self._flush()

    def match(self, spoken_name: str) -> str:
        """把聊天记录里的昵称对到已有联系人（按别名/包含关系匹配）。"""
        if not spoken_name:
            return ""
        low = spoken_name.lower()
        for name, rec in self.data.items():
            for alias in [name] + list(rec.get("aliases") or []):
                a = (alias or "").lower()
                if a and (a == low or a in low or low in a):
                    return name
        return ""

    def _flush(self) -> None:
        _write_json(self.path, self.data)

    def dir(self, name: str) -> Path:
        d = self.base / slugify(name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 消息 ----------
    def append_messages(self, name: str, messages: list[dict]) -> int:
        """追加并去重，返回真正新增条数。"""
        if not name or not messages:
            return 0
        path = self.dir(name) / "messages.jsonl"
        seen = set()
        existing = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                existing.append(rec)
                seen.add(hashlib.md5(
                    f"{rec.get('ts')}|{rec.get('speaker')}|{rec.get('text')}".encode()
                ).hexdigest())
        added = 0
        for m in messages:
            key = hashlib.md5(f"{m.get('ts')}|{m.get('speaker')}|{m.get('text')}".encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            existing.append(m)
            added += 1
        existing = existing[-MAX_MESSAGES:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in existing) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        self._bump_stats(name, added, existing)
        if added:
            try:
                self.auto_score(name)      # 记录一变就重算主干评分
                self.refresh_memory(name)  # 顺带自动刷新摘要与关键事件
            except Exception:
                pass
        return added

    def _bump_stats(self, name: str, added: int, all_msgs: list[dict]) -> None:
        rec = self.data.setdefault(name, {})
        stats = rec.setdefault("stats", {})
        stats["msg_count"] = len(all_msgs)
        stats["imported"] = stats.get("imported", 0) + added
        stats["last_import"] = time.strftime("%Y-%m-%d %H:%M")
        self._flush()

    def set_stats(self, name: str, extra: dict) -> None:
        """把导入解析出来的统计（平均回复分钟数等）记进对象档案。"""
        rec = self.data.get(name)
        if not rec or not extra:
            return
        stats = rec.setdefault("stats", {})
        for k, v in extra.items():
            if v not in (None, ""):
                stats[k] = v
        self._flush()

    def messages(self, name: str, limit: int = 0) -> list[dict]:
        path = self.dir(name) / "messages.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out[-limit:] if limit else out

    # ---------- 记忆 ----------
    def _memory(self, name: str) -> dict:
        return _read_json(self.dir(name) / "memory.json", {"events": [], "drafts": [], "summary": ""})

    def _save_memory(self, name: str, mem: dict) -> None:
        _write_json(self.dir(name) / "memory.json", mem)

    def add_event(self, name: str, text: str, source: str = "manual") -> None:
        if not (text or "").strip():
            return
        mem = self._memory(name)
        mem.setdefault("events", []).append({
            "ts": time.strftime("%Y-%m-%d"), "text": text.strip(), "source": source,
        })
        mem["events"] = mem["events"][-MAX_EVENTS:]
        self._save_memory(name, mem)

    def events(self, name: str) -> list[dict]:
        return self._memory(name).get("events", [])

    def remove_event(self, name: str, index: int) -> None:
        mem = self._memory(name)
        events = mem.get("events", [])
        if 0 <= index < len(events):
            events.pop(index)
            self._save_memory(name, mem)

    def set_summary(self, name: str, text: str) -> None:
        mem = self._memory(name)
        mem["summary"] = (text or "").strip()
        mem["summary_locked"] = bool(mem["summary"].strip())
        self._save_memory(name, mem)

    def unlock_summary(self, name: str) -> str:
        """清掉手改锁定，立刻按聊天重算一版自动摘要。"""
        mem = self._memory(name)
        mem["summary_locked"] = False
        self._save_memory(name, mem)
        self.refresh_memory(name)
        return self.summary(name)

    def summary(self, name: str) -> str:
        return self._memory(name).get("summary", "")

    def add_draft(self, name: str, draft: str, note: str = "") -> None:
        if not (draft or "").strip():
            return
        mem = self._memory(name)
        mem.setdefault("drafts", []).append({
            "ts": time.strftime("%Y-%m-%d %H:%M"), "draft": draft.strip(), "note": note,
        })
        mem["drafts"] = mem["drafts"][-MAX_DRAFTS:]
        self._save_memory(name, mem)

    def drafts(self, name: str, limit: int = 5) -> list[dict]:
        return self._memory(name).get("drafts", [])[-limit:]

    # ---------- 效果反馈 ----------
    def add_feedback(self, name: str, rating: str, draft: str, note: str = "") -> None:
        """rating: good / so-so / bad；note 可放她的真实回复。"""
        if not name:
            return
        mem = self._memory(name)
        mem.setdefault("feedback", []).append({
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "rating": rating,
            "draft": (draft or "")[:120],
            "note": (note or "").strip()[:200],
        })
        mem["feedback"] = mem["feedback"][-MAX_FEEDBACK:]
        self._save_memory(name, mem)

    def feedback(self, name: str, limit: int = 10) -> list[dict]:
        return self._memory(name).get("feedback", [])[-limit:]

    # ---------- 待判定的那一条 ----------
    def set_pending(self, name: str, draft: str, log: str = "", rescue: bool = False) -> None:
        if not name or not draft:
            return
        mem = self._memory(name)
        mem["pending"] = {
            "draft": draft[:400],
            "context": (log or "")[-1200:],
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "epoch": time.time(),
            "tries": 0,
            "rescue": bool(rescue),
        }
        self._save_memory(name, mem)

    def pending(self, name: str) -> dict:
        return self._memory(name).get("pending") or {}

    def bump_pending(self, name: str) -> int:
        mem = self._memory(name)
        p = mem.get("pending")
        if not p:
            return 0
        p["tries"] = int(p.get("tries", 0)) + 1
        self._save_memory(name, mem)
        return p["tries"]

    def clear_pending(self, name: str) -> None:
        mem = self._memory(name)
        if mem.pop("pending", None) is not None:
            self._save_memory(name, mem)

    def feedback_block(self, name: str) -> str:
        fb = self.feedback(name, 8)
        if not fb:
            return ""
        lines = []
        bad = [f for f in fb if f.get("rating") == "bad"][-2:]
        good = [f for f in fb if f.get("rating") == "good"][-2:]
        if good:
            lines.append("· 她用起来有效的写法（优先沿用这个力道）：")
            for f in good:
                lines.append(f"  - {f['draft']}" + (f"（她回了：{f['note']}）" if f.get("note") else ""))
        if bad:
            lines.append("· 试过但没推动的写法（别再来一遍）：")
            for f in bad:
                lines.append(f"  - {f['draft']}" + (f"（她回了：{f['note']}）" if f.get("note") else ""))
        return "\n".join(lines)

    # ---------- 风格学习 ----------
    def learn_style(self, name: str, my_names: list[str]) -> dict:
        msgs = self.messages(name)
        them = [m for m in msgs if m.get("role") == "them"]
        me = [m for m in msgs if m.get("role") == "me"]
        style = {
            "their": _deep(msgs, "them"),
            "mine": _deep(msgs, "me"),
            "learned_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        # 我方的原话样本：挑长度适中、信息量够的几条
        mine_samples = [m["text"] for m in me if 6 <= len(m.get("text", "")) <= 60]
        style["mine"]["samples"] = mine_samples[-3:][::-1]
        if name in self.data:
            self.data[name]["style"] = style
            self._flush()
        # 同时落一份人可读的「XX个人风格.md」
        try:
            style["md_path"] = str(self.write_style_md(name, my_names, style, msgs))
            if name in self.data:
                self.data[name]["style"]["md_path"] = style["md_path"]
                self._flush()
        except Exception:
            pass
        return style

    def style_md_path(self, name: str) -> Path:
        safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", (name or "对象").strip()) or "对象"
        return self.dir(name) / f"{safe}个人风格.md"

    def self_style_path(self) -> Path:
        """汇总所有对象里『我』的发言，生成一份自己的风格文件。"""
        return self.base.parent / "我的风格.md"

    def all_my_messages(self) -> list[dict]:
        rows: list[dict] = []
        for name in self.names():
            for m in self.messages(name):
                if m.get("role") == "me" and (m.get("text") or "").strip():
                    row = dict(m)
                    row["_contact"] = name
                    rows.append(row)
        return rows

    def learn_self_style(self, my_names: list[str], write_md: bool = True) -> dict:
        """按所有对象的我方发言，统计『我自己』的说话风格。"""
        rows = self.all_my_messages()
        if not rows:
            style = {"mine": {}, "by_contact": {}, "learned_at": time.strftime("%Y-%m-%d %H:%M")}
            if write_md:
                try:
                    style["md_path"] = str(self.write_self_style_md(my_names, style))
                except Exception:
                    pass
            return style

        # 用一条空对方消息隔开每个我方气泡，避免把不同对话的连发算成一轮
        flat: list[dict] = []
        for m in rows:
            flat.append({"role": "me", "text": m.get("text") or "", "ts": m.get("ts") or ""})
            flat.append({"role": "them", "text": ""})
        own = _deep(flat, "me")
        if own:
            own["initiative"] = 0
            own["burst"] = 1
            own["burst_max"] = 1
            samples: list[str] = []
            for m in rows:
                t = (m.get("text") or "").replace("\n", " / ").strip()
                if 6 <= len(t) <= 70 and t not in samples:
                    samples.append(t)
            own["samples"] = samples[-8:][::-1]

        by_contact: dict[str, dict] = {}
        for m in rows:
            c = m.get("_contact") or ""
            by_contact.setdefault(c, {"count": 0, "chars": 0})
            by_contact[c]["count"] += 1
            by_contact[c]["chars"] += len((m.get("text") or "").strip())
        style = {
            "mine": own,
            "by_contact": by_contact,
            "total": len(rows),
            "learned_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        if write_md:
            try:
                style["md_path"] = str(self.write_self_style_md(my_names, style))
            except Exception:
                pass
        return style

    def write_self_style_md(self, my_names: list[str], style: dict | None = None) -> Path:
        style = style or self.learn_self_style(my_names, write_md=False)
        own = style.get("mine") or {}
        rows = self.all_my_messages()
        by = style.get("by_contact") or {}
        lines: list[str] = []
        lines.append("# 我的说话风格（自动生成）")
        lines.append("")
        lines.append(f"> 汇总：{len(rows)} 条我方消息，覆盖 {len(by)} 个对象  ")
        lines.append(f"> 生成：{time.strftime('%Y-%m-%d %H:%M')}｜本地规则统计，不含模型推测  ")
        lines.append("> 重新统计：对象管理 → ★ 我（自己） →『重新学习风格』，会覆盖本文件。")
        lines.append("")
        if not own:
            lines.append("## 还没有样本")
            lines.append("先导入任意对象的聊天记录，系统会自动汇总你说过的话。")
            path = self.self_style_path()
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return path

        lines.append("## 一句话")
        bits = []
        if own.get("avg", 0) <= 10 and own.get("short_pct", 0) >= 45:
            bits.append(f"你偏短句直给（平均 {own['avg']} 字，短句 {own['short_pct']}%）")
        elif own.get("avg", 0) <= 18:
            bits.append(f"你句子不长（平均 {own['avg']} 字），接话快、不绕")
        else:
            bits.append(f"你愿意展开说（平均 {own['avg']} 字/条）")
        if own.get("q_pct", 0) >= 40:
            bits.append(f"问句偏多（{own['q_pct']}%），习惯用提问推进")
        else:
            bits.append(f"以陈述为主（问句仅 {own['q_pct']}%）")
        if own.get("emoji", 0) + own.get("laugh", 0) >= max(2, own.get("count", 1) // 10):
            bits.append("会带表情/玩笑，语气不板")
        else:
            bits.append("表情和玩笑不多，偏朴素口语")
        lines.append("；".join(bits) + "。")
        lines.append("")
        lines.append(f"## 我的风格特点（{('、'.join(my_names) if my_names else '我')}）")
        for row in _bullets(own, "你"):
            lines.append(f"- {row}")
        lines.append("")
        if own.get("samples"):
            lines.append("## 我的真实原话（新话术要像我会说的话）")
            for t in own["samples"]:
                lines.append(f"- {t}")
            lines.append("")
        lines.append("## 分对象分布")
        for name, s in sorted(by.items(), key=lambda kv: -kv[1].get("count", 0)):
            lines.append(f"- {name}：{s.get('count', 0)} 条，{s.get('chars', 0)} 字")
        lines.append("")
        lines.append("## 写话术时照这个来")
        lines.append(f"- 句长：一条控制在 {max(8, int(round((own.get('avg') or 10) * 1.3)))} 字左右，别突然写成小作文。")
        if own.get("common"):
            lines.append("- 口头禅可以自然带一点：" + "、".join(own["common"][:5]) + "。")
        if own.get("q_pct", 0) >= 45:
            lines.append("- 你本来就会提问：保留一个具体问题，但别连着追问。")
        else:
            lines.append("- 你不爱连环问：多用陈述和具体安排推进，一次最多留一个问题。")
        lines.append("- 保持你原来的称呼和语气，不要换成情感博主腔。")
        path = self.self_style_path()
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def self_style_block(self, my_names: list[str]) -> str:
        style = self.learn_self_style(my_names, write_md=False)
        own = style.get("mine") or {}
        if not own:
            return "还没有你的发言样本：先导入任意对象的聊天记录，再说。"
        lines = []
        bits = [f"{own.get('count', 0)} 条我方消息，平均 {own.get('avg')} 字/条（中位 {own.get('median')}）"]
        bits.append(f"短句 {own.get('short_pct')}%、问句 {own.get('q_pct')}%")
        bits.append(f"表情 {own.get('emoji')} 个、带笑 {own.get('laugh')} 条")
        if own.get("common"):
            bits.append("口头禅：" + "、".join(own["common"][:5]))
        lines.append("· " + "；".join(bits))
        lines.append("· 常用开头：" + ("、".join(own.get("openers") or []) or "样本还不够"))
        if own.get("samples"):
            lines.append("· 最近原话：")
            for i, s in enumerate(own["samples"][:3], 1):
                lines.append(f"  {i}. {s}")
        return "\n".join(lines)

    def write_style_md(self, name: str, my_names: list[str],
                       style: dict | None = None, msgs: list[dict] | None = None) -> Path:
        """把两人的说话风格写成一份 markdown，落在该对象目录下。"""
        style = style or self.data.get(name, {}).get("style") or {}
        msgs = msgs if msgs is not None else self.messages(name)
        their = [m for m in msgs if m.get("role") == "them"]
        me = [m for m in msgs if m.get("role") == "me"]
        chat = _deep(msgs, "them")
        own = _deep(msgs, "me")
        span = ""
        ts = [m.get("ts", "") for m in msgs if m.get("ts")]
        if ts:
            span = f"{min(ts)} ~ {max(ts)}"
        lines: list[str] = []
        lines.append(f"# {name} 的个人风格（自动生成）")
        lines.append("")
        lines.append(f"> 来源：`data/contacts/{slugify(name)}/messages.jsonl`  ")
        lines.append(f"> 语料：{len(msgs)} 条消息（她 {len(their)} / 我 {len(me)}）"
                     + (f"，{span}" if span else "") + "  ")
        lines.append(f"> 生成：{time.strftime('%Y-%m-%d %H:%M')}｜本地规则统计，不含模型推测  ")
        lines.append("> 重新学一遍：对象管理 → 记忆 →『重新学习风格』，会覆盖本文件。")
        lines.append("")
        lines.append("## 一句话")
        lines.append(_summary_line(chat, own))
        lines.append("")
        lines.append(f"## 她怎么说话（{name}，{len(their)} 条）")
        for row in _bullets(chat, "她"):
            lines.append(f"- {row}")
        lines.append("")
        lines.append(f"## 我怎么说话（{('、'.join(my_names) if my_names else '我')}，{len(me)} 条）")
        for row in _bullets(own, "你"):
            lines.append(f"- {row}")
        lines.append("")
        lines.append("## 实操口径（写话术时照这个来）")
        for row in _advice(chat, own):
            lines.append(f"- {row}")
        lines.append("")
        lines.append("## 真实语料·她的原话（保持语气，别改成书面语）")
        for t in _samples(their, 10):
            lines.append(f"- {t}")
        lines.append("")
        lines.append("## 真实语料·我的原话（新话术要像我说的话）")
        for t in _samples(me, 6):
            lines.append(f"- {t}")
        lines.append("")
        path = self.style_md_path(name)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _profile(msgs: list[dict]) -> dict:
        if not msgs:
            return {}
        texts = [m.get("text", "") for m in msgs]
        total = sum(len(t) for t in texts) or 1
        q = sum(1 for t in texts if "?" in t or "？" in t)
        emo = sum(len(EMOJI_RE.findall(t)) for t in texts)
        # 高频短句：按标点切，2–6 字且重复出现
        freq: dict[str, int] = {}
        for t in texts:
            for seg in SPLIT_RE.split(t):
                seg = seg.strip()
                if 2 <= len(seg) <= 6 and not all(c in STOP for c in seg):
                    freq[seg] = freq.get(seg, 0) + 1
        common = [w for w, c in sorted(freq.items(), key=lambda x: -x[1]) if c >= 2][:6]
        return {
            "count": len(msgs),
            "avg_len": round(total / len(msgs), 1),
            "question_pct": round(q * 100 / len(msgs)),
            "emoji": emo,
            "common": common,
        }

    def style_block(self, name: str, my_names: list[str]) -> str:
        rec = self.data.get(name, {})
        style = rec.get("style") or {}
        their = style.get("their") or {}
        if not style or (their and "median" not in their):
            # 没有风格画像，或还是旧版浅口径 → 重新学一遍（会顺带刷新 md）
            style = self.learn_style(name, my_names)
        their = style.get("their") or {}
        mine = style.get("mine") or {}
        lines = []
        if their:
            if "median" in their:                     # 新口径：带中位数/短句率/口头禅
                bits = [f"{their.get('avg')} 字/条（中位 {their.get('median')}，≤4 字 {their.get('short_pct')}%）"]
                bits.append(f"问句 {their.get('q_pct')}%")
                bits.append(f"表情 {their.get('emoji')} 个、带笑 {their.get('laugh')} 条")
                if their.get("common"):
                    bits.append("口头禅：" + "、".join(their["common"][:4]))
                lines.append("· 她：" + "；".join(bits))
                if their.get("cold"):
                    lines.append("· 她的收线词：" + "、".join(their["cold"][:4]) + "（看到就停，别硬推）")
                if their.get("initiative") is not None:
                    ni = their.get("initiative") or 0
                    lines.append("· 谁起话题：" + ("她几乎不先开口，话题得你起" if ni <= 1
                                               else f"她主动开过 {ni} 次话头"))
            else:
                bits = [f"平均 {their.get('avg_len')} 字/条"]
                if their.get("question_pct"):
                    bits.append(f"疑问句占 {their['question_pct']}%")
                if their.get("emoji"):
                    bits.append(f"用了 {their['emoji']} 个表情符号")
                if their.get("common"):
                    bits.append("高频用语：" + "、".join(their["common"][:4]))
                lines.append("· 她：" + "；".join(bits))
        if mine:
            if "median" in mine:
                bits = [f"{mine.get('avg')} 字/条（中位 {mine.get('median')}）"]
                if mine.get("q_pct"):
                    bits.append(f"问句 {mine['q_pct']}%")
                if mine.get("burst", 1) >= 2:
                    bits.append(f"平均一轮连发 {mine['burst']} 条")
                if mine.get("common"):
                    bits.append("你常用：" + "、".join(mine["common"][:4]))
                lines.append("· 你：" + "；".join(bits))
            else:
                bits = [f"平均 {mine.get('avg_len')} 字/条"]
                if mine.get("common"):
                    bits.append("常用：" + "、".join(mine["common"][:4]))
                lines.append("· 你：" + "；".join(bits))
            samples = mine.get("samples") or []
            if samples:
                lines.append("· 你最近的原话（保持这个语感，别写成情感博主腔）：")
                for i, s in enumerate(samples, 1):
                    lines.append(f"  {i}. {s}")
        deep_t, deep_m = ("median" in their), ("median" in mine)
        if deep_t or deep_m:
            tips = _advice(their, mine)[:2] if their else []
            if tips:
                lines.append("· 口径：" + " ".join(t.strip() for t in tips))
        elif their and their.get("avg_len", 0) and their["avg_len"] <= 12:
            lines.append("· 节奏要求：她说话很短，你的回复不要长篇大论，控制在她 1.5 倍长度以内。")
        return "\n".join(lines) if lines else ""

    def refresh_memory(self, name: str) -> None:
        """每收一次新聊天，自动刷新「现状摘要」和关键事件（本地规则，不花 API）。"""
        msgs = self.messages(name)
        if len(msgs) < 4:
            return
        them = [m for m in msgs if m.get("role") == "them" and (m.get("text") or "").strip()]
        me = [m for m in msgs if m.get("role") == "me" and (m.get("text") or "").strip()]
        if not them or not me:
            return
        mem = self._memory(name)
        recent = msgs[-30:]
        her_recent = them[-8:]

        # ---- 关键事件：从她的话里抓「工作/身体/钱/安排」这类可复用信息 ----
        hints = ("加班", "下班", "上班", "工作", "面试", "考试", "出差", "医院", "看病",
                 "生病", "发烧", "感冒", "失眠", "睡不着", "搬家", "房租", "借钱", "还钱",
                 "生日", "周末", "放假", "回家", "妈妈", "爸爸", "孩子", "相亲", "分手",
                 "冷战", "吵架", "约会", "见面", "减肥", "辞职")
        events = mem.setdefault("events", [])
        texts = {e.get("text") for e in events}
        added = 0
        for m in reversed(them[-40:]):
            t = (m.get("text") or "").strip()
            if not (4 <= len(t) <= 36) or t in texts:
                continue
            if not any(h in t for h in hints):
                continue
            ts = (m.get("ts") or "")[:16]
            events.append({"ts": ts or time.strftime("%Y-%m-%d"), "text": t, "source": "auto"})
            texts.add(t)
            added += 1
            if added >= 3:
                break
        # 自动事件最多留 6 条，避免刷屏；手动的全留
        auto = [e for e in events if e.get("source") == "auto"]
        if len(auto) > 6:
            drop = set(id(e) for e in auto[:-6])
            events = [e for e in events if id(e) not in drop]
            mem["events"] = events[-MAX_EVENTS:]
        elif added:
            mem["events"] = events[-MAX_EVENTS:]

        # ---- 现状摘要 ----
        if not mem.get("summary_locked"):
            topics = []
            for label, words in TOPICS:
                if any(w in (m.get("text") or "") for m in them[-40:] for w in words):
                    topics.append(label)
            topics = topics[:3]
            last = her_recent[-1].get("text", "").strip()
            tail = last[:16] + ("…" if len(last) > 16 else "")
            cold_hit = any(w in last for w in COLD_WORDS)
            if cold_hit:
                tone = "像在收线"
            elif "?" in last or "？" in last:
                tone = "在反问"
            elif LAUGH_RE.search(last) or EMOJI_RE.search(last):
                tone = "语气轻松"
            else:
                tone = "正常接话"
            mine_n = sum(1 for m in recent if m.get("role") == "me")
            ini = _initiative(recent, "them")
            unanswered = ""
            if msgs[-1].get("role") == "me":
                q = msgs[-1].get("text", "").strip()
                if "?" in q or "？" in q:
                    unanswered = f"你最后问的「{q[:18]}」还没回音。"
            sc = (self.data.get(name, {}).get("auto_score") or {})
            parts = [f"最近 30 条里你发了 {mine_n} 条、她主动开口 {ini} 次"]
            parts.append("她在" + ("、".join(topics) if topics else "忙自己的事"))
            parts.append(f"最后一句是「{tail}」（{tone}）")
            if unanswered:
                parts.append(unanswered.rstrip("。"))
            if sc.get("total"):
                parts.append(f"温度 {sc.get('grade')}（{sc.get('total')}）")
            mem["summary_auto"] = "；".join(parts) + "。"
            mem["summary"] = mem["summary_auto"]
        self._save_memory(name, mem)

    def auto_score(self, name: str) -> dict:
        """主干评分：只看聊天本身（热度/主动/情绪/开放/均衡/冷处理），本地算，不花 API。"""
        rec = self.data.get(name)
        msgs = self.messages(name)
        them = [m for m in msgs if m.get("role") == "them"]
        me = [m for m in msgs if m.get("role") == "me"]
        at = time.strftime("%Y-%m-%d %H:%M")
        if not rec or len(them) < 3 or len(me) < 3:
            res = {"total": 0, "grade": "样本不足", "dims": [],
                   "advice": "先导入几轮完整对话（双方各 3 条以上）再评分。", "at": at}
            if rec:
                rec["auto_score"] = res
                self._flush()
            return res

        chat = _deep(msgs, "them")
        own = _deep(msgs, "me")
        stats = rec.get("stats") or {}
        try:
            lat = float(stats.get("avg_reply_min") or 0)
        except (TypeError, ValueError):
            lat = 0.0
        her_lat = _reply_gap(msgs)          # 她回我的平均间隔（分钟）
        if her_lat:
            lat = her_lat
        avg, short, q = chat["avg"], chat["short_pct"], chat["q_pct"]
        warm = (chat["emoji"] + chat["laugh"]) / max(1, chat["count"])
        cold = chat["cold_n"] / max(1, chat["count"])
        ini, burst = chat["initiative"], chat["burst"]
        ratio = own["avg"] / max(1.0, avg)

        # 每个维度 0–100，口径写在 note 里，方便你手改时对照
        heat = _scale(0.35 * min(100, avg / 10 * 100) + 0.20 * (100 - short)
                      + 0.15 * min(100, warm * 250) + 0.15 * _reply_score(lat)
                      + 0.15 * min(100, burst * 30))
        initiate = _scale(22 + ini * 16 + min(30, (burst - 1) * 12) + q * 0.6)
        warmth = _scale(15 + warm * 260)
        openness = _scale(q * 1.6 + min(45, avg / 12 * 45))
        balance = _scale(100 - abs(ratio - 1.2) * 45)
        coldfree = _scale(100 - cold * 260)

        dims = [
            {"key": "heat", "name": "回应热度", "v": heat,
             "note": f"她 {avg} 字/条、短句 {short}%、连发 {burst} 条、平均隔 {lat or '—'} 分钟回你"},
            {"key": "initiate", "name": "主动性", "v": initiate,
             "note": f"她主动开话头 {ini} 次、问句 {q}%"},
            {"key": "warmth", "name": "情绪温度", "v": warmth,
             "note": f"表情 {chat['emoji']} 个、带笑 {chat['laugh']} 条"},
            {"key": "openness", "name": "开放度", "v": openness,
             "note": f"问句 {q}%、她愿意展开的平均长度 {avg} 字"},
            {"key": "balance", "name": "投入均衡", "v": balance,
             "note": f"你/她长度比 {ratio:.2f}（越接近 1.2 越顺）"},
            {"key": "coldfree", "name": "无明显冷处理", "v": coldfree,
             "note": f"收线词出现 {chat['cold_n']} 条（{chat['count']} 条里）"},
        ]
        total = _scale(heat * .28 + initiate * .20 + warmth * .14
                       + openness * .14 + balance * .12 + coldfree * .12)
        grade = "冷" if total < 45 else ("平" if total < 60 else ("温" if total < 75 else "热"))
        tips = []
        if warmth < 40:
            tips.append("她几乎不接情绪梗，少堆表情和玩笑")
        if openness < 45:
            tips.append("她很少反问，一次只留一个问题")
        if heat < 50:
            tips.append("回得短、隔得久，推进一次只走半步")
        if balance < 60:
            tips.append("你发得比她长不少，先压到自己 1.5 倍以内")
        if coldfree < 55:
            tips.append("收线词不少，读到「忙/算了/不用」就体面收")
        advice = "；".join(tips[:2]) + "。" if tips else "互动均衡，按现有节奏推进即可。"

        res = {"total": total, "grade": grade, "dims": dims, "advice": advice, "at": at}
        rec["auto_score"] = res
        self._flush()
        return res

    def effective_score(self, name: str) -> dict:
        """当前采用的分数：手填优先，没有就自动分。"""
        rec = self.data.get(name) or {}
        manual = str(rec.get("score") or "").strip()
        auto = rec.get("auto_score") or {}
        if not auto:
            auto = self.auto_score(name)
        if manual:
            try:
                val = int(float(manual))
            except ValueError:
                val = 0
            return {"total": val, "grade": auto.get("grade", ""), "source": "manual",
                    "auto": auto.get("total", 0), "advice": auto.get("advice", "")}
        return {"total": auto.get("total", 0), "grade": auto.get("grade", "样本不足"),
                "source": "auto", "auto": auto.get("total", 0), "advice": auto.get("advice", "")}

    def memory_block(self, name: str, my_names: list[str], with_style: bool = True) -> str:
        rec = self.data.get(name, {})
        if not rec:
            return ""
        parts = []
        head = [f"对象代号：{name}"]
        if rec.get("mbti"):
            head.append(f"MBTI：{rec['mbti']}")
        auto = rec.get("auto_score") or {}
        if rec.get("score"):
            head.append(f"主观评分：{rec['score']}"
                        + (f"（自动 {auto.get('total')}）" if auto.get("total") else ""))
        elif auto.get("total"):
            head.append(f"关系温度：{auto.get('grade')}（自动 {auto.get('total')}）")
        if rec.get("relation"):
            head.append(f"当前关系：{rec['relation']}")
        parts.append("；".join(head))
        if rec.get("note"):
            parts.append(f"背景：{rec['note']}")
        if rec.get("summary"):
            parts.append(f"现状摘要：{rec['summary']}")
        events = self.events(name)
        if events:
            parts.append("关键事件：")
            for e in events[-8:]:
                parts.append(f"  - {e['ts']} {e['text']}")
        drafts = self.drafts(name, 4)
        if drafts:
            parts.append("最近给过的话术（别重复）：" + " / ".join(d["draft"][:38] for d in drafts))
        fb = self.feedback_block(name)
        if fb:
            parts.append("实际效果回收：\n" + fb)
        if with_style:
            st = self.style_block(name, my_names)
            if st:
                parts.append("\n对象说话特征：\n" + st)
        return "\n".join(parts)

