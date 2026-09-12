# -*- coding: utf-8 -*-
"""chatlab/WeFlow JSONL 导入器（v0.2 O-5a 自 phase1_ingest 整体迁入，行为不变）。

源格式即 canonical 格式本身：每行一个 JSON 对象，
    {"_type":"message","platformMessageId":...,"timestamp":<秒>,
     "type":...,"content":...,"accountName":...}
非 _type=message 的行被跳过；JSON 解析失败按 v0.1 行为直接抛出。
"""
from __future__ import annotations

import json
from pathlib import Path

from importers.base import Importer  # noqa: F401  (类型标注用)


class ChatlabJsonlImporter:
    source_name = "chatlab"

    def detect(self, path: Path) -> bool:
        """读首个非空行：能解析为 JSON 且带 _type 键即命中（容忍其他行）。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    return isinstance(rec, dict) and "_type" in rec
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        return False

    def parse(self, path: Path) -> list[dict]:
        """与 v0.1 phase1_ingest.load_raw 逐字段等价（纯搬家）。"""
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("_type") == "message":
                    rows.append(rec)
        return rows


# 模块导入即登记进格式注册表（registry 懒加载本模块时生效）
from importers import registry as _registry            # noqa: E402
_registry.register(ChatlabJsonlImporter())
