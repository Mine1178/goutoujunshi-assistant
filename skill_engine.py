# -*- coding: utf-8 -*-
"""狗头军师技能运行时：加载 SKILL.md 内核、解析路由、按需取参考、组装提示词。

设计原则：
1. 技能目录只读，不做任何改写；
2. 上下文预算硬约束，避免把 30 万字知识库整包塞进模型；
3. 说话人映射显式标注，不做"按左右猜"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_REF_CHARS = 8000      # 参考材料总预算
MAX_LOG_CHARS = 8000      # 聊天记录预算（首尾保留，中间省略）
MAX_CORE_CHARS = 12000    # 内核预算


def truncate_middle(text: str, budget: int) -> str:
    """超长时保留开头与结尾：开头有语境，结尾是"刚刚发生的事"。"""
    if len(text) <= budget:
        return text
    head = text[:1000]
    tail = text[-(budget - 1000):]
    return f"{head}\n…（中间省略 {len(text) - 1000 - len(tail)} 字）…\n{tail}"


@dataclass
class SkillBundle:
    core: str
    routes: list[dict]
    root: Path


@dataclass
class PromptPack:
    system: str
    user: str
    refs: list[str] = field(default_factory=list)
    target: str = ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _strip_front_matter(text: str) -> str:
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)


SPLIT_RE = re.compile(r"[、，,；;/｜|]|\s{2,}")
NOISE_RE = re.compile(r"^(先读|要落地时再读|必要时再读|再读|需要时读)$")


def _keywords(cell: str) -> list[str]:
    """把『当前问题』列拆成关键词。"""
    cell = cell.replace("`", "")
    parts = [p.strip() for p in SPLIT_RE.split(cell) if p.strip()]
    out = []
    for p in parts:
        if NOISE_RE.match(p):
            continue
        # 去掉括号补充说明，保留主词
        p = re.sub(r"（[^）]*）|\([^)]*\)", "", p).strip()
        if len(p) >= 2:
            out.append(p)
    return out


def load_skill(root: Path) -> SkillBundle:
    core_path = root / "SKILL.md"
    core = _strip_front_matter(_read(core_path))
    if not core.strip():
        raise FileNotFoundError(f"找不到技能内核：{core_path}")
    core = core[:MAX_CORE_CHARS]
    return SkillBundle(core=core, routes=parse_routes(core), root=root)


_GENERIC_NAMES = {"对方", "他", "她", "ta", "TA", "对方本人", "对方（", "A", "B"}
_HINT_FILES = ["references/practical/实战话术编排器：从一句回复到后续分支.md"]

# 技能表里的『当前问题』是书面分类词，真实聊天里用的是口语。
# 这里补一层同义扩展，命中率比改技能原文更高，也不影响技能只读。
EXTRA_KEYWORDS = {
    "一句话回复": ["怎么回", "回复", "回她", "回他", "说什么", "开场", "开场白", "接话", "冷场"],
    "松弛感": ["松弛", "自然点", "别太用力", "轻松"],
    "Blueprint": ["内在状态", "自然流", "mystery", "社交体系"],
    "PUA": ["pua", "操控", "贬低", "推拉", "服从测试", "煤气灯"],
    "聊天截图": ["截图", "聊天记录", "微信记录", "导出记录", "聊天导出", "OCR"],
    "投入失衡": ["不回消息", "已读不回", "冷淡", "忽冷忽热", "不主动", "一直我主动", "投入", "不理我"],
    "依恋": ["没有安全感", "患得患失", "粘人", "焦虑", "回避"],
    "MBTI": ["mbti", "intj", "infp", "enfp", "isfj", "人格"],
    "冲突修复": ["吵架", "冷战", "道歉", "和好", "矛盾", "闹别扭", "生气"],
    "同意": ["接吻", "亲密", "上床", "边界", "越界", "拒绝"],
    "婚姻家庭": ["结婚", "彩礼", "婆媳", "家务", "育儿", "离婚", "怀孕"],
    "分手背叛": ["分手", "复合", "出轨", "背叛", "劈腿", "挽回", "信任崩"],
    "主动表达": ["表白", "约她", "约他", "邀约", "约会", "第一次见面", "见面"],
    "家暴": ["家暴", "跟踪", "威胁", "强迫", "人身安全", "报警"],
}


def parse_routes(core: str) -> list[dict]:
    """解析 SKILL.md 中『按需加载』表：当前问题 -> 参考文件。

    表格形如：
        | 当前问题 | 必读或优先读取 |
        | --- | --- |
        | 一句话回复、开场、邀约、演练 | `references/practical/xxx.md` |

    部分行的右列不写路径、只写『知识 11、12、15』，在 SkillEngine 初始化时再展开。
    """
    routes: list[dict] = []
    for line in core.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        left, right = cells
        if set(left) <= set("-: ") or left in ("当前问题", "Context"):
            continue
        files = re.findall(r"`([^`]+)`", right)
        if not files and not re.search(r"知识\s*\d", right):
            continue
        routes.append({"trigger": left, "keywords": _keywords(left),
                       "files": files, "raw": right})
    return routes


class SkillEngine:
    def __init__(self, root: Path):
        self.bundle = load_skill(root)
        self._expand_routes()

    def _expand_routes(self) -> None:
        """把『知识 11、12、15』这类纯数字引用展开成真实文件路径。"""
        kb = self.bundle.root / "references" / "knowledge"
        for r in self.bundle.routes:
            if r["files"]:
                continue
            files: list[str] = []
            for num in re.findall(r"知识\s*([\d、,，\s]+)", r.get("raw", "")):
                for n in re.findall(r"\d+", num):
                    hits = sorted(kb.glob(f"{int(n):02d}-*.md"))
                    if hits:
                        files.append(hits[0].relative_to(self.bundle.root).as_posix())
            r["files"] = files
        for r in self.bundle.routes:
            for key, extra in EXTRA_KEYWORDS.items():
                if key in r["trigger"]:
                    r["keywords"] = r["keywords"] + [w for w in extra if w not in r["keywords"]]

    # ---------- 路由 ----------
    def route(self, query: str, top: int = 3) -> list[str]:
        scored = []
        for r in self.bundle.routes:
            score = 0.0
            for kw in r["keywords"]:
                if not kw:
                    continue
                if kw in query:
                    score += len(kw)  # 长词命中权重更高
                else:
                    # 短语未整词命中时，用二元组做部分匹配（如"家务"命中"金钱家务育儿"）
                    grams = [kw[i:i + 2] for i in range(len(kw) - 1)]
                    hit = sum(1 for g in grams if g in query)
                    score += 0.6 * hit
            if score >= 0.5:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        picked: list[str] = []
        for _, r in scored[:top]:
            for f in r["files"]:
                if f not in picked:
                    picked.append(f)
        # 只要是在求"怎么回"，就把话术编排器垫在最前面；没有命中任何路由时也用它兜底
        reply_intent = ("怎么回", "回这条", "帮我回", "回复", "回我", "回她", "回他", "该回", "说点什么")
        if any(k in query for k in reply_intent):
            for h in _HINT_FILES:
                if h not in picked and (self.bundle.root / h).exists():
                    picked.insert(0, h)
        return picked

    def read_refs(self, files: list[str], budget: int = MAX_REF_CHARS) -> list[tuple[str, str]]:
        """按预算截断读取参考文件，超预算时只保留文件前段。"""
        out: list[tuple[str, str]] = []
        used = 0
        for rel in files:
            p = self.bundle.root / rel
            if not p.exists():
                continue
            text = _read(p)
            if not text:
                continue
            room = budget - used
            if room < 600:
                break
            if len(text) > room:
                text = text[:room] + "\n\n…（后续内容因上下文预算省略）"
            used += len(text)
            out.append((rel, text))
        return out

    # ---------- 说话人映射 ----------
    def map_speakers(self, raw: str, my_names: list[str]) -> tuple[str, str]:
        """把微信复制出来的聊天文本标注成 [我]/[对方]。

        返回 (标注后的文本, 推断出的对方昵称)。
        """
        raw = raw.replace("\r\n", "\n").strip()
        if not raw:
            return "", ""

        my_keys = [n.strip() for n in my_names if n and n.strip()]
        # 整段就一句、且没有昵称前缀，视为"对方刚说的那句"
        blocks = self._split_blocks(raw)
        if not blocks:
            return f"[对方] {raw}", ""

        target = ""
        lines: list[str] = []
        for name, content in blocks:
            clean = name.strip() if name else ""
            if not clean or clean in _GENERIC_NAMES:
                tag = "[对方]"
            elif my_keys and any(k.lower() in clean.lower() or clean.lower() in k.lower() for k in my_keys):
                tag = "[我]"
            else:
                tag = f"[对方·{clean[:10]}]"
                target = target or clean
            content = content.strip()
            if not content:
                continue
            lines.append(f"{tag} {content}")
        if not lines:
            return f"[对方] {raw}", ""
        return "\n".join(lines), target

    TIME_HEAD = re.compile(
        r"^(?P<name>[^\s\d:：]{1,16})\s+(?:"
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}\s*)?\d{1,2}:\d{2}(?::\d{2})?$"
    )
    INLINE = re.compile(r"^(?P<name>[^:：\s]{1,12})[:：]\s*(?P<text>.*)$")

    @classmethod
    def _split_blocks(cls, raw: str) -> list[tuple[str, str]]:
        """兼容微信复制出来的几种文本形态：
        1) 小雅: 你好                （昵称与内容同行）
        2) 小雅:\n你好                （昵称独占一行）
        3) 小雅 2026-11-23 20:15\n你好  （昵称带时间）
        没有昵称前缀的部分，归到上一位说话人。
        """
        blocks: list[tuple[str, str]] = []
        cur = ""
        buf: list[str] = []

        def flush():
            if buf or cur:
                blocks.append((cur, "\n".join(buf).strip()))
            buf.clear()

        for line in raw.replace("\r", "").split("\n"):
            s = line.rstrip()
            stripped = s.strip()
            if not stripped:
                continue

            m_time = cls.TIME_HEAD.match(stripped)
            if m_time and len(m_time.group("name")) <= 12:
                flush()
                cur = m_time.group("name").strip()
                continue

            if "://" not in stripped:
                m = cls.INLINE.match(stripped)
                if m and len(m.group("name")) <= 12:
                    flush()
                    cur = m.group("name").strip()
                    if m.group("text").strip():
                        buf.append(m.group("text").strip())
                    continue

            buf.append(stripped)
        flush()
        return blocks

    # ---------- 两段式路由 ----------
    def categories(self) -> list[str]:
        return [r["trigger"] for r in self.bundle.routes]

    def classify_prompt(self, log: str, ask: str = "") -> list[dict]:
        """先让模型判断"这属于哪一类问题"，再按类去读知识库。"""
        cats = self.categories()
        listing = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(cats))
        system = (
            "你是意图分类器。根据聊天记录和用户诉求，从编号列表里选出唯一最相关的一类。\n"
            "只输出那个编号数字，不要任何解释、标点或其它字符；都不相关就输出 0。\n\n"
            f"{listing}"
        )
        user = f"聊天记录：\n{truncate_middle(log, 1500)}\n\n用户诉求：{ask or '帮我回这条'}"
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    @staticmethod
    def parse_category(text: str) -> int:
        nums = re.findall(r"\d+", text or "")
        if not nums:
            return 0
        try:
            return int(nums[0])
        except ValueError:
            return 0

    # ---------- 效果自动判定 ----------
    def judge_prompt(self, draft: str, reply: str, context: str = "") -> list[dict]:
        system = (
            "你是关系对话的效果判定器。给出一条已经发出去的话术，以及对方随后的真实回复，"
            "判断这条话术实际起到的效果。\n"
            "只输出三段，不要任何额外解释：\n"
            "第一行：有效 / 一般 / 反效果 三个词中的一个\n"
            "第二行：不超过 15 个字的理由\n"
            "判定口径：对方接话、情绪缓和、给下一步（时间/问题/主动关心）= 有效；"
            "短促应答但没有推进或退场（好/嗯/随便/睡了）= 一般；"
            "更冷、指责、拉黑、明确拒绝 = 反效果。"
        )
        user = f"""我方发出的话术：
{draft}

