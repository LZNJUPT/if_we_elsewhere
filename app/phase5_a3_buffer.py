# -*- coding: utf-8 -*-
"""
IfWe Phase 5 · A3 短期/工作记忆缓冲（Letta core blocks 思想，轻量自研）
- WorkingMemory: 当前模拟会话的上下文字段（deque，按字符预算），与 facts 长期记忆分层
- 窗口满 → LLM 摘要压缩：老条目折叠成摘要(派生结论)写入 sim_facts(source='simulation_smm') + sim_working_mem 留档
- 检索桥接: 缓冲内按最近优先(text overlap)，长期走 MemoryRetriever(由 A2 调用)

用法: 供 phase5_a2_loop.py 使用；`python phase5_a3_buffer.py` 冒烟
"""
from __future__ import annotations

import json
from collections import deque

import phase5_common as pc

DEFAULT_BUDGET = 2400      # 对话缓冲字符预算
SUMMARY_SYS = """你是模拟对话的短期记忆压缩器。把一段早期对话压缩为 2-4 条要点（谁、聊了什么、情绪、结论），
只保留对后续互动有影响的派生信息，≤80 字。直接输出要点文本，不要输出 JSON、不要带原文逐字复述。"""


class WorkingMemory:
    def __init__(self, max_chars: int = DEFAULT_BUDGET):
        self.max_chars = max_chars
        self._entries: deque[dict] = deque()   # {"day","sender","text"}
        self.summaries: list[dict] = []        # 压缩摘要（已折叠）

    # ---- 写入 ----
    def add(self, day: str, sender: str, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        self._entries.append({"day": day, "sender": sender, "text": t})
        if self.current_chars > self.max_chars:
            self._fold_oldest()

    # ---- 渲染 ----
    def render(self, tail: int = 0) -> str:
        items = list(self._entries)[-tail:] if tail else list(self._entries)
        lines = []
        for e in items:
            lines.append(f"[{e['day']} {e['sender']}] {e['text']}")
        return "\n".join(lines)

    def last(self, n: int = 1) -> list[dict]:
        return list(self._entries)[-n:]

    @property
    def current_chars(self) -> int:
        return sum(len(e["text"]) + 8 for e in self._entries)

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    # ---- 折叠压缩（A3 核心）----
    def _fold_oldest(self, client=None, conn=None, sim_id: str = "", day: str = "") -> None:
        items = list(self._entries)
        n = max(1, len(items) // 2)
        oldest, rest = items[:n], items[n:]
        transcript = "\n".join(f"[{e['day']} {e['sender']}] {e['text']}" for e in oldest)
        summary_text = ""
        if client is not None:
            try:
                resp = client.client.chat.completions.create(
                    model=client.model,
                    messages=[{"role": "system", "content": SUMMARY_SYS},
                              {"role": "user", "content": "待压缩对话：\n" + transcript}],
                    temperature=0.3, max_tokens=400)
                summary_text = (resp.choices[0].message.content or "").strip()
            except Exception:
                summary_text = ""
        if not summary_text:
            summary_text = f"[已压缩{len(oldest)}条早期对话]"
        summary = {"day": day, "text": summary_text, "chars_saved": len(transcript)}
        self.summaries.append(summary)
        self._entries = deque(rest)
        if conn is not None:
            seq = len(self.summaries)
            fid = f"{sim_id}-WM{seq:03d}"
            kw = pc.build_keywords_cn("关系", "buffer_summary", summary_text)
            conn.execute(
                """INSERT OR REPLACE INTO sim_facts
                   (fact_id, sim_id, memory_type, subject, relation, object,
                    conclusion_text, valid_at, source, source_id, confidence, keywords, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fid, sim_id, "semantic", "relationship", "buffer_summary", day,
                 f"【模拟对话摘要】截至{day}双方讨论要点：{summary_text}", day,
                 "simulation_smm", day[:7], 0.6, kw, pc.now_str()))
            conn.execute(
                """INSERT OR REPLACE INTO sim_working_mem
                   (wm_id, sim_id, day, kind, content, meta, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (fid, sim_id, day, "compressed_summary", summary_text,
                 json.dumps({"transcript_len": len(transcript), "turns": len(oldest)},
                            ensure_ascii=False), pc.now_str()))
            conn.commit()


if __name__ == "__main__":
    wm = WorkingMemory(max_chars=120)
    for i in range(40):
        wm.add("2026-07-20", "A" if i % 2 == 0 else "B", f"测试短消息第{i}条：今天怎么样")
    print("entries:", wm.entry_count, "chars:", wm.current_chars)
    print("summaries:", len(wm.summaries), wm.summaries[0] if wm.summaries else "")