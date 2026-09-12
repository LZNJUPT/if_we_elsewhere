# -*- coding: utf-8 -*-
"""Telegram Desktop 官方导出 JSON 适配器（v0.2 O-5c）。

源：Telegram Desktop → Settings → Advanced → Export chat history →
Machine-readable JSON（result.json）。官方功能导出，无合规争议；
本项目只消费该已导出文件，不接触 Telegram 客户端/数据库。

导出结构要点（官方 export 格式）：
    {"name": ..., "type": "personal_chat"|"private_group"|...,
     "messages": [{"id":1, "type":"message", "date":"2024-09-11T09:30:00",
                   "from":"显示名", "from_id":"user123", "text": ...,
                   "photo"|"sticker"|"voice_message"|"video_file"|"file": ...}]}

字段 → canonical 映射表（类型映射写死，未列出的一律跳过+计数，不猜测归类）：
    from_id(缺则 actor_id)    -> accountName；再缺则 from/actor 显示名；
                                 out 消息两者皆缺时合成 "[outgoing]"
    date                      -> timestamp（ISO8601；带偏移按原偏移，裸日期按东八区，
                                 与项目 v1 时区约定一致）
    text                      -> type 0, content=拼接文本
    photo / sticker           -> type 7（content "[图片]"/贴纸文件名）
    video_file / file         -> type 4, content "[文件] <名>"
    附件行若带 text 图注       -> 拆成两条 canonical（媒体 + 文本），id 加后缀
    type:"service" 行          -> 跳过 + 计数
    带 message_edited 字段的行 -> 跳过 + 计数（编辑事件，不消费）
    voice_message             -> 跳过 + 计数（canonical 无语音类型，与 wecomsg 一致，
                                 不猜测归类——计划文本将其列入 7/4 映射，此处从红线规则）
    发送者 >2 人               -> 按"双人判定"保留消息最多的两位，
                                 其余发送者的行跳过 + 计数（群聊/杂入行）

合规立场：只消费用户已合法导出的 JSON 文件；不解析任何 IM 数据库。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from importers.base import TYPE_FILE, TYPE_IMAGE_EMOJI, TYPE_TEXT, canonical_message

TZ8 = timezone(timedelta(hours=8))

# 有对应 canonical 类型的附件键在 _media_of 里处理；以下键存在则该行跳过+计数
# （canonical 无语音/贴纸动画/投票等类型，不猜测归类）
_UNSUPPORTED_KEYS = ("voice_message", "animation", "video_note", "poll",
                     "location", "contact", "dice", "game", "invoice", "paid_media")

_DETECT_HEAD = 64 * 1024
_RE_MESSAGES = re.compile(r'"messages"\s*:')
_RE_SENDER_KEY = re.compile(r'"(from_id|actor_id)"\s*:')
_RE_CHAT_TYPE = re.compile(
    r'"type"\s*:\s*"(personal_chat|bot_chat|saved_messages|'
    r'private_group|public_supergroup|private_supergroup|channel)"')


def _parse_date(value) -> int | None:
    """ISO8601 → 秒级 unix；带时区偏移按原偏移，裸字符串按东八区。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ8)
    return int(dt.timestamp())


def _flatten_text(text) -> str:
    """text 可能是 string 或 [string, {"type":"link","text":...}, ...] 混合数组。
    顺序连接所有字符串元素与实体的 text 字段，忽略实体标注本身。"""
    if text is None:
        return ""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for el in text:
            if isinstance(el, str):
                parts.append(el)
            elif isinstance(el, dict):
                sub = el.get("text")
                if isinstance(sub, str):
                    parts.append(sub)
                elif isinstance(sub, list):        # 实体里再嵌数组（罕见）
                    parts.append(_flatten_text(sub))
        return "".join(parts)
    return ""


