# -*- coding: utf-8 -*-
"""Phase C · 立场/场景/行动决策 MVP + 关系增量通道 + 护栏修复（二期方案 v2 §11 Phase C）

回归清单（§13.4）：
  T9  20 轮中性话题 → closeness 不单调上升（§5.8）
  T10 连发 3 轮同一立场（boundary）→ 不被去重护栏改写（§11 Phase C）
  T11 未登记 event_type → 写入报错而非静默落库（§5.8）
外加：立场推断证据链 / 门控语义 / ActionDecision 落库 / generation_violation /
三模式（legacy | phase16_fixes | phase17_stance_action）冒烟。

    python -m unittest tests.test_phase17_phasec -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import phase15_dial_engine as de      # noqa: E402
import phase17_action as act17        # noqa: E402
import phase17_stance as st17         # noqa: E402
import phase5_a2_loop as loop         # noqa: E402
import phase6_engine as p6            # noqa: E402
import phase5_common as pc            # noqa: E402
from phase5_llm import AgentReply     # noqa: E402
from tests.test_persona_fidelity import PFBase, _load_dial  # noqa: E402

REJECT_START = "2026-08-01"   # fixture 证据日（2026-07-20）之后           # 测试证据标注日（fixture）之后 → 证据在场


# ---------------- 发布树适配：证据用中性 fixture（发布树不得携带真实决策点） ----------------
_FAKE_EVIDENCE = [
    {"id": "TEST-REJECT", "day": "2026-07-20", "label": "测试决策点",
     "note": "测试用中性占位（不含任何真实事件信息）",
     "stance_evidence": {
         "relationship_status": "非恋爱关系",
         "romantic_intent": "明确拒绝",
         "contact_willingness": "被动低频",
         "initiation_willingness": "低",
         "topic_scope": "中性事务可以酌情简短回应；情感推进不接受",
         "boundary_strength": "高",
         "boundary_consequence": "重申边界 / 敷衍 / 不回复",
         "affect_toward_partner": "仍有基本善意，但这不等于愿意恢复关系"}},
]

_orig_load_evidence = st17._load_evidence


def _patch_evidence(on: bool) -> None:
    st17._load_evidence = (lambda: list(_FAKE_EVIDENCE)) if on else _orig_load_evidence


def seed_rel_baseline(conn, period: str = "2026-08") -> None:
    """给 temp DB 播一条基线关系状态（world.rel_state / rel_current 的来源）"""
    conn.execute(
        "INSERT OR REPLACE INTO relationship_state "
        "(period, closeness, conflict, trust, emotional_safety, comm_quality, confidence) "
        "VALUES (?,?,?,?,?,?,?)", (period, 4.6, 3.8, 4.2, 3.5, 6.9, 0.6))
    conn.commit()


class FixedStub(de.StubClient):
    """固定回复的桩（可复现、可重复生成相同文本）"""

    def __init__(self, reply: str = "嗯，看到了"):
        self.reply = reply

    def extract(self, system: str = "", user: str = "", response_model=None, **kw):
        if response_model is AgentReply:
            return AgentReply(reply=self.reply, medium="text",
                              emotion={"label": "平静", "level": 5.0}, action="")
        try:
            return response_model()
        except Exception:
            return None


class TestStanceInference(unittest.TestCase):
    """立场推断：证据链来自 decision_points.json + P_anchor，代码无日期分支"""

    def test_rejection_evidence_after_s10b(self):
        _patch_evidence(True)
        self.addCleanup(_patch_evidence, False)
        conn = sqlite3.connect(":memory:")
        st = st17.infer_stance(conn, REJECT_START, p_anchor=0.05)
        self.assertEqual(st["romantic_intent"], "明确拒绝",
                         "拒绝类证据在场 → 明确拒绝")
        self.assertEqual(st["relationship_status"], "非恋爱关系")
        self.assertEqual(st["p_band"], "极低", "P_anchor=0.05 → 极低档")
        self.assertIn("TEST-REJECT", st["evidence_ids"], "证据 id 必须可溯源")
        conn.close()

    def test_no_evidence_before(self):
        _patch_evidence(True)
        self.addCleanup(_patch_evidence, False)
        conn = sqlite3.connect(":memory:")
        st = st17.infer_stance(conn, "2025-01-01", p_anchor=0.6)
        self.assertEqual(st["romantic_intent"], "未表态",
                         "证据不在场时不得臆测（不误判）")
        self.assertEqual(st["evidence_ids"], [])
        conn.close()

    def test_gate_semantics(self):
        st_reject = {"romantic_intent": "明确拒绝", "p_band": "极低"}
        st_warm = {"romantic_intent": "未表态", "p_band": "高"}
        d_daily = {"closeness": 0.015, "comm_quality": 0.015}
        self.assertEqual(st17.gated_delta(st_reject, d_daily),
                         {"closeness": 0.0, "comm_quality": 0.0},
                         "拒绝立场：普通话题正向增量门控为 0（T9 核心）")
        d_conflict = {"conflict": 0.080, "closeness": -0.050}
        gd = st17.gated_delta(st_reject, d_conflict)
        self.assertEqual(gd["conflict"], 0.080, "冲突恶化在任何立场下照常")
        self.assertEqual(gd["closeness"], -0.050, "事件自带负增量照常")
        self.assertEqual(st17.gated_delta(st_warm, d_daily), d_daily,
                         "warm 立场不门控（亲密期不被压成沉默/不改变升温行为）")


class TestT9NeutralNoHeating(PFBase):
    """T9（§5.8）：phase17 模式下 20 轮中性话题 → closeness 不单调上升"""

    def test_20_neutral_rounds_do_not_heat(self):
        os.environ["IFWE_ENGINE_MODE"] = "phase17_stance_action"
        _patch_evidence(True)
        try:
            self.seed_warm_then_cold()
            seed_rel_baseline(self.conn)
            self.commit()
            de_ = _load_dial()
            conn2 = sqlite3.connect(pc.DB_PATH)
            sim_id = de_.new_line(conn2, start_day=REJECT_START, name="中性线", seed=0)
            eng = de_.DialEngine(conn2, sim_id, client=FixedStub(),
                                 retriever=None, verbose=False)
            self.assertTrue(eng.phase17)
            self.assertEqual(eng._stance["romantic_intent"], "明确拒绝")
            eng.world.clock.rng.random = lambda: 0.0       # 确定性回复
            closeness = []
            for i in range(20):
                res = eng.say(f"今天天气不错{i}")
                closeness.append(res["rel"]["closeness"])
            rises = sum(1 for a, b in zip(closeness, closeness[1:]) if b > a)
            self.assertEqual(rises, 0,
                             f"拒绝立场下中性话题不得加热 closeness: {closeness[:5]}...")
            self.assertLessEqual(max(closeness), closeness[0] + 1e-9)
            # 行动决策已落库
            acts = act17.read_actions(conn2, sim_id)
            self.assertEqual(len(acts), 20)
            self.assertTrue(all(a["reply_mode"] == "brief" for a in acts),
                            "普通话题×低联系档 → brief")
            conn2.close()
        finally:
            _patch_evidence(False)
            os.environ.pop("IFWE_ENGINE_MODE", None)


class TestT10BoundaryNotRewritten(PFBase):
    """T10：连发 3 轮同一立场（boundary）→ 不被去重护栏改写"""

    def _agent(self):
        world = pc.build_world(self.conn, "2026-07-17",
                               branch_name="测试", sim_id="SIM-T10")
        return loop.PersonaAgent("B", world)

    def test_boundary_rounds_survive_guard(self):
        ag = self._agent()
        repeated = "我不会再讨论复合这件事了，我们就这样吧，请你尊重我的决定。"
        similar_prev = "我不会再讨论复合这件事了，我们就这样吧，请你尊重我的决定呀。"
        ag.recent_texts = [similar_prev]          # 近期自己消息与本次高度相似（bigram≥0.45）
        action = act17.ActionDecision(reply_mode="boundary", should_generate=True)

        class SameStub:
            def extract(self, system="", user="", response_model=None, **kw):
                if response_model is AgentReply:
                    return AgentReply(reply=repeated, medium="text",
                                      emotion={"label": "平静", "level": 5.0}, action="")
                return response_model()

        for k in range(3):                        # 连发 3 轮同一立场
            obj = ag.act(SameStub(), "", "", "", "我们再试试好不好", action=action)
            self.assertEqual(obj.reply, repeated,
                             f"boundary 立场重申第 {k+1} 轮不得被护栏改写为『换个话头』")
        # 对照：非 boundary 行动 → 护栏生效（触发重写路径，stub 二次返回不同文本）
        class RewriteStub(SameStub):
            def extract(self, system="", user="", response_model=None, **kw):
                obj = super().extract(system, user, response_model, **kw)
                if "重写要求" in (user or ""):
                    obj.reply = "换个角度说：最近就先这样，各自安好。"
                return obj

        ag2 = self._agent()
        ag2.recent_texts = [similar_prev]
        neutral = act17.ActionDecision(reply_mode="neutral", should_generate=True)
        obj = ag2.act(RewriteStub(), "", "", "", "我们再试试好不好", action=neutral)
        self.assertNotEqual(obj.reply, repeated, "非 boundary 行动护栏照常生效")


class TestT11RegistrationCheck(PFBase):
    """T11（§5.8）：未登记 event_type → 报错而非静默落库"""

    def test_unregistered_event_type_raises(self):
        conn = self.conn            # PFBase harness：完整 sim_events/sim_facts schema
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS sim_events (event_id TEXT PRIMARY KEY, sim_id TEXT,
               day TEXT, event_type TEXT, summary TEXT, severity INTEGER,
               importance INTEGER, source_session TEXT, created_at TEXT);
               CREATE TABLE IF NOT EXISTS sim_facts (fact_id TEXT PRIMARY KEY, sim_id TEXT,
               day TEXT, memory_type TEXT, subject TEXT, predicate TEXT, object TEXT,
               conclusion_text TEXT, confidence REAL, source_session TEXT, created_at TEXT);""")
        conn.commit()
        with self.assertRaises(ValueError):
            loop.log_event(conn, "SIM-T11", "2026-07-17",
                           {"event_type": "不存在的事件", "summary": "x"}, "S001")
        n = conn.execute("SELECT COUNT(*) FROM sim_events").fetchone()[0]
        self.assertEqual(n, 0, "未登记类型不得静默落库")
        # 已登记类型（含新增的 边界重申）可正常写入
        eid = loop.log_event(conn, "SIM-T11", "2026-07-17",
                             {"event_type": "边界重申", "summary": "重申边界", "severity": 3,
                              "importance": 4}, "S001")
        self.assertTrue(eid)
        conn.close()

    def test_boundary_event_registered_with_derivation(self):
        rule = p6.DEFAULT_RULES["边界重申"]
        self.assertTrue(rule.get("synthetic"), "合成事件必须标注 synthetic（推导出处）")
        d = p6.RelEngine().event_delta({"event_type": "边界重申",
                                        "severity": 3, "importance": 4})
        self.assertLess(d["closeness"], 0, "边界重申：亲密不升（拒绝推进）")
        self.assertGreater(d["conflict"], 0, "边界重申：触碰被指出，冲突小幅上升")


