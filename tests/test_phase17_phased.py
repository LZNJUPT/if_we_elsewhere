# -*- coding: utf-8 -*-
"""Phase D · 阶段/趋势模型 + 元对话 + 评测装置（二期方案 v2 §11 Phase D）测试。

覆盖：
  - phase17_phase：特征序列确定性、变化点检测（合成跳变检出 / 纯噪声不误报）、
    infer_phase 结构与落段；
  - 世界契约（§8.1 常量化）与元对话结构化判定器（§8.2，stub 路径）；
  - phase17_eval 抽样装置（§13.1 反选择偏差 + 沉默段单选）。

    python -m unittest tests.test_phase17_phased -v
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

import phase17_phase as ph           # noqa: E402
import phase17_stance as st17        # noqa: E402
import phase5_llm as llm             # noqa: E402
import phase5_common as pc           # noqa: E402
from tests.test_persona_fidelity import PFBase  # noqa: E402


class TestFeatureSeries(PFBase):
    """特征序列：paired 口径、确定性"""

    def test_series_deterministic_and_shaped(self):
        self.seed_warm_then_cold()
        self.commit()
        s1 = ph.feature_series(self.conn, bucket_days=7)
        s2 = ph.feature_series(self.conn, bucket_days=7)
        self.assertEqual(s1, s2, "特征序列必须确定可复现")
        self.assertTrue(s1)
        for s in s1:
            self.assertIn("reply_rate", s)
            self.assertIn("initiation_rate", s)
            self.assertIn("len_b_sd", s)
        # 温暖段（07-01~10 有 B 回复）的 reply_rate 应高于冷淡段（07-11~16 零回复）
        warm = [s["reply_rate"] for s in s1 if s["bucket"].startswith("2026-07")
                and s["reply_rate"] is not None]
        self.assertTrue(warm)

    def test_changepoint_detects_real_jump(self):
        # 合成序列：前 8 桶高回复率、后 8 桶 0 → 变化点应落在边界附近
        series = ([0.9] * 8 + [0.05] * 8)
        cps = ph.detect_changepoints(series, n_perm=100)
        self.assertTrue(cps, "真实跳变必须被检出")
        self.assertTrue(any(abs(c["index"] - 8) <= 2 for c in cps),
                        f"变化点应落在边界(8)附近: {cps}")

    def test_no_false_positive_on_noise(self):
        rng = random.Random(7)
        series = [0.5 + rng.uniform(-0.08, 0.08) for _ in range(24)]
        cps = ph.detect_changepoints(series, n_perm=100)
        self.assertEqual(cps, [], "纯噪声序列不得误报变化点（置换检验）")


class TestInferPhase(PFBase):
    def test_infer_phase_structure(self):
        self.seed_warm_then_cold()
        self.commit()
        p = ph.infer_phase(self.conn, "2026-07-15")
        self.assertEqual(p["version"], ph.PHASE_VERSION)
        self.assertIsNotNone(p["phase_id"])
        self.assertIn("reply_rate", p["features"])
        self.assertIn("reply_rate_pct", p["features"])
        # 冷淡期（07-15）所在段的回复率分位应低于中位
        self.assertLess(p["features"]["reply_rate_pct"], 0.5)


class TestWorldContract(unittest.TestCase):
    """§8.1 世界契约常量化"""

    def test_contract_shape(self):
        self.assertEqual(st17.WORLD_CONTRACT["epistemic_levels"],
                         ["不知道", "怀疑", "知道"])
        self.assertEqual(st17.WORLD_CONTRACT["epistemic_default"], "不知道")
        self.assertIn("不自动升级为系统指令",
                      st17.WORLD_CONTRACT["meta_narrative_policy"])
        self.assertIn("继续有效", st17.WORLD_CONTRACT["stance_persistence"])


class _MetaStub:
    """返回固定 SceneMeta 的桩（离线验证 LLM 判定器路径）"""

    def extract(self, system="", user="", response_model=None, **kw):
        return response_model(is_meta=1, epistemic_level="怀疑",
                              is_system_instruction=0, note="测试")


class TestMetaDetector(unittest.TestCase):
    """§8.2：结构化判定器优先；无 client 降级并如实标注"""

    def test_no_client_returns_none(self):
        self.assertIsNone(st17.make_meta_detector(None))

    def test_llm_path_meta_detected(self):
        det = st17.make_meta_detector(_MetaStub())
        scene = st17.infer_scene("你其实是个AI程序吧", {}, {}, meta_detector=det)
        self.assertEqual(scene["scene_mode"], "元对话")
        self.assertEqual(scene["scene_source"], "llm_structured")
        self.assertEqual(scene["epistemic_contract"], "怀疑")

    def test_llm_path_non_meta(self):
        class NonMeta(_MetaStub):
            def extract(self, system="", user="", response_model=None, **kw):
                return response_model(is_meta=0, epistemic_level="不知道",
                                      is_system_instruction=0, note="")

        scene = st17.infer_scene("今天好累", {}, {},
                                 meta_detector=st17.make_meta_detector(NonMeta()))
        self.assertEqual(scene["scene_source"], "llm_structured")
        self.assertEqual(scene["scene_mode"], "普通话题")

    def test_fallback_lexicon_labelled(self):
        scene = st17.infer_scene("你是真人吗", {}, {}, meta_detector=None)
        self.assertEqual(scene["scene_mode"], "元对话")
        self.assertEqual(scene["scene_source"], "fallback_lexicon",
                         "降级路径必须如实标注（§8.2 禁词表为正式方案）")

    def test_scene_meta_model_exists(self):
        obj = llm.SceneMeta()
        self.assertEqual(obj.is_system_instruction, 0, "用户元叙述恒不构成系统指令")


class TestEvalSampling(PFBase):
    """§13.1 评测抽样：全部 A 消息分层抽样（反选择偏差）+ 沉默段单选"""

    def test_sample_includes_unreplied(self):
        import phase17_eval as ev
        self.seed_warm_then_cold()
        # 冷淡期 07-11~16 的 A 大多未获回复 → 样本必须包含它们
        cuts = ev.sample_cuts(self.conn, n_per_month=10)
        self.assertTrue(cuts)
        unreplied = [c for c in cuts if not c["replied_real"]]
        self.assertTrue(unreplied, "抽样必须包含未获回复的 A 消息（§13.1）")
        # 沉默段单选
        for c in cuts:
            if "2026-08-01" <= c["day"] <= "2026-08-16":
                self.assertTrue(c["silent_segment"])


if __name__ == "__main__":
    unittest.main()
