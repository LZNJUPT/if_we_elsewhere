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


class TestLenientParsing(unittest.TestCase):
    """O-5d 字段宽容解析：别名表 / 时间戳单位 / CSV 通用支持。"""

    def setUp(self):
        self.imp = ChatlabJsonlImporter()

    def _parse_jsonl_lines(self, text: str) -> list[dict]:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text(text, encoding="utf-8")
            return self.imp.parse(p)

    def _parse_csv_rows(self, rows: list[dict]) -> list[dict]:
        import csv as _csv
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.csv"
            with open(p, "w", encoding="utf-8-sig", newline="") as f:
                w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            return self.imp.parse(p)

    # ---- 别名表（规范名 > 别名表序） ----
    def test_alias_ts(self):
        rows = self._parse_jsonl_lines(
            '{"ts": 1726010000, "text": "hi", "accountName": "a"}')
        self.assertEqual(rows[0]["timestamp"], 1726010000)
        self.assertEqual(rows[0]["content"], "hi")

    def test_alias_time(self):
        rows = self._parse_jsonl_lines('{"time": 1726010001, "content": "x"}')
        self.assertEqual(rows[0]["timestamp"], 1726010001)

    def test_alias_message_for_content(self):
        rows = self._parse_jsonl_lines('{"timestamp": 1726010002, "message": "m"}')
        self.assertEqual(rows[0]["content"], "m")

    def test_alias_sender_talker_nick(self):
        rows = self._parse_jsonl_lines(
            '{"timestamp": 1726010003, "content": "c", "nick": "小样"}')
        self.assertEqual(rows[0]["accountName"], "小样")
        rows = self._parse_jsonl_lines(
            '{"timestamp": 1726010003, "content": "c", "talker": "t1", "nick": "n1"}')
        self.assertEqual(rows[0]["accountName"], "t1")   # talker 优先于 nick

    def test_canonical_name_wins_over_alias(self):
        rows = self._parse_jsonl_lines(
            '{"timestamp": 1726010004, "ts": 999, "content": "c", "text": "old"}')
        self.assertEqual(rows[0]["timestamp"], 1726010004)
        self.assertEqual(rows[0]["content"], "c")

    def test_case_insensitive_header(self):
        rows = self._parse_csv_rows(
            [{"TimeStamp": "1726010005", "TEXT": "大小写", "Nick": "N"}])
        self.assertEqual(rows[0]["timestamp"], 1726010005)
        self.assertEqual(rows[0]["accountName"], "N")

    # ---- 时间戳单位 ----
    def test_unit_milliseconds(self):
        rows = self._parse_jsonl_lines('{"ts": 1726010006000, "text": "ms"}')
        self.assertEqual(rows[0]["timestamp"], 1726010006)

    def test_unit_seconds(self):
        rows = self._parse_jsonl_lines('{"ts": 1726010007, "text": "s"}')
        self.assertEqual(rows[0]["timestamp"], 1726010007)

    def test_unit_numeric_string(self):
        rows = self._parse_jsonl_lines('{"ts": "1726010008", "text": "str"}')
        self.assertEqual(rows[0]["timestamp"], 1726010008)

    def test_unit_iso_z(self):
        rows = self._parse_jsonl_lines('{"ts": "2024-09-11T01:30:08Z", "text": "z"}')
        self.assertEqual(rows[0]["timestamp"], 1726018208)

    def test_unit_iso_offset(self):
        rows = self._parse_jsonl_lines(
            '{"ts": "2024-09-11T04:30:08+03:00", "text": "off"}')
        self.assertEqual(rows[0]["timestamp"], 1726018208)

    def test_unit_iso_naive_is_tz8(self):
        rows = self._parse_jsonl_lines(
            '{"ts": "2024-09-11 09:30:08", "text": "naive"}')
        self.assertEqual(rows[0]["timestamp"], 1726018208)

    def test_bad_timestamp_skipped_and_counted(self):
        rows = self._parse_jsonl_lines(
            '{"ts": "not-a-time", "text": "bad"}\n{"ts": 1726010009, "text": "ok"}')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], 1726010009)
        self.assertEqual(self.imp.stats["skipped_no_time"], 1)

    # ---- CSV 通用支持 ----
    def test_csv_utf8_bom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bom.csv"
            p.write_bytes(
                b"\xef\xbb\xbf" + "timestamp,content,accountName\n".encode("utf-8") +
                "1726010010,BOM 内容,A甲\n".encode("utf-8"))
            self.assertTrue(self.imp.detect(p))
            rows = self.imp.parse(p)
            self.assertEqual(rows[0]["content"], "BOM 内容")
            self.assertEqual(self.imp.stats["encoding"], "utf-8-sig")

    def test_csv_gbk(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "gbk.csv"
            p.write_text("time,text,sender\n1726010011,中文GBK,B乙\n",
                         encoding="gbk")
            rows = self.imp.parse(p)
            self.assertEqual(rows[0]["content"], "中文GBK")
            self.assertEqual(rows[0]["accountName"], "B乙")
            self.assertEqual(self.imp.stats["encoding"], "gbk")

    def test_csv_end_to_end_ingest(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.csv"
            p.write_text("timestamp,content,accountName\n"
                         "1726010012,你好,A甲\n1726010132,你好呀,B乙\n",
                         encoding="utf-8")
            from phase1_ingest import ingest
            summary = ingest(p, {"A甲": "A", "B乙": "B"}, db_path=Path(td) / "t.db")
            self.assertEqual(summary["message_count"], 2)
            self.assertTrue(summary["gates_all_pass"])

    def test_registry_detect_lenient_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "lenient.jsonl"
            p.write_text('{"ts": 1726010013, "text": "无_type行"}\n', encoding="utf-8")
            imp = registry.auto_detect(p)
            self.assertIsInstance(imp, ChatlabJsonlImporter)
            self.assertEqual(imp.parse(p)[0]["content"], "无_type行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
