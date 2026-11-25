# -*- coding: utf-8 -*-
"""微信聊天文本解析：把 PC 微信"多选消息→复制"或导出的 txt 变成结构化消息。

只做解析，不读微信数据库，不做任何客户端 hook。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

DATE_SEP = re.compile(r"^[\s\-—=]{2,}\s*(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})[日]?\s*[\s\-—=]{2,}$")
TIME_HEAD = re.compile(
    r"^([^\s:：]{1,16})\s+(?:((\d{4})[./-](\d{1,2})[./-](\d{1,2}))\s+)?"
    r"(\d{1,2}:\d{2}(?::\d{2})?)$"
)
INLINE = re.compile(r"^([^\s:：][^:：]{0,15})[:：]\s*(.*)$")
NAME_ONLY = re.compile(r"^([^\s:：]{1,16})[:：]$")
SYSTEM_NOISE = re.compile(r"(以下为新消息|撤回了一条消息|加入了群聊|邀请.*加入了|拍了拍)")
# 单独一行的「2026年11月24日 0:00」——常见于 txt 导出（说话人独占一行、时间独占一行）
DATETIME_LINE = re.compile(
    r"^((\d{4})[年./-](\d{1,2})[月./-](\d{1,2})[日]?)\s+(\d{1,2}:\d{2}(?::\d{2})?)$"
)
DATE_ONLY_LINE = re.compile(r"^((\d{4})[年./-](\d{1,2})[月./-](\d{1,2})[日]?)$")
# 单独一行的说话人：不含空格/冒号，1~16 字
PLAIN_NAME = re.compile(r"^[^\s:：]{1,16}$")
TEXT_HINT = re.compile(r"[，。！？,.!?；;…~、（）()\[\]【】\"“”]")
# 程序自己生成的标注格式：`2026-11-23 23:57 [我] 内容` / `[对方·晶] 内容`
ANNOTATED = re.compile(
    r"^(?:(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s+)?"
    r"\[(我|对方·[^\]]+)\]\s*(.*)$"
)


def _looks_like_name(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 16 or " " in s:
        return False
    if not PLAIN_NAME.match(s):
        return False
    if TEXT_HINT.search(s):          # 带标点的一律当正文
        return False
    if s.isdigit():
        return False
    return True


@dataclass
class Msg:
    speaker: str
    role: str          # me / them / unknown
    text: str
    ts: str = ""
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _norm_date(y: str, m: str, d: str) -> str:
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return ""


def parse_wechat(text: str, my_names: list[str] | None = None) -> dict:
    """返回 {messages, speakers, target, stats, raw_chars}。"""
    my_keys = [n.strip().lower() for n in (my_names or []) if n and n.strip()]
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    msgs: list[Msg] = []
    cur_date = ""
    cur: Msg | None = None
    pending_speaker = ""
    pending_ts = ""

    def role_of(name: str) -> str:
        low = (name or "").lower()
        if low and my_keys and any(k in low or low in k for k in my_keys):
            return "me"
        return "them"

    def start(speaker: str, ts: str = "", first_text: str = ""):
        nonlocal cur
        cur = Msg(speaker=speaker, role=role_of(speaker), text=first_text, ts=ts)
        msgs.append(cur)

    for idx, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if SYSTEM_NOISE.search(stripped):
            continue

        # 程序自己标注过的记录（[我]/[对方·昵称]）直接按角色还原，
        # 否则二次解析会把标注当成正文，过滤时整段被误删。
        m = ANNOTATED.match(stripped)
        if m:
            ts = m.group(1) or ""
            tag = m.group(2)
            body = m.group(3).strip()
            if tag == "我":
                start("我", ts, body)
                if cur is not None:
                    cur.role = "me"
            else:
                speaker = tag.split("·", 1)[1].strip()
                start(speaker, ts, body)
                if cur is not None:
                    cur.role = "them"
            continue

        m = DATE_SEP.match(stripped)
        if m:
            cur_date = _norm_date(m.group(1), m.group(2), m.group(3))
            continue

        # 「2026年11月24日 0:00」独占一行：时间戳行，正文另起一行
        m = DATETIME_LINE.match(stripped)
        if m:
            cur_date = _norm_date(m.group(2), m.group(3), m.group(4))
            ts = f"{cur_date} {m.group(5)}"
            if pending_speaker:
                start(pending_speaker, ts)
                pending_speaker = ""
            elif cur is not None and not cur.ts:
                cur.ts = ts
            else:
                pending_ts = ts          # 说话人还没出现，先记下时间
            continue

        m = DATE_ONLY_LINE.match(stripped)
        if m:
            cur_date = _norm_date(m.group(1), m.group(2), m.group(3))
            continue

        # 独占一行的说话人（下面紧跟一行时间戳才算）
        if _looks_like_name(stripped):
            nxt = ""
            for j in range(idx + 1, len(lines)):
                if lines[j].strip():
                    nxt = lines[j].strip()
                    break
            if DATETIME_LINE.match(nxt):
                pending_speaker = stripped
                continue

        m = TIME_HEAD.match(stripped)
        if m:
            name = m.group(1).strip()
            if m.group(2):
                cur_date = _norm_date(m.group(3), m.group(4), m.group(5))
            start(name, f"{cur_date} {m.group(6)}".strip())
            pending_ts = ""
            continue

        if "://" not in stripped:
            m = INLINE.match(stripped)
            if m and len(m.group(1)) <= 16:
                cur_date = cur_date if cur_date else ""
                start(m.group(1).strip(), cur_date, m.group(2).strip())
                continue

            m = NAME_ONLY.match(stripped)
            if m:
                pending_speaker = m.group(1).strip()
                start(pending_speaker, cur_date)
                continue

        if cur is None:
            start(pending_speaker, pending_ts or cur_date, stripped)
            pending_ts = ""
            continue
        cur.text = (cur.text + "\n" + stripped).strip() if cur.text else stripped

    # 过滤空消息
    msgs = [x for x in msgs if x.text.strip()]

    # 没有识别出任何昵称时，整段当"对方刚说的话"
    if not msgs:
        body = text.strip()
        if body:
            msgs = [Msg(speaker="", role="them", text=body)]

    speakers: dict[str, int] = {}
    for x in msgs:
        key = x.speaker or "（未署名）"
        speakers[key] = speakers.get(key, 0) + 1

    # 对方昵称：出现最多、且不是自己的那个
    target = ""
    cand = [(n, c) for n, c in speakers.items()
            if n and n not in ("（未署名）", "我")]
    for name, _ in cand:
        if my_keys and any(k in name.lower() or name.lower() in k for k in my_keys):
            continue
        if not target or speakers[name] > speakers[target]:
            target = name

    stats = _stats(msgs)
    return {
        "messages": [m.as_dict() for m in msgs],
        "speakers": speakers,
        "target": target,
        "stats": stats,
        "raw_chars": len(text),
    }


def _stats(msgs: list[Msg]) -> dict:
    me = [m for m in msgs if m.role == "me"]
    them = [m for m in msgs if m.role == "them"]
    out = {
        "total": len(msgs),
        "me_count": len(me),
        "them_count": len(them),
        "me_chars": sum(len(m.text) for m in me),
        "them_chars": sum(len(m.text) for m in them),
    }
    lat = _reply_latency(msgs)
    if lat:
        out["avg_reply_min"] = round(lat, 1)
    return out


def _reply_latency(msgs: list[Msg]) -> float | None:
    """对方 → 我 的平均回复间隔（分钟），需要时间戳。"""
    gaps = []
    prev = None
    for m in msgs:
        if not m.ts:
            prev = None
            continue
        try:
            t = datetime.strptime(m.ts, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                t = datetime.strptime(m.ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                prev = None
                continue
        if prev and prev.role == "them" and m.role == "me":
            delta = (t - prev.t).total_seconds() / 60
            if 0 <= delta <= 1440:
                gaps.append(delta)
        prev = type("P", (), {"t": t, "role": m.role})
    if not gaps:
        return None
    return sum(gaps) / len(gaps)


def keep_related(messages: list[dict], name: str, my_names: list[str],
                 aliases: list[str] | None = None) -> tuple[list[dict], int]:
    """只保留『我』和这个对象说的话，把记录里其他人的消息剔掉。

    返回 (保留下来的消息, 被剔掉的条数)。name/aliases 里任一个匹配都算这个对象。
    """
    allowed = {str(x).strip() for x in ([name] + list(aliases or [])) if str(x).strip()}
    mine = {str(x).strip() for x in (my_names or []) if str(x).strip()}
    kept, dropped = [], 0
    for m in messages:
        if m.get("role") == "me":
            kept.append(m)
            continue
        sp = str(m.get("speaker") or "").strip()
        if sp in allowed or sp in mine:
            kept.append(m)
        else:
            dropped += 1
    return kept, dropped


def to_annotated(messages: list[dict], max_chars: int = 8000) -> str:
    """转成模型能读的 [我]/[对方·昵称] 文本，超长时保留首尾。"""
    lines = []
    for m in messages:
        tag = "[我]" if m.get("role") == "me" else f"[对方·{m.get('speaker') or '未知'}]"
        text = (m.get("text") or "").replace("\n", " / ")
        if m.get("ts"):
            lines.append(f"{m['ts']} {tag} {text}")
        else:
            lines.append(f"{tag} {text}")
    body = "\n".join(lines)
    if len(body) <= max_chars:
        return body
    head = body[:900]
    tail = body[-(max_chars - 900):]
    dropped = len(body) - len(head) - len(tail)
    return f"{head}\n…（中间省略 {dropped} 字）…\n{tail}"
