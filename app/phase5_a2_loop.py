# -*- coding: utf-8 -*-
"""
IfWe Phase 5 · A2 双 Agent 会话循环（核心）+ A3/A5 集成
每轮(=每日会话)：读世界状态 → MemoryRetriever 检索相关记忆 → 结合 S 层情绪与关系状态
               → 生成回复/行动 → 写分支记忆流(缓冲+sim_facts) → 事件落库(隔离)
双 Agent 对称独立；关系状态为共享只读视图；模拟时钟可控 + 固定种子可复现。

用法:
  设置 LLM API Key 环境变量（config llm.api_key_env，默认 LLM_API_KEY）后
  python app/phase5_a2_loop.py --sim SIM-xxx --days 12 --max-turns 8   # 指定已冻结 sim 续跑
  python app/phase5_a2_loop.py --smoke                                  # 2 天 × 3 回合冒烟
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Optional

import phase2_llm
import phase4_retrieval
import phase5_common as pc
import phase5_a3_buffer as buf
import phase5_a5_pcc as pcc
import phase6_engine as p6     # Phase 6 R4: 正式关系引擎（正式模型替换启发式）
from phase5_llm import AgentReply, SessionSummary, REPLY_RULES, reply_user_block, summary_user_block

SENDER_NAME = pc.SENDER_NAME
EVENT_TYPE_CONF = {3: 0.8, 2: 0.7, 1: 0.6}
REWRITE_KEYWORDS = ["复合", "试试", "先当朋友", "机会", "边界", "愿意", "答应",
                    "慢慢来", "相处", "重新", "接受"]


class PersonaAgent:
    """对称独立的双 Agent 之一：人格 + S 层情绪状态 + PCC 校准 + 媒介/去重护栏"""
    def __init__(self, person: str, world: pc.WorldState,
                 similarity_policy=None):
        self.person = person                       # A / B
        self.name = SENDER_NAME[person]
        self.persona_block = world.persona_blocks[person]
        self.block_lite = pc.persona_lite_block(person)   # 每回合精简卡（降人机感）
        self.s_state: dict = {"label": "平静", "level": 5.0}   # S 层动态情绪
        self.calib: str = ""                       # A5 校准提示（可空）
        self.plan_today: str = ""
        self.turn_count = 0
        self.recent_texts: list[str] = []          # 近期自己消息（去重护栏）
        # 媒介倾向（据 persona L 层统计：A gif/图片更频繁）
        self.medium_probs = {
            "A": {"text": 0.58, "image": 0.22, "emoji": 0.15, "voice": 0.05},
            "B": {"text": 0.66, "image": 0.16, "emoji": 0.14, "voice": 0.04},
        }
        # 去重护栏策略（二期 §10.9）：callable(text)->bool|None（None=交回默认判定）。
        # None = 默认 bigram Jaccard≥0.45；禁止再用模块级改写类属性的方式替换护栏。
        self.similarity_policy = similarity_policy

    def update_emotion(self, emotion: dict) -> None:
        if isinstance(emotion, dict):
            if emotion.get("label"):
                self.s_state["label"] = emotion["label"]
            lv = emotion.get("level")
            try:
                self.s_state["level"] = min(10.0, max(0.0, float(lv)))
            except Exception:
                pass

    def _recent_similarity(self, text: str) -> float:
        from phase4_retrieval import cjk_bigrams
        t = set(cjk_bigrams(text))
        if not t:
            return 0.0
        best = 0.0
        for r in self.recent_texts:
            rs = set(cjk_bigrams(r))
            if not rs:
                continue
            best = max(best, len(t & rs) / len(t | rs))
        return best

    def _should_rewrite(self, text: str, action=None) -> bool:
        """去重护栏判定（§10.9 可注入策略；§11 Phase C：boundary 不拦）。

        优先级：
          1. boundary 行动（坚定重申立场）永不重写——划界的本质就是重复重申（T10）；
          2. 注入的 similarity_policy（如 F4 长度中位数豁免）；
          3. 默认 bigram Jaccard≥0.45。
        """
        if action is not None and getattr(action, "reply_mode", "") == "boundary":
            return False
        if self.similarity_policy is not None:
            v = self.similarity_policy(text)
            if v is not None:
                return bool(v)          # None = 交回默认判定（F4 短回复豁免后的透传）
        return self._recent_similarity(text) >= 0.45

    def act(self, client, ws_text: str, memories_text: str, buffer_text: str,
            last_msg: str, rng=None, full_persona: bool = False,
            action=None) -> AgentReply:
        """一次『决策-行动』：读世界 → 检索记忆 → 结合情绪 → 生成回复/行动。
        full_persona=首回合用全量人格；其余回合用精简卡。rng 用于媒介抽样(可复现)。
        action：本轮 ActionDecision（phase17 模式传入；约束注入生成提示、
        boundary 豁免去重护栏——§6.2 步骤 7/8）。"""
        sys = (self.persona_block if full_persona else self.block_lite) + "\n\n" + REPLY_RULES
        extra_constraint = ""
        if action is not None:
            hint = getattr(action, "generation_hint", "")
            if hint:
                extra_constraint = "\n\n" + hint
        user = reply_user_block(ws_text, memories_text, buffer_text, last_msg,
                                self.s_state, self.calib) + extra_constraint
        obj = client.extract(system=sys, user=user, response_model=AgentReply)
        # 媒介抽样（确定性，按人格倾向）
        if rng is not None:
            probs = (self.medium_probs.get(self.person)
                     or self.medium_probs.get("A") or {"text": 1.0})
            roll = rng.random()
            acc, medium = 0.0, "text"
            for m, p in probs.items():
                acc += p
                if roll < acc:
                    medium = m
                    break
            obj.medium = medium
        # 去重护栏：过度相似 → 强制换一种说法重写一次（可注入策略 + boundary 豁免，§10.9/T10）
        if obj.reply.strip() and self._should_rewrite(obj.reply, action):
            extra = ("\n\n【重写要求】你刚才的说法和此前一条重复度太高。请换一种说法：换个角度、"
                     "换个具体细节或岔开一个自然的新话头，不要照搬刚才的用词。")
            try:
                obj2 = client.extract(system=sys, user=user + extra, response_model=AgentReply)
                obj.reply = obj2.reply or obj.reply
                if not obj2.medium or obj2.medium == "text":
                    obj.medium = obj.medium
                else:
                    obj.medium = obj2.medium
                obj.action = obj2.action or obj.action
                obj.emotion = obj2.emotion or obj.emotion
            except Exception:
                pass
        self.recent_texts.append(obj.reply)
        self.recent_texts = self.recent_texts[-4:]
        self.update_emotion(obj.emotion)
        self.turn_count += 1
        return obj


# ---------------- 记忆检索视图 ----------------
def retrieve_memories(retriever: phase4_retrieval.MemoryRetriever,
                      conn, ws: pc.WorldState, query: str, as_of: str, top_k: int = 5) -> str:
    """分支记忆视图：主 facts(历史) + sim_facts(本分支) 融合 → 提示文本。
    分歧点后：主记忆冻结到分歧点（§10.4），隐藏被改写掉的真实未来。"""
    if not query:
        return ""
    frozen = None
    if ws.divergence_point and as_of >= ws.divergence_point:
        frozen = ws.divergence_point
    rows = pc.search_branch_memory(retriever, conn, ws.sim_id, query, as_of,
                                   top_k=top_k, frozen_at=frozen)
    return pc.branch_memory_text(rows)


# ---------------- 事件落库（隔离 -> sim_events + sim_facts） ----------------
def _person_of_summary(summary: str) -> str:
    s = summary or ""
    names = pc.SENDER_NAME          # {"A": 显示名A, "B": 显示名B}（来自配置）
    has_a = ("A" in s) or (names.get("A", "A") in s)
    has_b = ("B" in s) or (names.get("B", "B") in s)
    if has_a and has_b:
        return "both"
    if has_a:
        return "A"
    if has_b:
        return "B"
    return "neither"


def log_event(conn, sim_id: str, day: str, ev: dict, source_session: str) -> str:
    # 登记校验（二期 §5.8 第 3 点 / T11）：未登记 DEFAULT_RULES 的事件类型
    # 会产生「记了事件但 event_delta 返回 {} 状态不动」的幽灵——报错而非静默落库。
    import phase6_engine as p6
    et = ev.get("event_type", "")
    if et not in p6.DEFAULT_RULES:
        raise ValueError(
            f"未登记的事件类型: {et!r}（event_type 必须先在 phase6_engine.DEFAULT_RULES "
            f"登记 dims/推导出处后再写入，§5.8/§14）")
    # 序号取 MAX+1（而非 COUNT+1）：事件被部分删除后 COUNT 会回退，导致主键冲突（Phase 15 实测）
    seq = conn.execute("SELECT MAX(CAST(substr(event_id, -3) AS INTEGER)) FROM sim_events "
                       "WHERE sim_id=?", (sim_id,)).fetchone()[0] or 0
    eid = f"{sim_id}-E{seq + 1:03d}"
    conn.execute(
        """INSERT INTO sim_events (event_id, sim_id, day, event_type, summary, severity, importance, source_session, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (eid, sim_id, day, ev.get("event_type", ""), (ev.get("summary") or "").strip(),
         int(ev.get("severity", 0)), int(ev.get("importance", 0)), source_session, pc.now_str()))
    # 同步写分支记忆（episodic）
    sev = int(ev.get("severity", 0))
    conf = EVENT_TYPE_CONF.get(sev, 0.5)
    summary = (ev.get("summary") or "").strip()
    if summary:
        fseq = conn.execute("SELECT MAX(CAST(substr(fact_id, -4) AS INTEGER)) FROM sim_facts "
                            "WHERE sim_id=?", (sim_id,)).fetchone()[0] or 0
        fid = f"{sim_id}-F{fseq + 1:04d}"
        etype = ev.get("event_type", "事件")
        kw = pc.build_keywords_cn(_person_of_summary(summary), etype, summary)
        conn.execute(
            """INSERT INTO sim_facts (fact_id, sim_id, memory_type, subject, relation, object,
                                      conclusion_text, valid_at, source, source_id, confidence, keywords, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, sim_id, "episodic", _person_of_summary(summary), etype, summary,
             f"【模拟事件】{day} {etype}: {summary}", day, "simulation_event", eid, conf, kw, pc.now_str()))
    conn.commit()
    return eid


# ---------------- 分支内关系状态启发式估计（Phase6 前的最小估计量，方法论标 estimate） ----------------
def update_rel_state(conn, sim_id: str, day: str, prev: dict, events: list[dict]) -> dict:
    s = dict(prev)
    for ev in events:
        t = ev.get("event_type", "")
        sev = int(ev.get("severity", 0)) or 1
        if t == "冲突/分歧":
            s["conflict"] += 0.05 * sev
            s["closeness"] -= 0.03 * sev
            s["emotional_safety"] -= 0.04 * sev
            s["comm_quality"] -= 0.03 * sev
        elif t == "冲突修复/和好":
            s["conflict"] -= 0.10 * sev
            s["closeness"] += 0.05
            s["emotional_safety"] += 0.06
            s["trust"] += 0.04
            s["comm_quality"] += 0.05
        elif t == "情绪低谷/安慰":
            s["closeness"] += 0.03
            s["emotional_safety"] += 0.05
        elif t == "见面/出行":
            s["closeness"] += 0.05
            s["comm_quality"] += 0.04
        elif t == "纪念日/承诺":
            s["closeness"] += 0.06
            s["trust"] += 0.05
        elif t == "高兴/庆祝":
            s["closeness"] += 0.03
            s["emotional_safety"] += 0.03
        elif t == "日常陪伴":
            s["closeness"] += 0.02
        elif t == "健康":
            s["emotional_safety"] -= 0.03
        elif t == "学业考试/工作求职":
            s["comm_quality"] += 0.02
    # 轻微回归基线（缓慢漂回）——中性默认值，可被 WorldState 的真实锚点覆盖
    base = {"closeness": 5.0, "conflict": 5.0, "trust": 5.0,
            "emotional_safety": 5.0, "comm_quality": 5.0}
    for k in base:
        s[k] = round(min(10.0, max(0.0, s[k] * 0.99 + base[k] * 0.01)), 3)
    conn.execute(
        """INSERT OR REPLACE INTO sim_rel_state
           (sim_id, day, closeness, conflict, trust, emotional_safety, comm_quality,
            confidence, method, notes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sim_id, day, s["closeness"], s["conflict"], s["trust"],
         s["emotional_safety"], s["comm_quality"], 0.4, "heuristic",
         "Phase6 前分支内估计量（事件驱动+基线回归）"))
    conn.commit()
    return s