class TelegramJsonImporter:
    source_name = "telegram"

    def __init__(self) -> None:
        self.stats: dict = {}

    # ---- 协议方法 ----
    def detect(self, path: Path) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(_DETECT_HEAD)
        except (OSError, UnicodeDecodeError):
            return False
        head = head.lstrip()
        if not head.startswith("{"):
            return False
        if not _RE_MESSAGES.search(head):
            return False
        return bool(_RE_SENDER_KEY.search(head) or _RE_CHAT_TYPE.search(head))

    def parse(self, path: Path) -> list[dict]:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            messages = []
        self.stats = {"source_name": self.source_name, "total_rows": len(messages),
                      "chat_type": (data.get("type") if isinstance(data, dict) else None),
                      "parsed": 0, "skipped_service": 0, "skipped_edited": 0,
                      "skipped_unknown_type": 0, "skipped_no_time": 0,
                      "skipped_group": 0, "unknown_senders": []}

        # 第一遍：统计发送者（双人判定）
        sender_count: dict[str, int] = {}
        for m in messages:
            if not isinstance(m, dict) or m.get("type") != "message":
                continue
            acct = self._account_of(m)
            if acct:
                sender_count[acct] = sender_count.get(acct, 0) + 1
        keep = set()
        if len(sender_count) > 2:
            ranked = sorted(sender_count.items(), key=lambda kv: -kv[1])
            keep = {a for a, _ in ranked[:2]}
            self.stats["unknown_senders"] = [a for a, _ in ranked[2:]]

        out: list[dict] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("type") == "service":
                self.stats["skipped_service"] += 1
                continue
            if "message_edited" in m:
                self.stats["skipped_edited"] += 1
                continue
            if m.get("type") != "message":
                self.stats["skipped_unknown_type"] += 1
                continue
            ts = _parse_date(m.get("date"))
            if ts is None:
                self.stats["skipped_no_time"] += 1
                continue
            acct = self._account_of(m)
            if len(sender_count) > 2 and acct not in keep:
                self.stats["skipped_group"] += 1
                continue
            if any(k in m for k in _UNSUPPORTED_KEYS):
                self.stats["skipped_unknown_type"] += 1
                continue

            text = _flatten_text(m.get("text"))
            media_type, media_content = self._media_of(m)
            msg_id = str(m.get("id") or "")
            if media_type is not None:
                out.append(canonical_message(ts, media_type, media_content, acct,
                                             platform_message_id=f"{msg_id}-m" if msg_id else None))
                self.stats["parsed"] += 1
                if text:                      # 媒体 + 图注 → 媒体、文本各一条
                    out.append(canonical_message(ts, TYPE_TEXT, text, acct,
                                                 platform_message_id=f"{msg_id}-t" if msg_id else None))
                    self.stats["parsed"] += 1
            else:
                out.append(canonical_message(ts, TYPE_TEXT, text, acct,
                                             platform_message_id=msg_id or None))
                self.stats["parsed"] += 1
        return out

    # ---- 内部工具 ----
    @staticmethod
    def _account_of(m: dict) -> str:
        acct = m.get("from_id") or m.get("actor_id") or ""
        if not acct:
            acct = m.get("from") or m.get("actor") or ""
        if not acct and m.get("out"):
            acct = "[outgoing]"
        return str(acct)

    @staticmethod
    def _media_of(m: dict) -> tuple[int | None, str]:
        """附件 → (canonical type, content)；voice_message 返回 None（跳过在调用侧计数）。"""
        if m.get("photo"):
            return TYPE_IMAGE_EMOJI, "[图片]"
        if m.get("sticker"):
            return TYPE_IMAGE_EMOJI, Path(str(m["sticker"]).replace("\\", "/")).name
        for key in ("video_file", "file"):
            if m.get(key):
                name = m.get("file_name") or Path(str(m[key]).replace("\\", "/")).name
                return TYPE_FILE, f"[文件] {name}"
        return None, ""


# 模块导入即登记进格式注册表
from importers import registry as _registry            # noqa: E402
_registry.register(TelegramJsonImporter())
