# -*- coding: utf-8 -*-
"""Phase 17 · 分支状态持久化与恢复（二期方案 v2 §5.10）

问题：`s_state`（情绪）/ `calib` / `recent_texts`（近期自己消息）都只在
PersonaAgent 内存里，引擎重启后回到「平静 5.0」；`rel_current` 之前只落在
`sim_runs.config_json` 的 run_state 里（例外）。

修法：新表 `sim_branch_state`（不要塞 sim_runs.config_json——那会被 UI 当配置读）。
  - DialEngine.__init__ 恢复；`_save_state()` 同时写新表与 run_state
    （后者为向后兼容，Phase B 收口后可去）；
  - 字段与方案 §5.10 的 DDL 一致：rel_current / s_state / scene_state / stance /
    calib / recent_texts / decision_tail；version 便于将来结构升级。

验收（§5.10）：构造一条线 → 产生情绪变化 → 重新实例化 DialEngine →
情绪与状态与重启前一致（tests/test_phase17_phaseb.py）。

隔离红线（§5.6）：分支状态只写 sim_* 表，绝不写主库 relationship_state。
"""
from __future__ import annotations

import json
import sqlite3

import phase5_common as pc

# 与 phase5_schema.sql 尾部定义保持一致（ensure_schema 供旧库/内存副本就地补表）
DDL = """
CREATE TABLE IF NOT EXISTS sim_branch_state (
  sim_id TEXT PRIMARY KEY, rel_current TEXT, s_state TEXT, scene_state TEXT,
  stance TEXT, calib TEXT, recent_texts TEXT, decision_tail TEXT,
  version INTEGER DEFAULT 1, updated_at TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """就地补建 sim_branch_state（幂等；加法迁移，不碰既有表）"""
    conn.executescript(DDL)


def save(conn: sqlite3.Connection, sim_id: str, *, rel_current: dict | None,
         s_state: dict | None = None, scene_state: dict | None = None,
         stance: dict | None = None, calib: str | None = None,
         recent_texts: list | None = None, decision_tail: dict | None = None,
         version: int = 1) -> None:
    """整行覆盖写（INSERT OR REPLACE）；失败由调用方决定是否容忍（引擎里只警告）"""
    ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO sim_branch_state "
        "(sim_id, rel_current, s_state, scene_state, stance, calib, recent_texts, "
        " decision_tail, version, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sim_id,
         json.dumps(rel_current or {}, ensure_ascii=False),
         json.dumps(s_state or {}, ensure_ascii=False),
         json.dumps(scene_state, ensure_ascii=False) if scene_state is not None else None,
         json.dumps(stance, ensure_ascii=False) if stance is not None else None,
         calib,
         json.dumps(recent_texts or [], ensure_ascii=False),
         json.dumps(decision_tail, ensure_ascii=False) if decision_tail is not None else None,
         int(version), pc.now_str()))
    conn.commit()


def load(conn: sqlite3.Connection, sim_id: str) -> dict | None:
    """读一条线的分支状态（无记录返回 None；JSON 解析失败的字段按 None 处理）"""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT rel_current, s_state, scene_state, stance, calib, recent_texts, "
        "decision_tail, version, updated_at FROM sim_branch_state WHERE sim_id=?",
        (sim_id,)).fetchone()
    if not row:
        return None

    def _j(s):
        try:
            return json.loads(s) if s else None
        except Exception:
            return None

    return {"rel_current": _j(row[0]), "s_state": _j(row[1]),
            "scene_state": _j(row[2]), "stance": _j(row[3]), "calib": row[4],
            "recent_texts": _j(row[5]) or [], "decision_tail": _j(row[6]),
            "version": row[7], "updated_at": row[8]}
