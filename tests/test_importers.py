# -*- coding: utf-8 -*-
"""导入器适配器单元测试（stdlib unittest，零新增依赖）。

样本全部为手工合成的纯虚构数据（tests/samples/），不含任何真实聊天记录。
覆盖：固定样本 → canonical 输出快照对比、detect 命中/排除、
跳过计数（群聊/未知类型/无时间戳）、端到端 ingest。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from importers import registry                              # noqa: E402
from importers.chatlab_jsonl import ChatlabJsonlImporter    # noqa: E402
from importers.wecomsg_csv import WecomsgCsvImporter        # noqa: E402

TZ8 = timezone(timedelta(hours=8))
SAMPLES = ROOT / "tests" / "samples"


def _msg(ts, t, content, account, pid=None):
    return {"_type": "message", "platformMessageId": pid, "timestamp": ts,
            "type": t, "content": content, "accountName": account}


class TestWecomsgAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.imp = WecomsgCsvImporter()
        cls.rows = cls.imp.parse(SAMPLES / "wecomsg_sample.csv")

    def test_detect(self):
        self.assertTrue(WecomsgCsvImporter().detect(SAMPLES / "wecomsg_sample.csv"))
        self.assertFalse(WecomsgCsvImporter().detect(ROOT / "sample_data" / "chat.sample.jsonl"))

    def test_snapshot(self):
        ts_str = int(datetime(2024, 9, 11, 9, 30, tzinfo=TZ8).timestamp())
        expected = [
            _msg(1726010000, 0, "今晚吃火锅吗", "wxid_demo_me", "7437267147299592501"),
            _msg(ts_str, 0, "吃！老地方？", "wxid_demo_peer", "7437267147299592502"),
            _msg(1726010120, 7, "[图片]", "wxid_demo_peer", "7437267147299592503"),
            _msg(1726010180, 7, "emoji_gif_001.gif", "wxid_demo_me", "7437267147299592504"),
            _msg(1726010240, 4, "[文件] 聚会的照片.zip", "wxid_demo_peer", "7437267147299592505"),
            _msg(1726010300, 23, "[通话]", "wxid_demo_me", "7437267147299592506"),
            _msg(1726010360, 25, "[引用 wxid_demo_me：今晚吃火锅吗] 吃！老地方？",
                 "wxid_demo_peer", "7437267147299592507"),
            _msg(1726010420, 27, "推荐的人名片", "wxid_demo_peer", "7437267147299592508"),
            _msg(1726010480, 99, "￥200.00", "wxid_demo_me", "7437267147299592509"),
            _msg(1726010540, 80, "你撤回了一条消息", "wxid_demo_peer", "7437267147299592510"),
            _msg(1726010900, 0, "这条MsgSvrID为空但仍可导入", "wxid_demo_me", "16"),
        ]
        self.assertEqual(self.rows, expected)

    def test_skip_counts(self):
        s = self.imp.stats
        self.assertEqual(s["total_rows"], 16)
        self.assertEqual(s["parsed"], 11)
        self.assertEqual(s["skipped_group"], 1)
        self.assertEqual(s["skipped_no_time"], 1)
        self.assertEqual(s["skipped_unknown_type"], 3)
        self.assertEqual(set(s["unknown_type_names"]), {"语音", "视频", "系统通知"})

    def test_legacy_content_column(self):
        """旧版导出：content 列为 JSON {"src":..,"msg":..}。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "legacy.csv"
            p.write_text(
                "id,type_name,is_sender,talker,room_name,content,CreateTime\n"
                '1,文本,1,me,peer,"{""src"": """", ""msg"": ""旧版列布局""}",1726010000\n',
                encoding="utf-8")
            imp = WecomsgCsvImporter()
            rows = imp.parse(p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], "旧版列布局")
            self.assertTrue(imp.detect(p))

    def test_end_to_end_ingest(self):
        from phase1_ingest import ingest
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            summary = ingest(SAMPLES / "wecomsg_sample.csv",
                             {"wxid_demo_me": "A", "wxid_demo_peer": "B"}, db_path=db)
            self.assertEqual(summary["message_count"], 11)
            self.assertTrue(summary["gates_all_pass"])


class TestChatlabAdapter(unittest.TestCase):
    def test_detect_and_parse_count(self):
        imp = ChatlabJsonlImporter()
        self.assertTrue(imp.detect(ROOT / "sample_data" / "chat.sample.jsonl"))
        rows = imp.parse(ROOT / "sample_data" / "chat.sample.jsonl")
        self.assertEqual(len(rows), 79)
        self.assertEqual(rows[0]["accountName"], "沈星然")

    def test_registry(self):
        self.assertIn("chatlab", registry.names())
        self.assertIn("wecomsg", registry.names())
        imp = registry.auto_detect(SAMPLES / "wecomsg_sample.csv")
        self.assertIsInstance(imp, WecomsgCsvImporter)
        imp2 = registry.auto_detect(ROOT / "sample_data" / "chat.sample.jsonl")
        self.assertIsInstance(imp2, ChatlabJsonlImporter)
        self.assertIsNone(registry.auto_detect(SAMPLES / "nonexistent_garbage.bin"))


