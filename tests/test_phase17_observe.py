# -*- coding: utf-8 -*-
"""Phase 17 决策观测（phase17_observe + DialEngine 埋点）单元测试 · 发布树版。

与私有树 tests/test_phase17_observe.py 同名同源；差异只有测试环境搭建
（本树走 config.py / PFBase 的临时 ROOT harness，引擎直接用 self.conn）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import phase17_observe as observe     # noqa: E402
import persona_fidelity as pf         # noqa: E402
import phase5_common as pc            # noqa: E402
from tests.test_persona_fidelity import PFBase, _load_dial  # noqa: E402


def _est_stub(p: float, layer: str = "test") -> dict:
    return {"p": p, "layer": layer, "state": {}, "samples": 0,
            "rate7": None, "rate14": None, "metric": "paired", "n_branch": 0}


class TestDecisionLog(PFBase):
    """产品路径埋点：沉默轮 / 回复轮 / 路径标记 / 快照"""

    def _make_engine(self, kind: str = "dial"):
        de = _load_dial()
        sim_id = de.new_line(self.conn, start_day="2026-07-17", name="OBS测试线",
                             seed=0)
        if kind != "dial":
            row = self.conn.execute("SELECT config_json FROM sim_runs WHERE sim_id=?",
                                    (sim_id,)).fetchone()
            cfg = json.loads(row[0] or "{}")
            cfg["kind"] = kind
            self.conn.execute("UPDATE sim_runs SET config_json=? WHERE sim_id=?",
                              (json.dumps(cfg, ensure_ascii=False), sim_id))
            self.conn.commit()
        eng = de.DialEngine(self.conn, sim_id, client=de.StubClient(),
                            retriever=None, verbose=False)
        return de, eng

    def test_silent_turn_decision_logged(self):
        de, eng = self._make_engine()
        orig = pf.estimate_reply_probability
        pf.estimate_reply_probability = lambda conn, **kw: _est_stub(0.0, "test-zero")
        try:
            res = eng.say("在吗")
            self.assertTrue(res["silent"])
        finally:
            pf.estimate_reply_probability = orig
        ds = observe.read_decisions(self.conn, eng.sim_id)
        self.assertEqual(len(ds), 1, "沉默轮也应落一行决策")
        d = ds[0]
        self.assertEqual(d["decided"], 0)
        self.assertEqual(d["path"], "product")
        self.assertAlmostEqual(d["p_reply"], 0.0)
        self.assertEqual(d["layer"], "test-zero")
        self.assertTrue(str(d["silent_reason"]).startswith("not_replied;"))
        self.assertEqual(d["reply_mode"], "none")
        self.assertFalse(d["prompt_digest"], "沉默轮无生成，不落装配摘要")
        self.assertIsNone(d["anchor_day"])
        self.assertIsNone(d["m"])
        json.loads(d["state_before"]); json.loads(d["state_after"])

    def test_reply_turn_decision_logged(self):
        de, eng = self._make_engine()
        orig = pf.estimate_reply_probability
        pf.estimate_reply_probability = lambda conn, **kw: _est_stub(1.0, "test-one")
        try:
            res = eng.say("在吗")
            self.assertFalse(res.get("silent", False))
        finally:
            pf.estimate_reply_probability = orig
        d = observe.last_decision(self.conn, eng.sim_id)
        self.assertEqual(d["decided"], 1)
        self.assertEqual(d["path"], "product")
        self.assertAlmostEqual(d["p_reply"], 1.0)
        self.assertEqual(d["layer"], "test-one")
        self.assertEqual(d["reply_mode"], res.get("medium"))
        self.assertTrue(d["prompt_digest"], "生成轮必须带装配摘要")
        self.assertIn("sha", d["prompt_digest"])

    def test_replay_path_marker(self):
        de, eng = self._make_engine(kind="replay")
        orig = pf.estimate_reply_probability
        pf.estimate_reply_probability = lambda conn, **kw: _est_stub(1.0)
        try:
            eng.say("在吗")
        finally:
            pf.estimate_reply_probability = orig
        d = observe.last_decision(self.conn, eng.sim_id)
        self.assertEqual(d["path"], "replay")

    def test_pf_unavailable_has_reason(self):
        """pf 不可用（kill switch）→ 闸门跳过必须留原因"""
        de, eng = self._make_engine()
        os.environ[pf.KILL_ENV] = "0"
        try:
            res = eng.say("在吗")
            self.assertFalse(res.get("silent", False))
        finally:
            os.environ.pop(pf.KILL_ENV, None)
        d = observe.last_decision(self.conn, eng.sim_id)
        self.assertTrue(str(d["silent_reason"]).startswith("gate_skipped;"),
                        d["silent_reason"])


class TestSelfFeedback(PFBase):
    """自反馈通道根除的观测验证（§5.1/§5.5，Phase B 锚定后）"""

    def test_branch_anchored_no_self_feedback(self):
        """锚定后（裁定三）：分支内往返不进 P → n_branch=0、sfb_flag=0，
        P 与纯真实锚定值一致；分支推进不改变 P"""
        self.seed_warm_then_cold()
        de = _load_dial()
        sim_id = de.new_line(self.conn, start_day="2026-07-17", name="SFB分支", seed=0)
        self.add_branch(sim_id, "2026-07-17", 0, "A", "昨天的事还想跟你说说")
        self.add_branch(sim_id, "2026-07-17", 1, "B", "嗯你说吧我听着")
        self.add_branch(sim_id, "2026-07-17", 2, "A", "就是关于我们之后怎么相处")
        self.commit()
        snap = observe.snapshot_estimate(self.conn, sim_id, "2026-07-17")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["n_branch"], 0, "分支样本不得进入 P（裁定三）")
        self.assertEqual(snap["sfb_flag"], 0, "锚定后自反馈通道应恒为 0")
        self.assertEqual(snap["est"]["anchor_day"], "2026-07-17")
        est_later = observe.snapshot_estimate(self.conn, sim_id, "2026-08-16")
        self.assertEqual(est_later["est"]["p"], snap["est"]["p"],
                         "m=1.0 时分支推进不得改变 P（§5.5 验收 1）")


if __name__ == "__main__":
    unittest.main()
