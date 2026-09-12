# -*- coding: utf-8 -*-
"""IfWe 数据体检脚本（第一版，只读不写，不输出消息正文内容）"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 数据源走 config chat.source 的默认位置（或环境变量 IFWE_CHAT_SOURCE 覆盖）
DATA = Path(__file__).resolve().parent.parent / os.environ.get(
    "IFWE_CHAT_SOURCE", "data/raw/chat.jsonl")
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai 显示用

types = Counter()
senders = Counter()
msg_types = Counter()
timestamps = []
total_lines = 0
n_messages = 0
content_len = []
min_ts = None
max_ts = None

with open(DATA, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        total_lines += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[WARN] 第 {total_lines} 行 JSON 解析失败: {e}", file=sys.stderr)
            continue
        t = rec.get("_type")
        types[t] += 1
        if t == "message":
            n_messages += 1
            senders[rec.get("accountName")] += 1
            msg_types[rec.get("type")] += 1
            ts = rec.get("timestamp")
            if ts:
                timestamps.append(ts)
                min_ts = ts if min_ts is None else min(min_ts, ts)
                max_ts = ts if max_ts is None else max(max_ts, ts)
            content = rec.get("content") or ""
            content_len.append(len(content))

print("=== 文件总行数 ===")
print(total_lines)
print("=== _type 分布 ===")
for k, v in types.most_common():
    print(f"  {k}: {v}")
print("=== 消息条数 ===")
print(n_messages)
print("=== 发送者分布（消息条数） ===")
for k, v in senders.most_common():
    print(f"  {k}: {v}")
print("=== 消息 type 分布 ===")
for k, v in msg_types.most_common():
    print(f"  type={k}: {v}")
print("=== 时间跨度 ===")
if min_ts and max_ts:
    print(f"  起始: {datetime.fromtimestamp(min_ts, TZ)}")
    print(f"  结束: {datetime.fromtimestamp(max_ts, TZ)}")
    days = (max_ts - min_ts) / 86400
    print(f"  跨度天数: {days:.1f} 天")
    # 按天消息数
    day_counts = Counter(datetime.fromtimestamp(ts, TZ).date() for ts in timestamps)
    print(f"  有消息的天数: {len(day_counts)} 天")
    avg = n_messages / len(day_counts) if day_counts else 0
    print(f"  平均每天消息数(按有消息的天): {avg:.1f}")
print("=== 内容长度统计（字符数） ===")
if content_len:
    content_len.sort()
    n = len(content_len)
    def pct(p):
        return content_len[min(n - 1, int(n * p))]
    print(f"  min={content_len[0]}  p25={pct(0.25)}  median={pct(0.5)}  p75={pct(0.75)}  p95={pct(0.95)}  max={content_len[-1]}")
    print(f"  空内容消息数: {sum(1 for c in content_len if c == 0)}")