# ---------------- 会话 — 事件抽取 ----------------
def extract_session(conn, client, sim_id: str, day: str, session_id: str,
                    transcript: str) -> dict:
    """LLM 从模拟会话抽取事件（隔离落库）"""
    obj = client.extract(system=phase2_llm.SYSTEM_PROMPT,
                         user=summary_user_block(day, transcript),
                         response_model=SessionSummary)
    evs = []
    for ev in obj.events if isinstance(obj.events, list) else []:
        if hasattr(ev, "model_dump"):
            ev = ev.model_dump()
        if not (ev.get("summary") or "").strip():
            continue
        eid = log_event(conn, sim_id, day, ev, session_id)
        evs.append({"event_id": eid, **ev})
    conn.execute("UPDATE sim_sessions SET n_turns=?, topic=? WHERE session_id=?",
                 (len(transcript.splitlines()), obj.topic, session_id))
    conn.commit()
    return {"topic": obj.topic, "events": evs, "salient": obj.salient}


# ---------------- 主循环 ----------------
# Phase 12-A 叙事注入（断联档闭合真实走廊实验，隔离命名空间，不改 R1 规则表）
NARR_DIMS = ("closeness", "trust", "comm_quality", "emotional_safety")
NARR_BASE_DELTA = {"closeness": -0.06, "trust": -0.03, "comm_quality": -0.07, "emotional_safety": -0.025}
NARR_KEYWORDS_A = {"想": 0.15, "思念": 0.22, "难受": 0.15, "放不下": 0.20, "心冷": 0.20, "算了": 0.22,
                   "放弃": 0.22, "低落": 0.15, "好想": 0.15, "emo": 0.12}