对方随后的真实回复：
{reply}

{('更早的上下文：' + context[:800]) if context else ''}"""
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    @staticmethod
    def parse_verdict(text: str) -> tuple[str, str]:
        """把判定结果映射成 good / so-so / bad + 理由。"""
        raw = (text or "").strip()
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        first = lines[0] if lines else ""
        reason = lines[1] if len(lines) > 1 else ""
        if "反效果" in first or "反效" in first or "负面" in first:
            rating = "bad"
        elif "有效" in first or first.lower().startswith(("good", "positive")):
            rating = "good"
        elif "一般" in first or "中性" in first:
            rating = "so-so"
        else:
            lowered = raw.lower()
            if "反效果" in raw:
                rating = "bad"
            elif "有效" in raw:
                rating = "good"
            else:
                rating = "so-so"
        return rating, reason[:30]

    def route_by_category(self, idx: int, query: str) -> list[str]:
        files: list[str] = []
        cats = self.bundle.routes
        if 1 <= idx <= len(cats):
            files = list(cats[idx - 1]["files"])
        if any(k in query for k in ("怎么回", "回这条", "帮我回", "回复", "回我", "回她", "回他", "该回", "说点什么")):
            for h in _HINT_FILES:
                if h not in files and (self.bundle.root / h).exists():
                    files.insert(0, h)
        return files

    # ---------- 提示词 ----------
    def build_prompt(self, log: str, ask: str, profile_ctx: str = "",
                     extra_style: str = "", style_ctx: str = "",
                     files: list[str] | None = None) -> PromptPack:
        query = (f"{log}\n{ask}")[:4000]
        picked = files if files is not None else self.route(query, top=2)
        refs = self.read_refs(picked)
        target = ""
        m = re.search(r"\[对方[·:：]([^\]\n]{1,20})\]", log)
        if m:
            target = m.group(1).strip()[:20]

        ref_block = "\n\n".join(
            f"### 参考：{Path(rel).stem}\n{text}" for rel, text in refs
        ) or "（本次未命中知识库片段，按内核原则处理）"

        system = f"""{self.bundle.core}

