# -*- coding: utf-8 -*-
"""Phase 17 · 行动决策 ActionDecision（二期方案 v2 §6.1E + §5.8 关系增量通道）

「是否回复 / 如何回复」由本模块在生成前结构化决定并落库 `sim_action_log`；
生成模型只负责按行动约束产出文字（两阶段架构，§6.2；不设"后续合并"）。

MVP 决策策略（确定性、数据驱动；LLM 结构化决策器留接口 `Decider`，Phase D 接）：
  - reply_mode=none      ：锚定 P 掷骰不中（与既有闸门同源，P 是 paired 口径锚定值）；
  - reply_mode=boundary  ：关系话题（再次情感推进）× 明确拒绝立场 → 重申边界；
  - reply_mode=brief     ：被动低频/极低联系档 × 普通话题 → 简短回应（不展开）；
  - reply_mode=neutral   ：冲突/事务等中性处理；
  - reply_mode=engaged   ：其余（warm/正常档）；
  - delayed / initiate   ：Phase E（字段保留，MVP 恒 False/None）。

rel_delta（§5.8）：本行动对关系五维的增量 = 立场门控后的事件增量（stance.gated_delta）。
产品路径（phase17 模式）下这是行动影响关系的唯一入口——但实现上取「幂等重算」形态：
_commit_rel 对 day_events 逐条套 stance.gated_delta（可重算、可回放），
本字段落库作该轮审计快照。普通话题在拒绝立场下 gate=0 → 不再自动加热（T9）。

登记校验（§5.8 第 3 点）：boundary 轮的事件类型固定为「边界重申」（已在
phase6_engine.DEFAULT_RULES 登记，synthetic 推导出处见该处注释）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import phase17_observe as observe
import phase17_stance as st
import phase5_common as pc

# 与 phase5_schema.sql 尾部定义保持一致（ensure_schema 供旧库/内存副本就地补表）
DDL = """
CREATE TABLE IF NOT EXISTS sim_action_log (
  action_id TEXT PRIMARY KEY, sim_id TEXT NOT NULL, session_id TEXT,
  day TEXT, turn_idx INTEGER, path TEXT,
  reply_mode TEXT, should_generate INTEGER, p_reply REAL,
  scene_mode TEXT, boundary_pressure TEXT,
  event_type TEXT, rel_delta TEXT,
  must_not_do TEXT, decision_reason TEXT,
  generation_violation INTEGER DEFAULT 0, violation_note TEXT,
  model_version TEXT, evidence_ids TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_simact_sim ON sim_action_log(sim_id, day, turn_idx);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """就地补建 sim_action_log（幂等；加法迁移，不碰既有表）"""
    conn.executescript(DDL)


@dataclass
class ActionDecision:
    """本轮行动决策（生成前产生，必须落库——§6.1E）"""
    reply_mode: str                     # none / brief / neutral / boundary / engaged / delayed / initiate
    should_generate: bool
    p_reply: float | None = None
    scene_mode: str = "普通话题"
    boundary_pressure: str = "low"
    event_type: str | None = None       # 本轮行动对应的事件类型（boundary → 边界重申）
    rel_delta: dict = field(default_factory=dict)
    must_preserve: list = field(default_factory=list)
    must_not_do: list = field(default_factory=list)
    generation_hint: str = ""           # 注入生成提示的行动约束（结构化行动 → 文字层）
    decision_reason: str = ""
    model_version: str = "action_mvp_v1"
    evidence_ids: list = field(default_factory=list)
    initiate: bool = False              # 主动开口：Phase E（§8.2 注 2）
    delay_bucket: str | None = None     # Phase E
    medium_policy: str | None = None    # Phase E
    length_policy: str | None = None
    generation_violation: int = 0
    violation_note: str = ""


def decide(stance: dict, scene: dict, ev_user: dict, p_reply: float,
           rng, intent_rejected: bool | None = None,
           profile: dict | None = None) -> ActionDecision:
    """MVP 行动决策（确定性；rng 只用于 none 掷骰，与既有闸门同语义）。"""
    mode_scene = scene.get("scene_mode", "普通话题")
    pressure = scene.get("boundary_pressure", "low")
    ev_type = (ev_user or {}).get("event_type")
    rejected = intent_rejected if intent_rejected is not None \
        else (stance.get("romantic_intent") == "明确拒绝")
    low_contact = stance.get("contact_willingness") in ("被动极低", "被动低频")

    should = bool(rng.random() <= p_reply)
    a = ActionDecision(reply_mode="none", should_generate=False, p_reply=p_reply,
                       scene_mode=mode_scene, boundary_pressure=pressure,
                       evidence_ids=list(stance.get("evidence_ids") or []))
    if not should:
        a.decision_reason = f"锚定 P={p_reply:.2f} 掷骰未中 → 沉默（与既有闸门同源）"
        return a

    if mode_scene == "关系话题" and pressure == "high" and rejected:
        # 再次情感推进 × 明确拒绝 → 边界重申（事件类型已登记，§5.8 扩展）
        a.reply_mode = "boundary"
        a.should_generate = True
        a.event_type = "边界重申"
        a.length_policy = "立场重申可完整表达，不迁就短约束"
        a.must_not_do = ["推进关系", "重开复合讨论", "为拒绝道歉", "给出模糊希望"]
        a.must_preserve = ["既有边界", "基本善意（不辱骂不冷战威胁）"]
        a.generation_hint = ("【本轮行动约束】重申既有边界：不接受情感推进、不重开关系讨论。"
                             "语气坚定、直接，不需要道歉，也不留模糊空间；可以保持基本礼貌。")
        a.decision_reason = "关系话题 × 明确拒绝立场 → boundary（§6.1C boundary_consequence）"
        return a

    if mode_scene == "冲突":
        a.reply_mode = "neutral"
        a.should_generate = True
        a.event_type = ev_type
        a.decision_reason = "冲突场景 → 中性处理（不升级不回避）"
        return a

    if mode_scene in ("普通话题", "事务咨询") and (rejected or low_contact):
        a.reply_mode = "brief"
        a.should_generate = True
        a.event_type = ev_type          # 兜底「日常陪伴」由 say() 补，rel 增量经门控=0
        a.length_policy = "短回复（1~2 句），不展开新话题、不主动延展"
        a.generation_hint = ("【本轮行动约束】简短回应即可（1~2 句），礼貌但克制，"
                             "不追问、不延展话题、不表达思念或亲密。")
        a.decision_reason = f"普通话题 × 低联系档（{stance.get('contact_willingness')}）→ brief"
        return a

    a.reply_mode = "engaged"
    a.should_generate = True
    a.event_type = ev_type
    a.decision_reason = f"{mode_scene} × 正常联系档 → 正常互动"
    return a


def apply_expression(action: ActionDecision, profile: dict | None) -> None:
    """表达层字段填充（§11 Phase E：长度/媒介策略来自真实数据画像，Phase E）。"""
    if not profile:
        return
    try:
        import phase17_expression as ex
        action.length_policy = ex.length_hint(action.reply_mode, profile) or action.length_policy
        action.medium_policy = profile.get("medium_probs")
    except Exception:
        pass


def action_event(action: ActionDecision, ev_user: dict, has_reply: bool) -> dict:
    """按 §5.7 事件-行动对应规则定本轮事件：boundary → 边界重申；有来回且无
    关键词事件 → 兜底「日常陪伴」（12 类坐标系保留）；沉默轮不计权（调用方处理）。"""
    if action.reply_mode == "boundary":
        return {"event_type": "边界重申", "severity": 3, "importance": 4}
    if ev_user:
        return dict(ev_user)
    if has_reply:
        return {"event_type": "日常陪伴", "severity": 1, "importance": 1}
    return {}


def log_action(conn: sqlite3.Connection, *, sim_id: str, session_id: str,
               day: str, turn_idx: int, path: str, action: ActionDecision) -> None:
    """落一行行动决策（INSERT OR REPLACE 幂等；失败由调用方决定容忍策略）"""
    ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO sim_action_log "
        "(action_id, sim_id, session_id, day, turn_idx, path, reply_mode, should_generate, "
        " p_reply, scene_mode, boundary_pressure, event_type, rel_delta, must_not_do, "
        " decision_reason, generation_violation, violation_note, model_version, "
        " evidence_ids, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{session_id}-A{int(turn_idx):03d}", sim_id, session_id, day, int(turn_idx),
         path, action.reply_mode, int(bool(action.should_generate)), action.p_reply,
         action.scene_mode, action.boundary_pressure, action.event_type,
         json.dumps(action.rel_delta, ensure_ascii=False),
         json.dumps(action.must_not_do, ensure_ascii=False),
         action.decision_reason, int(action.generation_violation),
         action.violation_note, action.model_version,
         json.dumps(action.evidence_ids, ensure_ascii=False), pc.now_str()))
    conn.commit()