NARR_KEYWORDS_B = {"忙": 0.10, "放下": 0.22, "习惯": 0.18, "不再": 0.16, "无所谓": 0.16, "平静": 0.10,
                   "想开": 0.16, "自己的生活": 0.18, "往前看": 0.20}


def _narr_alpha(plan_a: str, plan_b: str, day_idx: int) -> float:
    """叙事强度 0.5~2.5：断联天数斜坡（0~45 天线性升满）＋ A 计划思念/低落关键词 ＋ B 计划疏远/放下关键词 ＋ A 计划负性情绪（snownlp）"""
    ramp = min(1.0, day_idx / 45.0)
    s = 0.5 + 0.7 * ramp
    for w, v in NARR_KEYWORDS_A.items():
        if w in (plan_a or ""):
            s += v
    for w, v in NARR_KEYWORDS_B.items():
        if w in (plan_b or ""):
            s += v
    try:
        from snownlp import SnowNLP
        s += (0.5 - SnowNLP(plan_a or "").sentiments) * 0.8
    except Exception:
        pass
    return round(min(2.5, max(0.5, s)), 3)


def _narr_summary(day_idx: int, plan_a: str, plan_b: str) -> str:
    return (f"【叙事注入】断联第 {day_idx} 天：A 独处（今日心绪：{(plan_a or '').strip()[:60]}）；"
            f"B 各自生活（今日心绪：{(plan_b or '').strip()[:60]}）。两人无互动，关系处于沉默期的回声衰减。")