---

## 本次运行环境（PC 热键助手）

你现在不是一个聊天机器人，而是用户按快捷键唤起、只有几秒耐心的"回复军师"。遵守以下渠道约定：

1. 先自己完成说话人映射：`[我]` 是用户本人，`[对方·昵称]`（或只有 `[对方]`）是对方。若某句归属不确定，在该句后标 `(待确认)`，不要静默猜测。
2. 输出严格用四段，段内不要空行堆砌，总长度控制在 350 字以内：
   **【一句判断】** 不超过 40 字，说清这段关系的当前态势。
   **【可直接发送】** 只写能直接发出去的那一条消息本身：不要解释、不要加引号、不要写"你可以回复"、不要写多个版本。
   **【为什么】** 不超过 3 句，说清这样发能达到什么效果、代价是什么。
   **【观察点】** 一个可验证的反馈信号，一个该停下来的条件。
3. 默认只给一条成品。用户点"换一版"时才换风格，且保持同样的判断，不要前后矛盾。
4. 硬边界不变：不代发、不诱导操控、不做服从性测试、不编造对方心理；遇到家暴、跟踪、胁迫、自伤等，直接给安全建议并放弃"怎么回"。
5. 若聊天记录信息不足以判断，就在【一句判断】里明说"信息不足"，并在【可直接发送】给一条低风险、可退出的中性回复。

