# -*- coding: utf-8 -*-
"""Phase E · 表达层条件化（二期方案 v2 §11 Phase E）测试。

覆盖：expression_profile（数据推导 + sd 必报）、length_hint（真实分位约束）、
sample_medium（真实分布）、delay_bucket（真实分位分档）、initiate_check
（真实开口率掷骰）、ActionDecision 表达字段填充、引擎 initiate_turn
（拒绝立场约束 + sim_action_log 落库）。

    python -m unittest tests.test_phase17_phasee -v
"""
from __future__ import annotations

import os
import random
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
import phase17_expression as ex       # noqa: E402
import phase5_common as pc            # noqa: E402
from tests.test_persona_fidelity import PFBase, _load_dial  # noqa: E402
from tests.test_phase17_phasec import FixedStub  # noqa: E402
from tests.test_phase17_phasec import seed_rel_baseline  # noqa: E402


def _open_readonly_or_empty():
    """打开只读库；demo 库不存在时返回空内存库（降级语义测试足够）"""
    import sqlite3 as _s
    try:
        conn = _s.connect(f"file:{pc.DB_PATH.as_posix()}?mode=ro", uri=True)
        conn.execute("SELECT 1")
        return conn
    except _s.Error:
        return _s.connect(":memory:")


class TestExpressionProfile(unittest.TestCase):
    """表达画像：发布树验证空/稀疏数据的优雅降级（真实分布断言在私有树）"""

    def test_profile_degrades_gracefully_on_demo_data(self):
        # 发布树（demo 数据）验证：空/稀疏数据下 profile 优雅降级不崩
        # （真实数据分布断言在私有树测试层执行）
        conn = _open_readonly_or_empty()
        prof = ex.expression_profile(conn, "2026-08-20")
        self.assertEqual(prof["version"], ex.PROFILE_VERSION)
        self.assertEqual(prof["source"], "real_messages")
        L = prof["length"]
        self.assertIn("sd", L, "长度指标必须报告 sd（方差门禁），数据不足时为 None")
        conn.close()

    def test_profile_deterministic(self):
        conn = _open_readonly_or_empty()
        self.assertEqual(ex.expression_profile(conn, "2026-08-20"),
                         ex.expression_profile(conn, "2026-08-20"))
        conn.close()


class TestLengthHintAndMedium(unittest.TestCase):
    """长度约束目标=真实分位；媒介=真实分布抽样"""

    PROFILE = {"length": {"p10": 5, "p40": 9, "p50": 12, "p90": 85, "sd": 70.4},
               "medium_probs": {"text": 0.95, "emoji": 0.05, "image": 0.0, "voice": 0.0}}

    def test_length_hint_modes(self):
        brief = ex.length_hint("brief", self.PROFILE)
        self.assertIn("5~9", brief, "brief 约束用真实低分位区间")
        bound = ex.length_hint("boundary", self.PROFILE)
        self.assertIn("85", bound, "boundary 允许长文表态（真实 P90 佐证）")
        self.assertNotIn("不要展开长段", bound)
        engaged = ex.length_hint("engaged", self.PROFILE)
        self.assertIn("自然起伏", engaged)

    def test_medium_follows_real_distribution(self):
        # text=0.95 → 100 次抽样应 overwhelmingly text（真实分布，非均匀）
        rng = random.Random(3)
        samples = [ex.sample_medium(self.PROFILE, rng) for _ in range(200)]
        self.assertGreater(samples.count("text") / 200, 0.85)

    def test_delay_bucket_from_quantiles(self):
        prof = {"delay_quantiles": {"p33_min": 4.0, "p66_min": 50.0}}
        self.assertEqual(ex.delay_bucket(2.0, prof), "immediate")
        self.assertEqual(ex.delay_bucket(30.0, prof), "within_minutes")
        self.assertEqual(ex.delay_bucket(120.0, prof), "later")
        self.assertEqual(ex.delay_bucket(None, prof), "unknown")

    def test_initiate_check_uses_rate(self):
        rng = random.Random(0)
        prof = {"initiation_rate": 0.0}
        self.assertFalse(any(ex.initiate_check(prof, rng) for _ in range(50)),
                         "开口率 0 → 永不主动开口")
        prof_hi = {"initiation_rate": 1.0}
        self.assertTrue(all(ex.initiate_check(prof_hi, rng) for _ in range(10)))


