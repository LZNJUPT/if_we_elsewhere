# -*- coding: utf-8 -*-
"""WeChatMsg（MemoTrace / 留痕）CSV 导出适配器（v0.2 O-5b）。

源格式实测结论（以 MemoTrace 当前稳定版 2.0.x 的 CSV 聊天记录导出为准，
字段结构与 WeClone 官方文档引用的 ChatMessage 模型一致）：

    id, MsgSvrID, type_name, is_sender, talker, room_name, msg, src, CreateTime

- id: 顺序号；MsgSvrID: 平台原始消息 id
- type_name: 消息类型中文名（文本/图片/动画表情/语音/视频/文件/音视频通话/…）
- is_sender: 0=对方发送, 1=自己发送
- talker: 发送者 id；room_name: 私聊=对方 id，群聊=xxx@chatroom
- msg/src: 消息文本 / 媒体路径；CreateTime: 秒级 unix 或 "YYYY-MM-DD HH:MM:SS"
- 旧版导出把 msg/src 合并在 content 列（JSON 字符串 {"src":...,"msg":...}），
  本 adapter 兼容两种列布局。

字段 → canonical 映射表（类型映射写死，未列出的一律跳过+计数，不猜测归类）：
    talker                    -> accountName（为空时按 is_sender 合成 "[is_sender=N]"）
    MsgSvrID(缺则 id)         -> platformMessageId
    CreateTime                -> timestamp（字符串按东八区解析为秒级 unix）
    文本          -> type 0, content=msg
    图片          -> type 7, content="[图片]"
    动画表情      -> type 7, content=src 文件名（无 src 则 "[动画表情]"）
    文件          -> type 4, content="[文件] <名>"
    音视频通话    -> type 23, content="[通话]"
    引用          -> type 25, content=msg 原文
    名片          -> type 27, content=msg 或 "[名片]"
    转账          -> type 99, content=msg 原文（金额由流水线提取）
    撤回          -> type 80, content=msg 或 "[撤回]"
    群聊行(room_name 含 @chatroom)          -> 跳过 + 计数（v1 仅双人对话）
    语音/视频/系统通知/红包/位置/卡片链接等  -> 跳过 + 计数（canonical 无对应类型）

合规立场：只消费用户已合法导出的 CSV 文件；不解析任何 IM 数据库。
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from importers.base import (TYPE_CALL, TYPE_FILE, TYPE_IMAGE_EMOJI, TYPE_NAMECARD,
                            TYPE_QUOTE_TEXT, TYPE_RECALL, TYPE_TEXT, TYPE_TRANSFER,
                            canonical_message)

TZ8 = timezone(timedelta(hours=8))   # 与 phase1_ingest.TZ 一致（项目 v1 固定东八区）

# type_name（及常见变体） -> canonical type
TYPE_NAME_MAP: dict[str, int] = {
    "文本": TYPE_TEXT,
    "图片": TYPE_IMAGE_EMOJI,
    "动画表情": TYPE_IMAGE_EMOJI,
    "表情包": TYPE_IMAGE_EMOJI,
    "文件": TYPE_FILE,
    "音视频通话": TYPE_CALL,
    "语音/视频通话": TYPE_CALL,
    "引用": TYPE_QUOTE_TEXT,
    "名片": TYPE_NAMECARD,
    "转账": TYPE_TRANSFER,
    "撤回": TYPE_RECALL,
    "撤回消息": TYPE_RECALL,
}

# 必需列（存在其一布局即判定命中）
_REQUIRED_COLS = {"type_name", "is_sender", "talker", "createtime"}

DATETIME_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(:\d{2})?$")


def _read_csv_rows(path: Path) -> tuple[list[dict], str]:
    """编码探测 utf-8-sig → utf-8 → gbk，返回 (DictReader 行列表, 实际编码)。"""
    errors = []
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            return rows, enc
        except (UnicodeDecodeError, UnicodeError) as e:
            errors.append(f"{enc}: {e}")
    raise ValueError("CSV 编码无法识别（尝试 utf-8-sig/utf-8/gbk 均失败）")


def _norm_key(k: str) -> str:
    return (k or "").strip().lower()


class WecomsgCsvImporter:
    source_name = "wecomsg"

    def __init__(self) -> None:
        self.stats: dict = {}

    # ---- 协议方法 ----
    def detect(self, path: Path) -> bool:
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
                first = f.readline()
            if not first.strip():
                return False
            # 简易表头判断（避免整读大文件）
            import csv as _csv
            header = next(_csv.reader([first]))
            keys = {_norm_key(k) for k in header}
            return _REQUIRED_COLS.issubset(keys)
        except Exception:
            return False

    def parse(self, path: Path) -> list[dict]:
        rows, encoding = _read_csv_rows(Path(path))
        self.stats = {"source_name": self.source_name, "total_rows": len(rows),
                      "encoding": encoding, "parsed": 0, "skipped_group": 0,
                      "skipped_unknown_type": 0, "skipped_no_time": 0,
                      "unknown_type_names": {}}
        out: list[dict] = []
        for r in rows:
            row = {_norm_key(k): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}

            room = row.get("room_name") or ""
            if "chatroom" in room.lower() or "@chatroom" in room:
                self.stats["skipped_group"] += 1
                continue

            type_name = row.get("type_name") or ""
            msg_type = TYPE_NAME_MAP.get(type_name)
            if msg_type is None:
                self.stats["skipped_unknown_type"] += 1
                self.stats["unknown_type_names"][type_name or "(空)"] = \
                    self.stats["unknown_type_names"].get(type_name or "(空)", 0) + 1
                continue

            ts = self._to_unix(row.get("createtime") or "")
            if ts is None:
                self.stats["skipped_no_time"] += 1
                continue

            msg, src = self._extract_msg_src(row)
            account = row.get("talker") or f"[is_sender={row.get('is_sender') or '0'}]"
            content = self._to_content(msg_type, msg, src, type_name)
            pid = row.get("msgsvrid") or row.get("id") or ""
            out.append(canonical_message(ts, msg_type, content, account,
                                         platform_message_id=str(pid) if pid else None))
            self.stats["parsed"] += 1
        return out

    # ---- 内部工具 ----
    @staticmethod
    def _to_unix(value: str) -> int | None:
        """CreateTime 秒级 unix（数字）或 "YYYY-MM-DD HH:MM:SS"（按东八区）。"""
        v = (value or "").strip()
        if not v:
            return None
        try:
            return int(float(v))
        except ValueError:
            pass
        if DATETIME_RE.match(v):
            v = v.replace("/", "-").replace("T", " ")
            try:
                dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    dt = datetime.strptime(v, "%Y-%m-%d %H:%M")
                except ValueError:
                    return None
            return int(dt.replace(tzinfo=TZ8).timestamp())
        return None

    @staticmethod
    def _extract_msg_src(row: dict) -> tuple[str, str]:
        """新版 msg/src 分列；旧版 content 列为 JSON {"src":..,"msg":..}。"""
        if "msg" in row or "src" in row:
            return row.get("msg") or "", row.get("src") or ""
        raw = row.get("content") or ""
        if raw.startswith("{"):
            try:
                d = json.loads(raw)
                return str(d.get("msg") or ""), str(d.get("src") or "")
            except ValueError:
                return raw, ""
        return raw, ""

    @staticmethod
    def _to_content(msg_type: int, msg: str, src: str, type_name: str = "") -> str:
        """按 canonical 约定合成 content（忠实转换源数据，不虚构）。"""
        if msg_type == TYPE_TEXT or msg_type == TYPE_QUOTE_TEXT or msg_type == TYPE_TRANSFER:
            return msg
        if msg_type == TYPE_IMAGE_EMOJI:
            if type_name in ("动画表情", "表情包"):
                return Path(src.replace("\\", "/")).name if src else "[动画表情]"
            return "[图片]"          # 图片仅元数据，不携带路径
        if msg_type == TYPE_FILE:
            name = msg or (Path(src.replace("\\", "/")).name if src else "")
            return f"[文件] {name}".strip() if name else "[文件]"
        if msg_type == TYPE_CALL:
            return "[通话]"
        if msg_type == TYPE_NAMECARD:
            return msg or "[名片]"
        if msg_type == TYPE_RECALL:
            return msg or "[撤回]"
        return msg


# 模块导入即登记进格式注册表
from importers import registry as _registry            # noqa: E402
_registry.register(WecomsgCsvImporter())