def check_generation_violation(action: ActionDecision, reply_text: str,
                               advance_lexicon: list[str] | None = None) -> tuple[int, str]:
    """§6.2 步骤 8：生成结果是否违反行动决策（记录，不改写为友好回复——§15.6）。

    MVP 校验：boundary 轮的回复不得包含关系推进表述（推进词复用
    phase17_stance.ADVANCE_LEXICON 同源坐标系）。
    """
    if action.reply_mode != "boundary":
        return 0, ""
    lex = advance_lexicon or st.ADVANCE_LEXICON
    hits = [k for k in lex if k in (reply_text or "")]
    if hits:
        return 1, f"boundary 轮回复含推进表述: {hits[:3]}"
    return 0, ""


def mark_violation(conn: sqlite3.Connection, session_id: str, turn_idx: int,
                   note: str) -> None:
    """生成后校验发现违反行动决策 → 回写该轮 sim_action_log（§6.2 步骤 8 / §15.6：
    记录可诊断的 generation_violation，不静默改写成友好回复）"""
    ensure_schema(conn)
    conn.execute(
        "UPDATE sim_action_log SET generation_violation=1, violation_note=? "
        "WHERE action_id=?", (note, f"{session_id}-A{int(turn_idx):03d}"))
    conn.commit()


def read_actions(conn: sqlite3.Connection, sim_id: str, limit: int = 0) -> list[dict]:
    sql = ("SELECT action_id, sim_id, session_id, day, turn_idx, path, reply_mode, "
           "should_generate, p_reply, scene_mode, boundary_pressure, event_type, "
           "rel_delta, must_not_do, decision_reason, generation_violation, "
           "violation_note, model_version, evidence_ids, created_at "
           "FROM sim_action_log WHERE sim_id=? ORDER BY rowid")
    rows = conn.execute(sql, (sim_id,)).fetchall()
    if limit > 0:
        rows = rows[-limit:]
    cols = ["action_id", "sim_id", "session_id", "day", "turn_idx", "path",
            "reply_mode", "should_generate", "p_reply", "scene_mode",
            "boundary_pressure", "event_type", "rel_delta", "must_not_do",
            "decision_reason", "generation_violation", "violation_note",
            "model_version", "evidence_ids", "created_at"]
    out = [dict(zip(cols, r)) for r in rows]
    for o in out:
        for k in ("rel_delta", "must_not_do", "evidence_ids"):
            try:
                o[k] = json.loads(o[k]) if o[k] else None
            except Exception:
                pass
    return out