class TestActionDecision(PFBase):
    """ActionDecision MVP：决策语义 + 落库 + violation"""

    def _reject_stance(self):
        # 显式构造（decide 的决策语义测试；不依赖证据文件）
        return {"relationship_status": "非恋爱关系", "romantic_intent": "明确拒绝",
                "contact_willingness": "被动低频", "p_band": "极低",
                "evidence_ids": ["TEST-REJECT"]}

    def test_decide_boundary(self):
        st = self._reject_stance()
        scene = {"scene_mode": "关系话题", "boundary_pressure": "high"}
        a = act17.decide(st, scene, {}, p_reply=0.9,
                         rng=type("R", (), {"random": staticmethod(lambda: 0.0)})())
        self.assertEqual(a.reply_mode, "boundary")
        self.assertEqual(a.event_type, "边界重申")
        self.assertIn("推进关系", a.must_not_do)

    def test_decide_silent_on_low_p(self):
        st = self._reject_stance()
        scene = {"scene_mode": "普通话题", "boundary_pressure": "low"}
        a = act17.decide(st, scene, {}, p_reply=0.1,
                         rng=type("R", (), {"random": staticmethod(lambda: 0.5)})())
        self.assertEqual(a.reply_mode, "none")
        self.assertFalse(a.should_generate)

    def test_violation_detection(self):
        a = act17.ActionDecision(reply_mode="boundary", should_generate=True)
        v, note = act17.check_generation_violation(a, "那我们复合吧，重新开始。")
        self.assertEqual(v, 1)
        self.assertIn("推进", note)
        v2, _ = act17.check_generation_violation(a, "我再说一次，我们不可能了。")
        self.assertEqual(v2, 0, "不含推进表述的边界重申不算违规")

    def test_action_logged_and_violation_marked(self):
        os.environ["IFWE_ENGINE_MODE"] = "phase17_stance_action"
        _patch_evidence(True)
        try:
            self.seed_warm_then_cold()
            seed_rel_baseline(self.conn)
            self.commit()
            de_ = _load_dial()
            conn2 = sqlite3.connect(pc.DB_PATH)
            sim_id = de_.new_line(conn2, start_day=REJECT_START, name="行动线", seed=0)
            eng = de_.DialEngine(conn2, sim_id, client=FixedStub("那我们复合吧。"),
                                 retriever=None, verbose=False)
            eng.world.clock.rng.random = lambda: 0.0
            res = eng.say("我们重新在一起好不好")     # 关系话题 × 拒绝 → boundary
            acts = act17.read_actions(conn2, sim_id)
            self.assertEqual(len(acts), 1)
            self.assertEqual(acts[0]["reply_mode"], "boundary")
            self.assertEqual(acts[0]["generation_violation"], 1,
                             "boundary 轮回复含推进表述 → generation_violation 可诊断")
            self.assertEqual(res["event"]["event_type"], "边界重申")
            conn2.close()
        finally:
            _patch_evidence(False)
            os.environ.pop("IFWE_ENGINE_MODE", None)