def run_simulation(conn, world: pc.WorldState, retriever: phase4_retrieval.MemoryRetriever,
                   client, n_days: int = 12, max_turns: int = 8, day_contact_prob: float = 0.7,
                   pcc_mode: str = "full", narrative: dict | None = None,
                   plan_replay: dict | None = None) -> dict:
    """双 Agent 续演核心（Phase 5 A2，P6 R4 已接入）
    Phase 12-A 扩展（向后兼容）: narrative 取 {mode:'nar'} 时，断联无会话天注入『断联期心绪』
    叙事事件（强度由 A/B 每日计划文本的情绪+关键词与断联天数斜坡驱动，隔离命名空间，
    不触碰 R1 规则表，写 sim_rel_state method='engine_narrative'）。
    Phase 12-B 扩展（向后兼容）: narrative 支持 k=全局强度倍数 / breakup={from, daily_close_extra,
    daily_trust_extra}=末段决裂事件；plan_replay={day:{A|B:文本}} 时跳过 LLM daily_plan（离线重放，
    零 token，用于强度扫描，K=1.0 应复现原轨迹）。"""
    agents = {"A": PersonaAgent("A", world), "B": PersonaAgent("B", world)}
    wm = buf.WorkingMemory()
    sid = world.sim_id
    log = {"sim_id": sid, "days": [], "n_messages": 0, "n_events": 0,
           "pcc": {"plans": 0, "reflections": 0}}
    rel = dict(world.rel_state or {"closeness": 5.0, "conflict": 5.0, "trust": 5.0,
                                   "emotional_safety": 5.0, "comm_quality": 5.0})
    prev_day = world.day
    narr_i = 0   # Phase 12-A 断联天数计数（叙事注入用）

    for d in range(n_days):
        world.clock.forward()
        day = world.day
        rec = {"day": day, "session": False}
        # 每日规划（A5 全量：每"天"计划一次；plan_replay 时跳过 LLM 离线重放）
        if pcc_mode == "full":
            yesterday = ""
            for k in ("A", "B"):
                rp = (plan_replay or {}).get(day, {}).get(k)
                if rp is not None:
                    agents[k].plan_today = rp
                    log["pcc"]["plans"] += 1
                    continue
                try:
                    plan = pcc.daily_plan(client, k, agents[k].persona_block,
                                          world.summary_text(with_memories=False),
                                          wm.render(tail=6), yesterday, wm.render())
                    agents[k].plan_today = plan.get("plan", "")
                    pcc.log_pcc(conn, sid, day, "plan", k, agents[k].plan_today)
                    log["pcc"]["plans"] += 1
                except Exception as e:
                    print(f"  [A5/plan] {k} 失败: {str(e)[:60]}", flush=True)
        # 当日是否有会话（确定性随机；反映真实稀疏联系）
        if world.clock.rng.random() >= day_contact_prob:
            rec["no_session"] = "contact_prob"
            rel = p6.update_rel_state_engine(conn, sid, day, rel, [], anchor=world.rel_state)
            if narrative and narrative.get("mode") == "nar":
                # Phase 12-A 叙事注入：断联期心绪（隔离命名空间；不改 R1 规则表）
                # Phase 12-B 扩展：k=全局强度倍数；breakup=末段决裂事件（每天额外衰减）
                narr_i += 1
                nar_k = float(narrative.get("k", 1.0))
                alpha = _narr_alpha(agents["A"].plan_today, agents["B"].plan_today, narr_i)
                dl = {k: round(NARR_BASE_DELTA[k] * alpha * nar_k, 4) for k in NARR_DIMS}
                brk = narrative.get("breakup")
                if brk and narr_i >= int(brk.get("from", 10 ** 9)):
                    dl["closeness"] += float(brk.get("daily_close_extra", 0.0))
                    dl["trust"] += float(brk.get("daily_trust_extra", 0.0))
                summary = _narr_summary(narr_i, agents["A"].plan_today, agents["B"].plan_today)
                if brk and narr_i >= int(brk.get("from", 10 ** 9)):
                    summary = (f"【叙事注入·末段决裂】断联第 {narr_i} 天：A 情绪反复后逐渐心冷、"
                               f"不再期待（今日心绪：{(agents['A'].plan_today or '').strip()[:50]}）；"
                               f"这段关系在沉默中走向冷却的尾声。")
                conn.execute(
                    "INSERT INTO sim_events (event_id, sim_id, day, event_type, summary, severity, "
                    "importance, source_session, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{sid}-NV{narr_i:03d}", sid, day, "断联期心绪", summary,
                     int(round(alpha * 2)) + 1, 3, None, pc.now_str()))
                for k in NARR_DIMS:
                    rel[k] = round(min(10.0, max(0.0, rel[k] + dl[k])), 3)
                conn.execute(
                    "UPDATE sim_rel_state SET closeness=?, conflict=?, trust=?, emotional_safety=?, "
                    "comm_quality=?, notes=? WHERE sim_id=? AND day=?",
                    (rel["closeness"], rel["conflict"], rel["trust"], rel["emotional_safety"],
                     rel["comm_quality"], "engine_narrative(Phase12-A)", sid, day))
                conn.commit()
                rec["narrative"] = {"alpha": alpha, "delta": dl}
            log["days"].append(rec)
            prev_day = day
            continue
        # —— 会话 ——
        session_seq = conn.execute("SELECT COUNT(*) FROM sim_sessions WHERE sim_id=?",
                                   (sid,)).fetchone()[0]
        session_id = f"{sid}-S{session_seq + 1:03d}"
        initiator = "A" if world.clock.next_int(0, 1) == 0 else "B"
        conn.execute(
            "INSERT INTO sim_sessions (session_id, sim_id, seq, day, n_turns, initiator, created_at) "
            "VALUES (?,?,?,?,0,?,?)",
            (session_id, sid, session_seq + 1, day, initiator, pc.now_str()))
        conn.commit()

        transcript_lines, last_msg = [], ""
        active = initiator
        div_guarded = False   # 分歧护栏：首个（发生在分歧点后的）会话首回合必须体现改写
        # 首回合用计划作"想聊的话题"提示
        planner_hint = agents[active].plan_today or ""
        for turn in range(max_turns):
            active = initiator if turn % 2 == 0 else ("B" if initiator == "A" else "A")
            other = "B" if active == "A" else "A"
            a = agents[active]
            ws_text = world.summary_text(with_memories=False)
            scene = world.divergence_scene()     # 分歧日首个回合注入改写场景卡
            if scene:
                ws_text = ws_text + "\n" + scene
            query = (f"{day} " + (planner_hint or last_msg or f"{a.name} 今日安排"))[:80]
            mem_text = retrieve_memories(retriever, conn, world, query, day, top_k=5)
            buffer_text = wm.render()
            hint = f"（今日计划：{planner_hint}）" if planner_hint else ""
            reply = a.act(client, ws_text, mem_text, buffer_text + hint, last_msg,
                          rng=world.clock.rng, full_persona=(turn == 0))
            # 分歧护栏：改写件事后首个会话首回合若未体现改写 → 带指令重写一次
            if scene and not div_guarded:
                div_guarded = True
                body = (reply.reply or "") + " " + (reply.action or "")
                if not any(w in body for w in REWRITE_KEYWORDS):
                    guard_extra = ("【重写】今天是改写后的第一天，请务必让今日互动**从回应这个决定开始**："
                                   "例如对方表明愿意再试一次/提出新的相处方式，或你收到回应后给出"
                                   "符合人格的真实反应。仍然按你的真实人格说话，但话题必须从改写决定出发。")
                    try:
                        reply2 = a.act(client, ws_text, mem_text,
                                       buffer_text + hint + "\n" + guard_extra, last_msg,
                                       rng=world.clock.rng, full_persona=True)
                        reply.reply = reply2.reply or reply.reply
                        reply.action = reply2.action or reply.action
                        reply.emotion = reply2.emotion or reply.emotion
                        reply.medium = getattr(reply2, "medium", reply.medium)
                    except Exception:
                        pass
            msg_id = f"{session_id}-T{turn + 1:03d}"
            medium = getattr(reply, "medium", "text") or "text"
            conn.execute(
                """INSERT INTO sim_messages (msg_id, sim_id, session_id, day, turn_idx, sender,
                                             content, emotion_s, meta, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (msg_id, sid, session_id, day, turn + 1, active, reply.reply,
                 json.dumps(a.s_state, ensure_ascii=False),
                 json.dumps({"action": reply.action, "medium": medium}, ensure_ascii=False), pc.now_str()))
            conn.commit()
            wm.add(day, active, reply.reply)
            disp = reply.reply
            if medium == "image":
                disp = f"（发图）{disp[:40]}"
            elif medium == "emoji":
                disp = f"（表情包）{disp[:24]}"
            elif medium == "voice":
                disp = f"（语音消息）{disp[:24]}"
            line = f"[{day} {SENDER_NAME[active]}] {disp}"
            transcript_lines.append(line)
            if reply.action:
                transcript_lines.append(f"[{day} {SENDER_NAME[active]}·行动] {reply.action}")
            last_msg = reply.reply
            log["n_messages"] += 1
            planner_hint = ""
            if turn and run_budget_exceeded(wm):
                break
        # 事件抽取
        transcript = "\n".join(transcript_lines)
        try:
            sess = extract_session(conn, client, sid, day, session_id, transcript)
            rec["topic"] = sess["topic"]
            rec["n_events"] = len(sess["events"])
            log["n_events"] += len(sess["events"])
        except Exception as e:
            print(f"  [event-extract] {day} 失败: {str(e)[:80]}", flush=True)
            rec["topic"] = ""
        # 关系状态更新（Phase 6 R4: 正式增量模型 method='engine'，替换原启发式 update_rel_state）
        evs_cur = conn.execute(
            "SELECT event_type, summary, severity FROM sim_events WHERE sim_id=? AND day=?",
            (sid, day)).fetchall()
        evs = [{"event_type": r[0], "summary": r[1], "severity": r[2]} for r in evs_cur]
        rel = p6.update_rel_state_engine(conn, sid, day, rel, evs, anchor=world.rel_state)
        rec["rel_state"] = {k: rel[k] for k in ("closeness", "conflict", "trust",
                                                "emotional_safety", "comm_quality")}
        # A5 反思（全量：每会话后对双 Agent 各一次）
        if pcc_mode == "full":
            for k in ("A", "B"):
                try:
                    rf = pcc.reflect(client, k, agents[k].persona_block,
                                     "\n".join(transcript_lines),
                                     world.summary_text(with_memories=False))
                    agents[k].calib = rf.get("calibration") or agents[k].calib
                    pcc.log_pcc(conn, sid, day, "reflection", k, rf.get("content", ""),
                                rf.get("deviations", []))
                    if rf.get("calibration"):
                        pcc.log_pcc(conn, sid, day, "calibration", k, rf["calibration"])
                        log["pcc"]["reflections"] += 1
                except Exception as e:
                    print(f"  [A5/reflect] {k} 失败: {str(e)[:60]}", flush=True)
        # 缓冲压缩（A3）：按字符预算 或 条目数>48 触发折叠，保证后续会话窗口不被历史挤占
        if (wm.current_chars > buf.DEFAULT_BUDGET or wm.entry_count > 48) and wm.entry_count > 8:
            wm._fold_oldest(client=client, conn=conn, sim_id=sid, day=day)
        rec["session"] = True
        log["days"].append(rec)
        prev_day = day
        touched = [t for t in ("topic", "n_events", "session") if t in rec]
        print(f"[A2] {day} 会话{'开' if rec['session'] else '无'} topic={rec.get('topic','')[:20]} "
              f"n_events={rec.get('n_events', 0)} rel=({rec.get('rel_state', {}).get('closeness')}) "
              f"msgs={log['n_messages']}", flush=True)
    pc.finish_sim(conn, sid, prev_day)
    return log


def run_budget_exceeded(wm) -> bool:
    """回合数预算软上限：缓冲长度超 60 条即收尾，防无限循环"""
    return wm.entry_count >= 60


def load_world(conn, sim_id: str) -> pc.WorldState:
    row = conn.execute(
        "SELECT sim_id, branch_name, branch_of, divergence_point, divergence_desc, rewritten_choice, "
        "       start_day, clock_seed FROM sim_runs WHERE sim_id=?", (sim_id,)).fetchone()
    if not row:
        raise ValueError(f"无此 sim: {sim_id}")
    (sid, bname, bof, divp, divd, rw, start_day, seed) = row
    return pc.build_world(conn, start_day=start_day, seed=seed or 0,
                          branch_name=bname, branch_of=bof, divergence_point=divp,
                          divergence_desc=divd, rewritten_choice=rw, sim_id=sid)


def main():
    ap = argparse.ArgumentParser(description="IfWe Phase 5 A2 双 Agent 会话循环")
    ap.add_argument("--smoke", action="store_true", help="2 天 × 3 回合冒烟")
    ap.add_argument("--sim", type=str, default=None, help="已冻结的 sim_id（缺省用最新）")
    ap.add_argument("--days", type=int, default=12)
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--pcc", type=str, default="full", choices=["full", "none"])
    args = ap.parse_args()

    pc.RESULT_DIR.mkdir(parents=True, exist_ok=True)
    conn = pc.connect()
    pc.apply_all_schemas(conn)
    if args.smoke:
        args.days, args.max_turns = 2, 3
    if args.sim:
        world = load_world(conn, args.sim)
    else:
        sid = conn.execute("SELECT sim_id FROM sim_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        world = load_world(conn, sid[0]) if sid else None
        if world is None:
            raise SystemExit("无 sim 记录：先跑 phase5_a1_world.py 冻结世界")
    print(f"[A2] 加载世界 {world.sim_id} day={world.day} 分支={world.branch_name}")
    retriever = phase4_retrieval.MemoryRetriever()
    client = phase5_llm_get_client()
    t0 = time.time()
    log = run_simulation(conn, world, retriever, client,
                         n_days=args.days, max_turns=args.max_turns, pcc_mode=args.pcc)
    log["elapsed_s"] = round(time.time() - t0, 1)
    out = pc.RESULT_DIR / f"a2_run_{world.sim_id}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in log.items() if k != "days"}, ensure_ascii=False, indent=2))
    print("saved:", out)
    conn.close()


def phase5_llm_get_client():
    from phase5_llm import get_client
    return get_client()


if __name__ == "__main__":
    main()