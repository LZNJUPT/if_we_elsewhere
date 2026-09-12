# -*- coding: utf-8 -*-
"""Importer 协议与 canonical 消息构造（v0.2 O-5a）。"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Importer(Protocol):
    """导入器协议：把一种「用户已合法导出」的聊天文件解析为 canonical 消息列表。

    实现约定：
    - source_name: 格式标识（如 "chatlab"），用于 doctor 报告与 CLI --format；
    - detect(path) -> bool: 能否识别该格式（只读文件头部即可判定，不整读大文件）；
    - parse(path) -> list[dict]: 产出 canonical 消息 dict 列表（见包 __init__ 注释）；
      无法对应的消息类型必须「跳过 + 计数」，不得猜测归类。
    """

    source_name: str

    def detect(self, path: Path) -> bool: ...

    def parse(self, path: Path) -> list[dict]: ...


# canonical type 常量（与 phase1_ingest.parse_message 的分支一一对应）
TYPE_TEXT = 0
TYPE_IMAGE_EMOJI = 7
TYPE_FILE = 4
TYPE_CALL = 23
TYPE_MINIPROGRAM = 24
TYPE_QUOTE_TEXT = 25
TYPE_NAMECARD = 27
TYPE_RECALL = 80
TYPE_TRANSFER = 99

KNOWN_TYPES = {TYPE_TEXT, TYPE_IMAGE_EMOJI, TYPE_FILE, TYPE_CALL,
               TYPE_MINIPROGRAM, TYPE_QUOTE_TEXT, TYPE_NAMECARD,
               TYPE_RECALL, TYPE_TRANSFER}


def canonical_message(timestamp: int, msg_type: int, content: str,
                      account_name: str,
                      platform_message_id: str | None = None) -> dict:
    """构造一条 canonical 消息（做最小类型归一，保证下游 int/str 契约）。"""
    return {
        "_type": "message",
        "platformMessageId": platform_message_id,
        "timestamp": int(timestamp),
        "type": int(msg_type),
        "content": content if content is not None else "",
        "accountName": account_name if account_name is not None else "",
    }