class TestDoctor(unittest.TestCase):
    def test_wecomsg_report(self):
        from doctor import run_doctor
        r = run_doctor(SAMPLES / "wecomsg_sample.csv")
        self.assertTrue(r["recognized"])
        self.assertEqual(r["importer"], "wecomsg")
        self.assertEqual(r["message_count"], 11)
        self.assertEqual(r["total_rows"], 16)
        self.assertEqual(r["candidates"][0]["account"], "wxid_demo_peer")
        self.assertEqual(r["candidates"][1]["account"], "wxid_demo_me")
        self.assertTrue(r["importable"])
        self.assertEqual(r["suggested_args"]["format"], "wecomsg")
        self.assertGreaterEqual(r["privacy_estimate"]["messages_with_hits"], 0)
        self.assertIsNotNone(r["time_span"])

    def test_chatlab_report(self):
        from doctor import run_doctor
        r = run_doctor(ROOT / "sample_data" / "chat.sample.jsonl")
        self.assertEqual(r["importer"], "chatlab")
        self.assertTrue(r["importable"])
        self.assertEqual(len(r["candidates"]), 2)
        # 建议参数默认 A=消息较多一方；样本中林晚语消息多于沈星然
        self.assertEqual(r["suggested_args"]["sender_a"], "林晚语")
        self.assertEqual(r["suggested_args"]["sender_b"], "沈星然")

    def test_garbage_and_missing(self):
        from doctor import run_doctor
        with tempfile.TemporaryDirectory() as td:
            junk = Path(td) / "junk.bin"
            junk.write_bytes(b"\x00\x01\x02 not a chat export \xff\xfe")
            r = run_doctor(junk)
            self.assertFalse(r["recognized"])
            self.assertFalse(r["importable"])
            self.assertTrue(r["reasons"])
            r2 = run_doctor(Path(td) / "no_such_file.bin")
            self.assertFalse(r2["readable"])
            self.assertFalse(r2["importable"])


class TestTelegramAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from importers.telegram_json import TelegramJsonImporter
        cls.imp_cls = TelegramJsonImporter
        cls.imp = TelegramJsonImporter()
        cls.rows = cls.imp.parse(SAMPLES / "telegram_sample.json")

    def test_detect(self):
        self.assertTrue(self.imp_cls().detect(SAMPLES / "telegram_sample.json"))
        self.assertFalse(self.imp_cls().detect(SAMPLES / "wecomsg_sample.csv"))
        self.assertFalse(self.imp_cls().detect(ROOT / "sample_data" / "chat.sample.jsonl"))

    def test_snapshot(self):
        from datetime import datetime, timezone as _tz, timedelta as _td
        ts_off = int(datetime(2024, 9, 11, 2, 37, tzinfo=_tz(_td(hours=3))).timestamp())
        expect = [
            _msg(1726018200, 0, "TG 合成样本第一句", "user111", "1"),
            _msg(1726018260, 0, "看这个链接 https://example.com/a ，还有 纯文本段结尾", "user111", "2"),
            _msg(1726018320, 0, "我发出的回复", "user100", "3"),
            _msg(1726018440, 7, "[图片]", "user111", "5-m"),
            _msg(1726018440, 0, "这是图注", "user111", "5-t"),
            _msg(1726018500, 7, "sticker_1.webp", "user100", "6-m"),
            _msg(1726018620, 4, "[文件] 说明书.pdf", "user111", "8-m"),
            _msg(1726018620, 0, "文档说明", "user111", "8-t"),
            _msg(ts_off, 4, "[文件] video_1.mp4", "user100", "9-m"),
        ]
        self.assertEqual(self.rows, expect)

    def test_skip_counts(self):
        s = self.imp.stats
        self.assertEqual(s["total_rows"], 11)
        self.assertEqual(s["parsed"], 9)
        self.assertEqual(s["skipped_service"], 1)
        self.assertEqual(s["skipped_edited"], 1)
        self.assertEqual(s["skipped_unknown_type"], 1)   # voice_message
        self.assertEqual(s["skipped_group"], 1)          # 第三发送者 user999
        self.assertEqual(s["unknown_senders"], ["user999"])

    def test_end_to_end_ingest(self):
        from phase1_ingest import ingest
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            summary = ingest(SAMPLES / "telegram_sample.json",
                             {"user100": "A", "user111": "B"}, db_path=db)
            self.assertEqual(summary["message_count"], 9)
            self.assertTrue(summary["gates_all_pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