class TestEngineModes(unittest.TestCase):
    """三模式映射（§12：不新增第三个正交开关；各模式可回退）"""

    def test_mode_mapping(self):
        cases = [
            ("phase17_stance_action", None, "phase17_stance_action"),
            ("legacy", None, "legacy"),
            ("phase16_fixes", None, "phase16_fixes"),
            (None, "0", "legacy"),                       # 旧开关向后兼容
            (None, None, "phase16_fixes"),
        ]
        for mode_env, fidelity_env, expect in cases:
            if mode_env is not None:
                os.environ["IFWE_ENGINE_MODE"] = mode_env
            else:
                os.environ.pop("IFWE_ENGINE_MODE", None)
            if fidelity_env is not None:
                os.environ["IFWE_PERSONA_FIDELITY"] = fidelity_env
            else:
                os.environ.pop("IFWE_PERSONA_FIDELITY", None)
            self.assertEqual(de._engine_mode(), expect,
                             f"ENGINE_MODE={mode_env} FIDELITY={fidelity_env}")
        os.environ.pop("IFWE_ENGINE_MODE", None)
        os.environ.pop("IFWE_PERSONA_FIDELITY", None)


class TestPhaseCIntegration(PFBase):
    """phase17 模式端到端：run_simulation 兼容 + 主库零侵入（复验 Phase B 断言不回退）"""

    def test_phase17_turns_leave_main_tables_untouched(self):
        os.environ["IFWE_ENGINE_MODE"] = "phase17_stance_action"
        _patch_evidence(True)
        try:
            self.seed_warm_then_cold()
            seed_rel_baseline(self.conn)
            self.commit()
            de_ = _load_dial()
            conn2 = sqlite3.connect(pc.DB_PATH)
            before = {t: conn2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("messages", "facts", "events", "relationship_state")}
            sim_id = de_.new_line(conn2, start_day=REJECT_START, name="集成线", seed=0)
            eng = de_.DialEngine(conn2, sim_id, client=FixedStub(),
                                 retriever=None, verbose=False)
            eng.world.clock.rng.random = lambda: 0.0
            for i in range(3):
                eng.say(f"中性话题{i}")
            after = {t: conn2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("messages", "facts", "events", "relationship_state")}
            self.assertEqual(before, after)
            conn2.close()
        finally:
            _patch_evidence(False)
            os.environ.pop("IFWE_ENGINE_MODE", None)


if __name__ == "__main__":
    unittest.main()
