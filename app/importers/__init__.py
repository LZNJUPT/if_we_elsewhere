# -*- coding: utf-8 -*-
"""
IfWe 导入器插件架构（v0.2 O-5a）

所有外部聊天导出格式经 Importer 适配为 canonical 消息 dict 列表后，
统一进入 phase1_ingest 流水线（脱敏/会话化/入库/门禁）。

产品立场（合规红线）：本项目不解析任何 IM 的数据库、进程内存或备份文件，
只消费用户已合法导出的文件。每个 adapter 只做「格式转换」，不做业务判断。

canonical 消息 dict（与 phase1_ingest 内部结构对齐）:
    {"_type": "message",
     "timestamp": <秒级 unix int>,
     "type": <0 文本 / 7 图片表情 / 4 文件 / 23 通话 / 24 小程序 /
              25 引用文本 / 27 名片 / 80 撤回 / 99 转账>,
     "content": <str>,
     "accountName": <原始账号名>,
     "platformMessageId": <可选；缺省时由 ingest 派生>}
"""
from importers.base import Importer, canonical_message          # noqa: F401
from importers.registry import auto_detect, by_name, register   # noqa: F401