## 用户自填档案与长期记忆
{profile_ctx or "（未填写。可按行为判断，不要自行编造 MBTI 或评分。）"}

## 对方说话特征（统计自真实聊天，用来校准语气与长度，不要机械模仿）
{style_ctx or "（暂无历史聊天，按通用语气处理，但保持口语、短句。）"}

## 参考材料（技能知识库，只读，不要整段复述）
{ref_block}
"""

        user = f"""聊天记录（已标注双方身份）：

{truncate_middle(log, MAX_LOG_CHARS)}

用户诉求：{ask or "帮我回这条，我需要能直接发出去的话"}
{extra_style}
"""
        return PromptPack(system=system, user=user, refs=[r for r, _ in refs], target=target)


SECTION_RE = re.compile(r"\*\*【([^】]+)】\*\*|【([^】]+)】")


def split_sections(text: str) -> tuple[dict[str, str], str]:
    """把模型输出拆成四段，并抽出可直接发送的草稿。"""
    text = (text or "").strip()
    if not text:
        return {}, ""
    marks = [(m.start(), m.end(), (m.group(1) or m.group(2))) for m in SECTION_RE.finditer(text)]
    if not marks:
        return {}, text
    sections: dict[str, str] = {}
    for i, (_, end, name) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        sections[name.strip()] = text[end:nxt].strip()
    draft = ""
    for key, val in sections.items():
        if "发送" in key or "话术" in key:
            draft = val
            break
    if not draft:
        draft = next(iter(sections.values()), "")
    # 去掉模型偶发的包裹符号
    draft = re.sub(r"^[「『“\"']+|[」』”\"']+$", "", draft.strip())
    draft = re.sub(r"^(可以这样回|建议回复|回复)[:：]\s*", "", draft)
    return sections, draft