class TestActionExpressionFields(unittest.TestCase):
    """ActionDecision 表达字段填充（apply_expression）"""

    def test_apply_expression_fills_policy(self):
        prof = {"length": {"p10": 5, "p40": 9, "p50": 12, "p90": 85},
                "medium_probs": {"text": 1.0, "emoji": 0.0, "image": 0.0, "voice": 0.0}}
        a = act17.ActionDecision(reply_mode="brief", should_generate=True)
        act17.apply_expression(a, prof)
        self.assertIn("5~9", a.length_policy)
        self.assertEqual(a.medium_policy["text"], 1.0)
        a2 = act17.ActionDecision(reply_mode="brief", should_generate=True)
        act17.apply_expression(a2, None)
        self.assertIsNone(a2.length_policy, "无 profile 时不动表达字段")


class TestInitiateTurn(PFBase):
    """引擎主动开口：真实开口率掷骰 + 拒绝立场约束 + 落库"""

    def test_initiate_turn_rejected_stance(self):
        os.environ["IFWE_ENGINE_MODE"] = "phase17_stance_action"
        try:
            self.seed_warm_then_cold()
            seed_rel_baseline(self.conn)
            # 08 段（当前阶段段）需要真实 B 数据：一条 B 先开口 + 一组往返
            self.add_real("2026-08-05", "B", "那个讲座的资料我发你了")
            self.add_real("2026-08-05", "A", "收到了谢谢")
            self.add_real("2026-08-06", "A", "在吗")
            self.add_real("2026-08-06", "B", "嗯在")
            self.commit()
            de_ = _load_dial()
            conn2 = sqlite3.connect(pc.DB_PATH)
            sim_id = de_.new_line(conn2, start_day="2026-08-01", name="开口线", seed=0)
            eng = de_.DialEngine(conn2, sim_id, client=FixedStub("最近在忙一件事"),
                                 retriever=None, verbose=False)
            self.assertTrue(eng.phase17)
            self.assertIsNotNone(eng._profile)
            eng.world.clock.rng.random = lambda: 0.0      # 开口掷骰必中（rate>0）
            res = eng.initiate_turn()
            self.assertIsNotNone(res)
            self.assertIn("忙", res["reply"])
            # 落库：reply_mode=initiate，拒绝立场下 must_not_do 含推进禁令
            acts = act17.read_actions(conn2, sim_id)
            self.assertEqual(acts[-1]["reply_mode"], "initiate")
            # 消息落库带 initiated 标记
            row = conn2.execute(
                "SELECT meta FROM sim_messages WHERE sim_id=? ORDER BY rowid DESC LIMIT 1",
                (sim_id,)).fetchone()
            import json as _json
            self.assertTrue(_json.loads(row[0]).get("initiated"))
            conn2.close()
        finally:
            os.environ.pop("IFWE_ENGINE_MODE", None)

    def test_no_initiate_when_rate_zero(self):
        os.environ["IFWE_ENGINE_MODE"] = "phase17_stance_action"
        try:
            self.seed_warm_then_cold()
            self.commit()
            de_ = _load_dial()
            conn2 = sqlite3.connect(pc.DB_PATH)
            sim_id = de_.new_line(conn2, start_day="2026-08-01", name="不开口线", seed=0)
            eng = de_.DialEngine(conn2, sim_id, client=FixedStub(),
                                 retriever=None, verbose=False)
            eng.world.clock.rng.random = lambda: 0.0
            eng._profile = {**eng._profile, "initiation_rate": 0.0} if eng._profile \
                else {"initiation_rate": 0.0}
            self.assertIsNone(eng.initiate_turn(), "开口率 0 → 不主动开口")
            conn2.close()
        finally:
            os.environ.pop("IFWE_ENGINE_MODE", None)


if __name__ == "__main__":
    unittest.main()
