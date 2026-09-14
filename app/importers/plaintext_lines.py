# -*- coding: utf-8 -*-
"""纯文本行流导入器（v0.3 · 数据规范 v2）：.txt / .md / .log 之类的「人类可读」导出。

与 JSONL / CSV 不同，这类文件**没有结构化字段**，时间与发送者只能靠行内启发式识别。
因此本适配器遵守三条纪律：

1. **只认锚定的时间戳**：必须能在行首附近匹配到「有年份的日期」，绝不猜测
   「昨天」「上午十点」这类相对时间（拿不到绝对时间就不导入，宁可少不可错）。
2. **解析不了的行走「续行」或「跳过」，绝不猜归类**：
   - 紧跟在成功解析行之后、自身无时间戳的行 → 视为上一条消息的**续行**（聊天导出
     普遍会软换行），计数 `appended_lines`；
   - 其余无时间戳的行 → 跳过并计数，报告里如实给出。
3. **无法对应 canonical 类型的媒体行跳过并计数**（语音/视频等），
   其余中括号标注（[图片] / [表情包] / [文件] / [转账]）映射到对应类型。

编码：BOM → utf-8 → gb18030（中文聊天记录导出常见 GBK 系），探测结果进报告。
docx 适配器把段落/表格行拍平成同一套行流后复用本模块的解析（见 docx_text.py）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from importers.base import (TYPE_FILE, TYPE_IMAGE_EMOJI, TYPE_TEXT,
                            TYPE_TRANSFER, canonical_message)

TZ8 = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 时间戳
# 只识别「带年份的绝对日期」（可带时分秒）。年/月/日与 时:分(:秒) 都支持多种分隔符。
TS_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("Y-M-D H:M:S", re.compile(
        r"(?P<y>\d{4})[-/.年](?P<mo>\d{1,2})[-/.月](?P<d>\d{1,2})[日]?"
        r"(?:[T\s]+(?P<h>\d{1,2}):(?P<mi>\d{1,2})(?::(?P<s>\d{1,2}))?)?")),
)
_TS = TS_PATTERNS[0][1]
_TS_LOOSE = re.compile(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}")   # 快速预筛

# ---------------------------------------------------------------- 发送者
WHO_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\*\*(?P<who>[^*\n]{1,32})\*\*\s*[:：]\s*(?P<text>.*)$"),        # **张三**: 内容
    re.compile(r"^[【\[](?P<who>[^】\]\n]{1,32})[】\]]\s*[:：]?\s*(?P<text>.*)$"),  # 【张三】内容 / [张三]: 内容
    re.compile(r"^(?P<who>[^:：\t\n]{1,32}?)\s*[:：]\s*(?P<text>.*)$"),           # 张三: 内容
    re.compile(r"^(?P<who>[^\t\n]{1,32}?)\t+\s*(?P<text>.*)$"),                  # 表格：张三<TAB>内容
)

# 中括号标注 → canonical 类型 / 跳过判定
_MEDIA_MAP = (
    ("[图片]", TYPE_IMAGE_EMOJI),
    ("[表情包]", TYPE_IMAGE_EMOJI),
    ("[动画表情]", TYPE_IMAGE_EMOJI),
    ("[表情]", TYPE_IMAGE_EMOJI),
    ("[文件]", TYPE_FILE),
    ("[转账]", TYPE_TRANSFER),
)
# canonical 无对应类型 → 跳过并计数（与 wecomsg / telegram 适配器一致）
_SKIP_MARKS = ("[语音]", "[视频]", "[位置]", "[语音消息]", "[视频通话]", "[视频消息]",
               "[链接]", "[分享]", "[音乐]", "[卡券]", "[红包]")

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


def _to_ts(m: re.Match) -> int | None:
    """正则命中 → 秒级 unix（裸时间按东八区，与项目 v1 约定一致）；非法值返回 None。"""
    try:
        y, mo, d = int(m.group("y")), int(m.group("mo")), int(m.group("d"))
        h = int(m.group("h") or 0)
        mi = int(m.group("mi") or 0)
        s = int(m.group("s") or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        return None
    try:
        return int(datetime(y, mo, d, h, mi, s, tzinfo=TZ8).timestamp())
    except ValueError:                    # 例如 2 月 30 日
        return None


def parse_line(line: str) -> tuple[int, str, str] | None:
    """单行 → (ts, 发送者, 内容)；识别不出锚定时间戳返回 None。

    兼容两种列序：`时间 发送者: 内容`（常见）与 `发送者 时间 内容`（部分表格导出）。
    """
    m = _TS.search(line)
    if not m:
        return None
    ts = _to_ts(m)
    if ts is None:
        return None
    rest = line[m.end():].lstrip(" \t]】").strip()
    for pat in WHO_PATTERNS:
        wm = pat.match(rest)
        if wm:
            who = wm.group("who").strip(" \t*_`")
            return ts, who, (wm.group("text") or "").strip()
    # 发送者在时间戳之前（如表格列序 发送者 | 时间 | 内容）
    pre = line[:m.start()].strip(" \t[【|")
    if pre and len(pre) <= 32 and not _TS_LOOSE.search(pre):
        return ts, pre.strip(" \t*_`"), rest
    return ts, "", rest


def _media_type(text: str) -> tuple[int, str]:
    """内容 → (canonical 类型, 是否跳过)。跳过项返回 (-1, True)。"""
    t = (text or "").strip()
    for mark in _SKIP_MARKS:
        if t.startswith(mark):
            return -1, True
    for mark, typ in _MEDIA_MAP:
        if t.startswith(mark):
            return typ, False
    return TYPE_TEXT, False


def parse_lines(lines: list[str]) -> tuple[list[dict], dict]:
    """行流 → canonical 消息列表 + 统计（docx 适配器复用此函数）。"""
    stats: dict = {"total_lines": 0, "parsed": 0, "appended_lines": 0,
                   "skipped_no_ts": 0, "skipped_unknown_type": 0,
                   "unknown_type_names": [], "template": TS_PATTERNS[0][0]}
    rows: list[dict] = []
    cur: dict | None = None
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stats["total_lines"] += 1
        if not line.strip():
            if cur is not None:                     # 空行留在消息内（保留段落感）
                cur["content"] = (cur["content"] + "\n").rstrip()
            continue
        got = parse_line(line)
        if got is None:
            if cur is not None:                     # 续行：并入上一条
                cur["content"] = (cur["content"] + "\n" + line.strip()).strip()
                stats["appended_lines"] += 1
            else:
                stats["skipped_no_ts"] += 1
            continue
        ts, who, text = got
        typ, skip = _media_type(text)
        if skip:
            stats["skipped_unknown_type"] += 1
            name = text.strip()[:12]
            if name not in stats["unknown_type_names"]:
                stats["unknown_type_names"].append(name)
            cur = None                              # 跳过项不承接续行
            continue
        cur = {"_type": "message", "platformMessageId": None, "timestamp": ts,
               "type": typ, "content": text, "accountName": who}
        rows.append(cur)
        stats["parsed"] += 1
    for r in rows:                                  # 去掉尾部空行
        r["content"] = (r["content"] or "").strip()
    return rows, stats


def read_text_lines(path: Path) -> tuple[list[str], str]:
    """按 BOM → utf-8 → gb18030 顺序读行；返回 (行列表, 命中的编码)。"""
    raw = Path(path).read_bytes()
    last = None
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
            return text.splitlines(), enc
        except (UnicodeDecodeError, UnicodeError) as e:
            last = e
    raise ValueError(f"文本编码无法识别（尝试 {'/'.join(ENCODINGS)} 均失败）: {last}")


class PlaintextLinesImporter:
    """txt / md / log 等纯文本聊天导出。注册序在最后（最宽松，作为兜底）。"""

    source_name = "plaintext"

    def __init__(self) -> None:
        self.stats: dict = {}

    def _head_lines(self, path: Path, limit: int = 80) -> list[str]:
        try:
            lines, _ = read_text_lines(path)
        except Exception:
            return []
        return [l for l in lines[:limit] if l.strip()]

    def detect(self, path: Path) -> bool:
        """保守命中：前 80 个非空行里至少有 3 行能解析出锚定时间戳，
        且其中至少 1 行带发送者标记。避免把普通散文/日志误当聊天记录。"""
        p = Path(path)
        if p.suffix.lower() not in (".txt", ".md", ".log", ".text", ".markdown"):
            return False
        head = self._head_lines(p)
        if not head:
            return False
        hits = [parse_line(l) for l in head]
        ok = [h for h in hits if h is not None]
        if len(ok) < 3:
            return False
        return any(who for _, who, _ in ok)

    def parse(self, path: Path) -> list[dict]:
        lines, enc = read_text_lines(path)
        rows, stats = parse_lines(lines)
        stats["source_name"] = self.source_name
        stats["encoding"] = enc
        self.stats = stats
        return rows


# 模块导入即登记进格式注册表
from importers import registry as _registry            # noqa: E402
_registry.register(PlaintextLinesImporter())
