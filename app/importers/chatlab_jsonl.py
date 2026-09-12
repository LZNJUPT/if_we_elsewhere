# -*- coding: utf-8 -*-
"""chatlab/WeFlow JSONL 导入器（v0.2 O-5a 迁入 + O-5d 字段宽容解析）。

源格式（严格路径，与 v0.1 逐字段一致）：每行一个 JSON 对象，
    {"_type":"message","platformMessageId":...,"timestamp":<秒>,
     "type":...,"content":...,"accountName":...}

v0.2 宽容解析（O-5d）：
1. 字段别名表（取值优先级：规范名 > 别名表序）：
       timestamp <- timestamp / ts / time
       content   <- content / text / message
       accountName <- accountname / sender / talker / nick
2. 时间戳单位自动识别：数值 >1e12 判毫秒（/1000）；1e9~1e12 判秒；
   字符串按 ISO8601 解析（含 Z 与时区偏移；裸日期按东八区，与项目约定一致）；
   无法识别的时间戳 → 跳过该行 + 计数（不猜测）。
3. CSV 通用支持：编码探测 utf-8-sig → utf-8 → gbk；首行表头经别名表映射
   后输出 canonical（与 JSONL 同一出口）；不含 chatlab 必需列的 CSV 不命中。
4. JSONL 行缺 `_type` 但具备 可识别时间戳 + 内容 别名列时，按宽容路径
   接受为消息（v0.1 对这类行是忽略——只影响原本产出 0 条的文件）。

合规立场：只消费用户已合法导出的文件；不解析任何 IM 数据库。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from importers.base import TYPE_TEXT, canonical_message

TZ8 = timezone(timedelta(hours=8))

# 字段别名表（规范名 -> 候选别名，按序取第一个命中的列；匹配不区分大小写）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "ts", "time"),
    "content": ("content", "text", "message"),
    "accountName": ("accountname", "sender", "talker", "nick"),
}

_MS_THRESHOLD = 1e12
_SEC_LOWER = 1e9


def normalize_timestamp(value) -> int | None:
    """时间戳单位自动识别 → 秒级 unix；无法识别返回 None（调用方跳过+计数）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return _parse_iso(s)
    if v >= _MS_THRESHOLD:
        return int(v / 1000)
    if _SEC_LOWER <= v < _MS_THRESHOLD:
        return int(v)
    return _parse_iso(s) if not isinstance(value, (int, float)) else None


def _parse_iso(s: str) -> int | None:
    """ISO8601（含 Z / 偏移；裸日期按东八区）。"""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ8)
    return int(dt.timestamp())


def resolve_field(row: dict, canonical: str):
    """按别名表从行内取字段（键归一为小写比较；规范名优先于别名序）。"""
    lowered = {k.strip().lower(): v for k, v in row.items() if k is not None}
    for name in FIELD_ALIASES[canonical]:
        if name in lowered:
            v = lowered[name]
            return v.strip() if isinstance(v, str) else v
    return None


def _row_has_min_fields(rec: dict) -> bool:
    return (resolve_field(rec, "timestamp") is not None
            and resolve_field(rec, "content") is not None)


class ChatlabJsonlImporter:
    source_name = "chatlab"

    def __init__(self) -> None:
        self.stats: dict = {}

    # ---- 探测 ----
    def detect(self, path: Path) -> bool:
        p = Path(path)
        if p.suffix.lower() == ".csv":
            return self._detect_csv(p)
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        return False
                    # 严格命中（_type 键）或宽容命中（时间戳+内容别列）
                    return "_type" in rec or _row_has_min_fields(rec)
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        return False

    def _detect_csv(self, path: Path) -> bool:
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
                first = f.readline()
            if not first.strip():
                return False
            header = next(csv.reader([first]))
            keys = {k.strip().lower() for k in header}
            if "type_name" in keys:            # WeChatMsg CSV 由专属适配器负责
                return False
            return ("timestamp" in keys or "ts" in keys or "time" in keys) and \
                   bool({"content", "text", "message"} & keys)
        except Exception:
            return False

    # ---- 解析 ----
    def parse(self, path: Path) -> list[dict]:
        p = Path(path)
        if p.suffix.lower() == ".csv":
            return self._parse_csv(p)
        return self._parse_jsonl(p)

    def _parse_jsonl(self, path: Path) -> list[dict]:
        """与 v0.1 等价 + 宽容路径：_type=message 原样接受；缺 _type 但字段齐全
        亦接受；时间戳无法识别的行跳过+计数。"""
        rows = []
        self.stats = {"source_name": self.source_name, "total_rows": 0,
                      "parsed": 0, "skipped_no_time": 0}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    continue
                self.stats["total_rows"] += 1
                if rec.get("_type") == "message" or \
                        ("_type" not in rec and _row_has_min_fields(rec)):
                    msg = self._canonicalize(rec)
                    if msg is None:
                        self.stats["skipped_no_time"] += 1
                        continue
                    rows.append(msg)
                    self.stats["parsed"] += 1
        return rows

    def _read_csv_rows(self, path: Path) -> tuple[list[dict], str]:
        errors = []
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                with open(path, "r", encoding=enc, newline="") as f:
                    rows = list(csv.DictReader(f))
                return rows, enc
            except (UnicodeDecodeError, UnicodeError) as e:
                errors.append(f"{enc}: {e}")
        raise ValueError("CSV 编码无法识别（尝试 utf-8-sig/utf-8/gbk 均失败）")

    def _parse_csv(self, path: Path) -> list[dict]:
        """通用 CSV：表头经别名表映射 → canonical（与 JSONL 同一出口）。"""
        rows, encoding = self._read_csv_rows(path)
        self.stats = {"source_name": self.source_name, "total_rows": len(rows),
                      "encoding": encoding, "parsed": 0, "skipped_no_time": 0}
        out = []
        for r in rows:
            self.stats["total_rows"] += 1
            msg = self._canonicalize(r)
            if msg is None:
                self.stats["skipped_no_time"] += 1
                continue
            out.append(msg)
            self.stats["parsed"] += 1
        return out

    # ---- 行 → canonical ----
    def _canonicalize(self, rec: dict) -> dict | None:
        ts = normalize_timestamp(rec.get("timestamp"))
        if ts is None:
            ts = normalize_timestamp(resolve_field(rec, "timestamp"))
        if ts is None:
            return None
        msg_type = rec.get("type")
        try:
            msg_type = int(msg_type)
        except (TypeError, ValueError):
            msg_type = TYPE_TEXT
        content = rec.get("content")
        if content is None:
            content = resolve_field(rec, "content")
        account = rec.get("accountName")
        if not account:
            account = resolve_field(rec, "accountName") or ""
        pid = rec.get("platformMessageId") or rec.get("id") or None
        if pid is not None:
            pid = str(pid)
        return canonical_message(ts, msg_type, str(content or ""), str(account),
                                 platform_message_id=pid)


# 模块导入即登记进格式注册表
from importers import registry as _registry            # noqa: E402
_registry.register(ChatlabJsonlImporter())
