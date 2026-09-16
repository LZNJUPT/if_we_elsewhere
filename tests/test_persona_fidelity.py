# -*- coding: utf-8 -*-
"""
人格保真（persona_fidelity）单元测试。

方法论（对应交接文档 §五/§六 的教训）：
- **不依赖真实数据**：全部用合成 fixture（已知回复率结构），验证的是「机制」而非
  私有仓真实数据才能复现的百分比（≥80% 沉默这类基线在发布树上不可复算）；
- **不用「有回复的配对」造样本**：fixture 直接给全量消息时间线，无选择偏差；
- 沉默判定、落库标记、事件跳过、回退开关、F2 门禁与截断、F3/F4 patch 全覆盖。

    python -m unittest tests.test_persona_fidelity -v
    （或 python -m unittest discover -s tests）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config as cfg_mod          # noqa: E402
import phase5_common as pc        # noqa: E402
import persona_fidelity as pf     # noqa: E402

_DIAL = None                      # 延迟导入（触发 F3/F4 patch），见 _load_dial()


def _load_dial():
    global _DIAL
    if _DIAL is None:
        import phase15_dial_engine as de
        _DIAL = de
    return _DIAL


class PFBase(unittest.TestCase):
    """项目根搬到临时目录；每个用例一份干净的库（主库零侵入的老规矩）"""

    def setUp(self) -> None:
        self._real_root = cfg_mod.ROOT
        self.tmp = Path(tempfile.mkdtemp(prefix="ifwe_pf_test_"))
        cfg_mod.ROOT = self.tmp
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        cfg_mod.set_active_profile("")
        pc.refresh_paths()
        self._seed_min_persona()
        self.conn = pc.connect()
        pc.apply_all_schemas(self.conn)
        pf.register_exempt_len(None)
        self._env_backup = os.environ.get(pf.KILL_ENV)
        os.environ.pop(pf.KILL_ENV, None)

    def _seed_min_persona(self) -> None:
        """最小 persona JSON（build_world 必需；L/M/S/U 四层允许为空）"""
        pdir = cfg_mod.persona_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        import json as _json
        body = {"display_name": "", "layers": {k: {"items": []} for k in "LMSU"}}
        for person in ("A", "B"):
            (pdir / f"persona_v1_{person}.json").write_text(
                _json.dumps(body, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        if self._env_backup is None:
            os.environ.pop(pf.KILL_ENV, None)
        else:
            os.environ[pf.KILL_ENV] = self._env_backup
        pf.register_exempt_len(None)
        try:
            self.conn.close()
        except Exception:
            pass
        cfg_mod.ROOT = self._real_root
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures -----------------------------------------------------
    _seq = 0

    def add_real(self, day: str, sender: str, content: str = "",
                 att: str = "") -> None:
        """往 messages 表插一条真实记录（NOT NULL 列齐全；content_clean 为脱敏字段）"""
        PFBase._seq += 1
        self.conn.execute(
            "INSERT INTO messages (message_id, ts, ts_local, day, sender_key, type, "
            "subtype, content_orig, content_clean, attachment_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"M{PFBase._seq:08d}", 1750000000 + PFBase._seq,
             f"{day} 12:00:{PFBase._seq % 60:02d}", day, sender, 1, "text",
             content, content, att))

    def add_branch(self, sim_id: str, day: str, turn: int, sender: str,
                   content: str, meta: str = "{}") -> None:
        self.conn.execute(
            "INSERT INTO sim_messages (msg_id, sim_id, session_id, day, turn_idx, sender, "
            "content, emotion_s, meta, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{sim_id}-T{turn:03d}", sim_id, f"{sim_id}-S001", day, turn, sender,
             content, "", meta, pc.now_str()))

    def commit(self) -> None:
        self.conn.commit()

    def seed_warm_then_cold(self) -> None:
        """2026-07-01~10 亲密期（A/B 有来有回，B 当天必回）；07-11~16 降温期（B 零回复）。
        每天固定 3 轮，保证条件化意愿层的样本量 ≥ min_samples。"""
        for d in range(1, 11):
            day = f"2026-07-{d:02d}"
            t = 0
            for _ in range(3):
                t += 1
                self.add_real(day, "A", f"亲密期消息{t}号今天怎么样呀")
                t += 1
                self.add_real(day, "B", f"还好呀你呢我也是刚忙完{t}")
        for d in range(11, 17):
            day = f"2026-07-{d:02d}"
            for t in range(1, 4):
                self.add_real(day, "A", f"降温期的单方面消息第{t}条")
        self.commit()


class TestWillingness(PFBase):
    """F1 意愿函数：分层数值行为"""

    def test_empty_db_falls_back(self):
        est = pf.estimate_reply_probability(self.conn, start_day="2026-07-17",
                                            current_day="2026-07-16")
        self.assertEqual(est["layer"], "fallback")
        self.assertAlmostEqual(est["p"], 0.5, places=4)

    def test_cold_period_low(self):
        self.seed_warm_then_cold()
        est = pf.estimate_reply_probability(self.conn, start_day="2026-07-17",
                                            current_day="2026-07-16")
        self.assertLessEqual(est["p"], 0.2, est)
        self.assertEqual(est["state"]["partner_recent"], 0)   # 降温期对方久未说话

    def test_partner_activity_raises_p(self):
        """已知残留的修复：同一降温期，对方刚说过话（表态后收尾期）→ 意愿显著回升"""
        self.seed_warm_then_cold()
        cold = pf.estimate_reply_probability(self.conn, start_day="2026-07-17",
                                             current_day="2026-07-16")
        # 对方 07-16 上午发了一条表态，用户随后回复 → partner_recent=1
        self.add_real("2026-07-16", "B", "我们都需要冷静一段时间，先别联系了")
        self.add_real("2026-07-16", "A", "好，我知道了，那我等你")
        self.commit()
        hot = pf.estimate_reply_probability(self.conn, start_day="2026-07-17",
                                            current_day="2026-07-16")
        self.assertEqual(hot["state"]["partner_recent"], 1, hot)
        self.assertGreaterEqual(hot["p"], 0.5, hot)
        self.assertGreater(hot["p"] - cold["p"], 0.3, {"cold": cold, "hot": hot})

    def test_warm_period_high(self):
        self.seed_warm_then_cold()
        # 当前消息落在亲密期内（07-06），此前 5 天全是高回复率
        self.add_real("2026-07-06", "A", "今晚一起吃饭吗")
        self.commit()
        est = pf.estimate_reply_probability(self.conn, start_day="2026-07-07",
                                            current_day="2026-07-06")
        self.assertGreaterEqual(est["p"], 0.6, est)

    def test_branch_silent_marker_is_not_a_reply(self):
        """分支内的沉默标记行（B 侧空内容）不得被当成「对方回复了」"""
        sid = "SIM-TEST01"
        self.add_branch(sid, "2026-07-20", 1, "A", "在吗")
        self.add_branch(sid, "2026-07-20", 2, "B", "",
                        meta='{"silent": true, "willingness": 0.1}')
        self.add_branch(sid, "2026-07-20", 3, "A", "还在吗")
        self.commit()
        est = pf.estimate_reply_probability(self.conn, sim_id=sid,
                                            start_day="2026-07-20",
                                            current_day="2026-07-20")
        self.assertEqual(est["layer"], "global")
        self.assertEqual(est["p"], 0.0, est)      # 唯一样本（第 1 条 A）未被回应

    def test_branch_reply_counts(self):
        sid = "SIM-TEST02"
        self.add_branch(sid, "2026-07-20", 1, "A", "在吗")
        self.add_branch(sid, "2026-07-20", 2, "B", "在的怎么了")
        self.add_branch(sid, "2026-07-20", 3, "A", "没事就想问问")
        self.commit()
        est = pf.estimate_reply_probability(self.conn, sim_id=sid,
                                            start_day="2026-07-20",
                                            current_day="2026-07-20")
        self.assertGreaterEqual(est["p"], 0.9, est)


class TestEngineSilentTurn(PFBase):
    """F1/F5 引擎接入：沉默轮不调 LLM、落库标记、跳过事件判定"""

    def _make_engine(self):
        de = _load_dial()
        conn2 = pc.connect()

        class CountingStub(de.StubClient):
            calls = 0

            def extract(self, *a, **kw):
                CountingStub.calls += 1
                return super().extract(*a, **kw)

        sim_id = de.new_line(conn2, start_day="2026-07-17", name="PF测试线",
                             seed=0)
        eng = de.DialEngine(conn2, sim_id, client=CountingStub(), retriever=None,
                            verbose=False)
        return de, eng, CountingStub

    def setUp(self):
        super().setUp()
        # 引擎测试用独立连接（engine 自持 conn；tearDown 关主连接即可）

    def test_silent_turn_skips_llm_and_events(self):
        de, eng, Stub = self._make_engine()
        orig = pf.estimate_reply_probability
        pf.estimate_reply_probability = lambda conn, **kw: {
            "p": 0.0, "layer": "test", "state": {}, "samples": 0,
            "rate7": None, "rate14": None}
        try:
            res = eng.say("在吗")
            self.assertTrue(res["silent"])
            self.assertEqual(res["willingness"], 0.0)
            self.assertEqual(Stub.calls, 0, "沉默轮不得调用 LLM")
            row = eng.conn.execute(
                "SELECT sender, content, meta FROM sim_messages WHERE sim_id=? "
                "ORDER BY rowid DESC LIMIT 1", (eng.sim_id,)).fetchone()
            self.assertEqual(row[0], "B")
            self.assertEqual(row[1], "")
            self.assertIn('"silent":true', row[2].lower().replace(" ", ""))
            n_ev = eng.conn.execute("SELECT COUNT(*) FROM sim_events WHERE sim_id=?",
                                    (eng.sim_id,)).fetchone()[0]
            self.assertEqual(n_ev, 0, "沉默轮不得产生事件（否则兜底成日常陪伴加热关系）")
            n_rel = eng.conn.execute("SELECT COUNT(*) FROM sim_rel_state WHERE sim_id=?",
                                     (eng.sim_id,)).fetchone()[0]
            self.assertEqual(n_rel, 0, "沉默轮不得更新关系状态")
        finally:
            pf.estimate_reply_probability = orig
            eng.conn.close()

    def test_reply_turn_when_p_is_one(self):
        de, eng, Stub = self._make_engine()
        orig = pf.estimate_reply_probability
        pf.estimate_reply_probability = lambda conn, **kw: {
            "p": 1.0, "layer": "test", "state": {}, "samples": 0,
            "rate7": None, "rate14": None}
        try:
            res = eng.say("在吗")
            self.assertFalse(res["silent"])
            self.assertEqual(Stub.calls, 1)
            self.assertTrue(res["reply"])
            row = eng.conn.execute(
                "SELECT content, meta FROM sim_messages WHERE sim_id=? "
                "ORDER BY rowid DESC LIMIT 1", (eng.sim_id,)).fetchone()
            self.assertEqual(row[0], res["reply"])
            self.assertNotIn("silent", row[1])
        finally:
            pf.estimate_reply_probability = orig
            eng.conn.close()


class TestF2Window(PFBase):

    def _seed(self):
        for d in range(1, 6):
            self.add_real(f"2026-07-0{d}", "A", f"白天说的话{d}")
            self.add_real(f"2026-07-0{d}", "B", f"对方的回复{d}")
        self.commit()

    def test_content_and_limit(self):
        self._seed()
        txt = pf.real_window_text(self.conn, start_day="2026-07-06", limit=4)
        self.assertTrue(txt.startswith("【最近的真实对话"))
        self.assertIn("对方的回复5", txt)
        self.assertIn("白天说的话5", txt)
        self.assertNotIn("白天说的话1", txt)      # 只取最近 4 条

    def test_divergence_truncation(self):
        self._seed()
        txt = pf.real_window_text(self.conn, start_day="2026-07-06",
                                  divergence_point="2026-07-03",
                                  has_rewrite=True, limit=50)
        self.assertIn("对方的回复2", txt)
        self.assertNotIn("2026-07-03", txt)
        self.assertNotIn("对方的回复3", txt)      # 分歧点之后不泄露

    def test_no_rewrite_uses_start_day(self):
        self._seed()
        txt = pf.real_window_text(self.conn, start_day="2026-07-03",
                                  divergence_point="2026-07-03",
                                  has_rewrite=False, limit=50)
        self.assertIn("对方的回复2", txt)          # 续聊线：分歧点前的真实数据可用

    def test_privacy_gate_disables_window(self):
        self._seed()
        (self.tmp / "config.yaml").write_text(
            "privacy:\n  allow_llm_send: false\n", encoding="utf-8")
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        try:
            self.assertFalse(pf.f2_enabled())
            self.assertEqual(pf.real_window_text(self.conn, start_day="2026-07-06"), "")
        finally:
            cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})


class TestRulesAndExempt(PFBase):

    def test_rules_patched_after_import(self):
        de = _load_dial()
        self.assertTrue(de.loop._pf_patched)
        self.assertIn("重申", de.loop.REPLY_RULES)               # F3：允许坚定重申立场
        self.assertNotIn("不要连续几轮表达同一个意思", de.DIAL_RULES)  # R5 反向诱导已移除
        self.assertIn("按情境自然取用", de.DIAL_RULES)

    def test_kill_switch_restores_old_behavior(self):
        """总开关置 off：f1/f2 全关、规则回原版、豁免失效（行为=改动前）。

        ⚠ REPLY_RULES 的替换发生在首次导入时（act() 直接读模块全局），因此本测试
        必须在干净环境下完成导入之后运行（同类的 test_dedup_* 已先行触发导入）。
        """
        de = _load_dial()                              # 确保已在干净环境下导入
        os.environ[pf.KILL_ENV] = "0"
        try:
            self.assertFalse(pf.f1_enabled())
            self.assertFalse(pf.f2_enabled())
            self.assertEqual(pf.dial_rules("ORIGINAL"), "ORIGINAL")
            # 即便豁免阈值已注册，开关关闭也不豁免（wrapper 动态读开关）
            pf.register_exempt_len(6)
            agent = de.loop.PersonaAgent.__new__(de.loop.PersonaAgent)
            agent.recent_texts = ["一样的短句"]
            self.assertGreater(agent._recent_similarity("一样的短句"), 0.99)
        finally:
            os.environ.pop(pf.KILL_ENV, None)
            pf.register_exempt_len(None)

    def test_dedup_exempt_short_reply(self):
        de = _load_dial()
        pf.register_exempt_len(6)     # 对方真实长度中位数 = 6 字
        try:
            agent = de.loop.PersonaAgent.__new__(de.loop.PersonaAgent)
            agent.recent_texts = ["我们先彼此冷静一段时间吧，都安静几天好不好"]
            self.assertEqual(agent._recent_similarity("不想说了"), 0.0,
                             "短回复（≤中位数）应豁免去重护栏")
            self.assertAlmostEqual(
                agent._recent_similarity("我们先彼此冷静一段时间吧，都安静几天好不好"),
                1.0, places=2)     # 长复读仍被护栏捕捉
        finally:
            pf.register_exempt_len(None)

    def test_median_len_from_db(self):
        self.add_real("2026-07-01", "B", "四字")
        self.add_real("2026-07-01", "B", "八九个字左右")
        self.add_real("2026-07-01", "A", "我这边的话随便多少字都行")
        self.commit()
        self.assertEqual(pf.partner_median_len(self.conn), 6)   # B 侧中位数


if __name__ == "__main__":
    unittest.main()
