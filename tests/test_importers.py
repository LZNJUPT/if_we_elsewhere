# -*- coding: utf-8 -*-
"""导入器适配器单元测试（stdlib unittest，零新增依赖）。

样本全部为手工合成的纯虚构数据（tests/samples/），不含任何真实聊天记录。
覆盖：固定样本 → canonical 输出快照对比、detect 命中/排除、
跳过计数（群聊/未知类型/无时间戳）、端到端 ingest。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import config as cfg_mod                                    # noqa: E402
from importers import registry                              # noqa: E402
from importers.chatlab_jsonl import ChatlabJsonlImporter    # noqa: E402
from importers.wecomsg_csv import WecomsgCsvImporter        # noqa: E402

TZ8 = timezone(timedelta(hours=8))
SAMPLES = ROOT / "tests" / "samples"

try:                                    # 接口契约测试需要 fastapi + httpx（打包 venv / CI 里有）
    import fastapi                                          # noqa: F401
    import httpx                                            # noqa: F401
    HAS_WEB = True
except Exception:
    HAS_WEB = False


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


class TestPlaintextImporter(unittest.TestCase):
    """v0.3 纯文本行流适配器：不猜时间、不猜归类、续行合并、编码探测。"""

    def setUp(self):
        from importers.plaintext_lines import PlaintextLinesImporter
        self.imp_cls = PlaintextLinesImporter
        self.imp = PlaintextLinesImporter()
        self.tmp = Path(tempfile.mkdtemp(prefix="ifwe_txt_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text: str, name="a.txt", enc="utf-8") -> Path:
        p = self.tmp / name
        p.write_bytes(text.encode(enc))
        return p

    def test_detect_needs_anchored_timestamps(self):
        good = ("2024-09-11 09:30:00 张三: 在吗\n"
                "2024-09-11 09:31:00 李四: 在\n"
                "2024-09-11 09:32:00 张三: 好\n")
        self.assertTrue(self.imp_cls().detect(self._write(good, "good.txt")))
        prose = "这是一篇普通散文。\n今天天气不错。\n我们去了公园，然后回家。\n"
        self.assertFalse(self.imp_cls().detect(self._write(prose, "prose.txt")))
        # 只有时间戳、没有发送者标记 → 不认（避免把日志误当聊天）
        loggy = "".join(f"2024-09-11 09:3{i}:00 服务启动完成\n" for i in range(5))
        self.assertFalse(self.imp_cls().detect(self._write(loggy, "svc.log")))
        # 不相关扩展名一律不认
        self.assertFalse(self.imp_cls().detect(self._write(good, "good.json")))

    def test_gbk_encoding(self):
        p = self._write("2024-09-11 09:30:00 张三: 中文GBK内容\n"
                        "2024-09-11 09:31:00 李四: 第二句\n"
                        "2024-09-11 09:32:00 张三: 第三句\n", "gbk.txt", enc="gbk")
        rows = self.imp.parse(p)
        self.assertEqual(rows[0]["content"], "中文GBK内容")
        self.assertEqual(self.imp.stats["encoding"], "gb18030")

    def test_continuation_merged(self):
        p = self._write("2024-09-11 09:30:00 张三: 第一行\n"
                        "这行没有时间戳，是续行\n"
                        "2024-09-11 09:31:00 李四: 收到\n")
        rows = self.imp.parse(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["content"], "第一行\n这行没有时间戳，是续行")
        self.assertEqual(self.imp.stats["appended_lines"], 1)
        self.assertEqual(self.imp.stats["skipped_no_ts"], 0)

    def test_leading_noise_skipped(self):
        p = self._write("== 聊天记录导出 ==\n"
                        "2024-09-11 09:30:00 张三: 甲\n"
                        "2024-09-11 09:31:00 李四: 乙\n"
                        "2024-09-11 09:32:00 张三: 丙\n")
        rows = self.imp.parse(p)
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.imp.stats["skipped_no_ts"], 1)

    def test_media_marks_and_skip(self):
        p = self._write("2024-09-11 09:30:00 张三: [图片]\n"
                        "2024-09-11 09:31:00 李四: [文件] 资料.zip\n"
                        "2024-09-11 09:32:00 张三: [语音]\n"
                        "2024-09-11 09:33:00 李四: 正常一句\n")
        rows = self.imp.parse(p)
        types = [r["type"] for r in rows]
        self.assertEqual(types, [7, 4, 0])
        self.assertEqual(self.imp.stats["skipped_unknown_type"], 1)
        self.assertEqual(rows[1]["content"], "[文件] 资料.zip")

    def test_sender_before_timestamp(self):
        p = self._write("张三 2024-09-11 09:30:00 发送者在前的列序\n"
                        "李四 2024-09-11 09:31:00 第二条\n"
                        "张三 2024-09-11 09:32:00 第三条\n")
        rows = self.imp.parse(p)
        self.assertEqual(rows[0]["accountName"], "张三")
        self.assertEqual(rows[0]["content"], "发送者在前的列序")

    def test_tab_separated_table(self):
        p = self._write("2024-09-11 09:30:00\t李四\t表格单元格\n"
                        "2024-09-11 09:31:00\t张三\t第二条\n"
                        "2024-09-11 09:32:00\t李四\t第三条\n")
        rows = self.imp.parse(p)
        self.assertEqual(rows[0]["accountName"], "李四")
        self.assertEqual(rows[0]["content"], "表格单元格")

    def test_bracketed_sender(self):
        p = self._write("2024-09-11 09:30:00【张三】中括号标记\n"
                        "2024-09-11 09:31:00【李四】第二条\n"
                        "2024-09-11 09:32:00【张三】第三条\n")
        rows = self.imp.parse(p)
        self.assertEqual(rows[0]["accountName"], "张三")
        self.assertEqual(rows[0]["content"], "中括号标记")

    def test_relative_time_not_guessed(self):
        p = self._write("昨天 上午十点 张三: 相对时间不猜\n"
                        "刚才 李四: 也不猜\n")
        self.assertEqual(self.imp.parse(p), [])
        self.assertFalse(self.imp_cls().detect(p))

    def test_end_to_end_ingest(self):
        from phase1_ingest import ingest
        p = self._write("2024-09-11 09:30:00 张三: 你好\n"
                        "2024-09-11 09:31:00 李四: 你好呀\n"
                        "2024-09-11 09:32:00 张三: 在忙吗\n"
                        "2024-09-11 09:33:00 李四: 不忙\n")
        with tempfile.TemporaryDirectory() as td:
            s = ingest(p, {"张三": "A", "李四": "B"}, db_path=Path(td) / "t.db")
            self.assertEqual(s["message_count"], 4)
            self.assertTrue(s["gates_all_pass"])


def _make_docx(path: Path, para_specs: list, with_image: bool = False) -> Path:
    """手写一个最小 .docx（zip + word/document.xml），用于测试零依赖解析。"""
    import zipfile

    def para(text: str, drawing: bool = False) -> str:
        d = "<w:r><w:drawing/></w:r>" if drawing else ""
        return f"<w:p>{d}<w:r><w:t>{text}</w:t></w:r></w:p>"

    body = "".join(para(*s) if isinstance(s, tuple) else para(s) for s in para_specs)
    rows = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
        + "</w:tr>" for row in getattr(_make_docx, "_table", []))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}{'<w:tbl>' + rows + '</w:tbl>' if rows else ''}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
                   '/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)
    return path


class TestDocxImporter(unittest.TestCase):
    """v0.3 Word 适配器：纯标准库 zipfile + ElementTree，段落与表格都收。"""

    def setUp(self):
        from importers.docx_text import DocxTextImporter
        self.imp_cls = DocxTextImporter
        self.imp = DocxTextImporter()
        self.tmp = Path(tempfile.mkdtemp(prefix="ifwe_docx_"))
        _make_docx._table = [["2024-09-11 09:31:00", "李四", "表格里的一条"],
                             ["2024-09-11 09:32:00", "张三", "表格里的第二条"]]

    def tearDown(self):
        import shutil
        _make_docx._table = []
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detect(self):
        p = _make_docx(self.tmp / "a.docx", ["2024-09-11 09:30:00 张三: 段落"])
        self.assertTrue(self.imp_cls().detect(p))
        not_zip = self.tmp / "b.docx"
        not_zip.write_bytes(b"not a zip at all")
        self.assertFalse(self.imp_cls().detect(not_zip))
        plain_zip = self.tmp / "c.docx"
        import zipfile
        with zipfile.ZipFile(plain_zip, "w") as z:
            z.writestr("hello.txt", "x")
        self.assertFalse(self.imp_cls().detect(plain_zip))
        # 旧版二进制 .doc 不支持
        old = self.tmp / "d.doc"
        old.write_bytes(b"\xd0\xcf\x11\xe0")
        self.assertFalse(self.imp_cls().detect(old))

    def test_parse_paragraphs_and_table(self):
        p = _make_docx(self.tmp / "a.docx",
                       [("2024-09-11 09:30:00 张三: 表格外的段落", False)],
                       with_image=True)
        rows = self.imp.parse(p)
        self.assertEqual([r["content"] for r in rows],
                         ["表格外的段落", "表格里的一条", "表格里的第二条"])
        self.assertEqual([r["accountName"] for r in rows], ["张三", "李四", "张三"])
        self.assertEqual(self.imp.stats["tables"], 1)

    def test_images_counted_not_imported(self):
        p = _make_docx(self.tmp / "b.docx",
                       [("2024-09-11 09:30:00 张三: 一行", True),
                        ("2024-09-11 09:31:00 李四: 两行", False)])
        self.imp.parse(p)
        self.assertEqual(self.imp.stats["images"], 1)

    def test_end_to_end_ingest(self):
        from phase1_ingest import ingest
        _make_docx._table = []                       # 本用例只走段落路径（表格路径见上）
        p = _make_docx(self.tmp / "c.docx", [
            ("2024-09-11 09:30:00 张三: 甲", False),
            ("2024-09-11 09:31:00 李四: 乙", False)])
        with tempfile.TemporaryDirectory() as td:
            s = ingest(p, {"张三": "A", "李四": "B"}, db_path=Path(td) / "t.db")
            self.assertEqual(s["message_count"], 2)
            self.assertTrue(s["gates_all_pass"])


class TestPdfGuidance(unittest.TestCase):
    """PDF 只识别不解析：必须给出可执行的转存引导，且不抛异常。"""

    def test_pdf_gets_guidance(self):
        from doctor import run_doctor
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chat.pdf"
            p.write_bytes(b"%PDF-1.4\nfake")
            r = run_doctor(p)
            self.assertFalse(r["recognized"])
            self.assertFalse(r["importable"])
            self.assertIn("PDF", r["verdict"])
            self.assertTrue(any("txt" in a or "docx" in a for a in r["advice"]))
            self.assertTrue(r["reasons"])


class TestSpecV2Schema(unittest.TestCase):
    """旧库（v1 结构）必须能被幂等升级到 v2。"""

    def test_ensure_v2_columns_idempotent(self):
        import sqlite3
        import phase1_ingest as p1
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "old.db"
            conn = sqlite3.connect(db)
            conn.executescript((ROOT / "app" / "schema_v1.sql").read_text(encoding="utf-8"))
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
            self.assertNotIn("source_id", cols)          # v1 里确实没有
            p1.ensure_v2_schema(conn)
            p1.ensure_v2_schema(conn)                    # 幂等：再跑一次不报错
            conn.commit()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
            self.assertIn("source_id", cols)
            self.assertIn("dedup_key", cols)
            idx = {r[1] for r in conn.execute("PRAGMA index_list(messages)").fetchall()}
            self.assertIn("idx_messages_source", idx)
            self.assertIn("idx_messages_dedup", idx)
            conn.close()

    def test_ensure_v2_noop_without_table(self):
        import sqlite3
        import phase1_ingest as p1
        conn = sqlite3.connect(":memory:")
        p1.ensure_v2_schema(conn)                        # 表还不存在 → 静默返回
        conn.close()

    def test_apply_all_schemas_builds_v2_columns_on_fresh_db(self):
        """回归：全新库（= 界面「新建好友」后那条路径）也必须带上 v2 两列。

        曾经的 bug：`apply_all_schemas()` 在建表**之前**调 `ensure_v2_schema()`，
        那一刻 messages 还不存在 → 守卫直接返回 → 随后 schema_v1 建出的 v1 版
        messages 永远缺 source_id/dedup_key（只有走导入的 build_db 才正确）。
        """
        import sqlite3
        import phase5_common as pc
        conn = sqlite3.connect(":memory:")
        pc.apply_all_schemas(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        self.assertIn("source_id", cols)
        self.assertIn("dedup_key", cols)
        idx = {r[1] for r in conn.execute("PRAGMA index_list(messages)").fetchall()}
        self.assertIn("idx_messages_source", idx)
        self.assertIn("idx_messages_dedup", idx)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in ("import_sources", "media", "message_media"):
            self.assertIn(t, tables)
        pc.apply_all_schemas(conn)                       # 幂等：再跑一次不报错
        conn.close()


class TestMessageIdNoCollision(unittest.TestCase):
    """回归：无平台消息 id 且同秒的多条消息，v1 会撞主键被静默覆盖丢数据。"""

    def test_same_second_messages_all_kept(self):
        import sqlite3
        from phase1_ingest import ingest
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "same_second.jsonl"
            lines = []
            for i, who in enumerate(["甲", "甲", "乙", "乙"]):
                lines.append(json.dumps({"_type": "message", "timestamp": 1726010000,
                                         "type": 0, "content": f"同一秒的第{i}句",
                                         "accountName": who}, ensure_ascii=False))
            src.write_text("\n".join(lines) + "\n", encoding="utf-8")
            db = td / "t.db"
            s = ingest(src, {"甲": "A", "乙": "B"}, db_path=db)
            self.assertEqual(s["message_count"], 4)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4)
            self.assertEqual(
                conn.execute("SELECT COUNT(DISTINCT message_id) FROM messages").fetchone()[0], 4)
            conn.close()


class TestMultiSourceMerge(unittest.TestCase):
    """v2 多源合并：逐源映射、跨源去重、按时间合并、G8 门禁、来源台账。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ifwe_merge_"))
        T0 = 1726010000
        rows = ["id,MsgSvrID,type_name,is_sender,talker,room_name,msg,src,CreateTime"]
        for i in range(1, 11):
            me = i % 2
            rows.append(f"{i},{7000 + i},文本,{me},"
                        f"{'wxid_me' if me else 'wxid_ta'},,第{i}句,,{T0 + i * 60}")
        cls.a = cls.tmp / "wechat.csv"
        cls.a.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")

        def iso(i):
            return datetime.fromtimestamp(T0 + i * 60, TZ8).strftime("%Y-%m-%dT%H:%M:%S")
        msgs = [{"id": 9000 + i, "type": "message", "date": iso(i), "from": "x",
                 "from_id": "user_me" if i % 2 else "user_ta", "text": f"第{i}句"}
                for i in range(5, 16)]
        cls.b = cls.tmp / "tg.json"
        cls.b.write_text(json.dumps({"type": "personal_chat", "messages": msgs},
                                    ensure_ascii=False), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _specs(self, swap=False):
        ma = {"wxid_ta": "A", "wxid_me": "B"} if swap else {"wxid_me": "A", "wxid_ta": "B"}
        mb = {"user_me": "A", "user_ta": "B"}
        return [{"path": self.a, "sender_map": ma, "name": "wechat.csv"},
                {"path": self.b, "sender_map": mb, "name": "tg.json"}]

    def test_merge_counts_and_dedup(self):
        from phase1_ingest import plan_merge
        m = plan_merge(self._specs())
        self.assertEqual(m["parsed_total"], 21)
        self.assertEqual(m["duplicates_removed"], 6)      # 第 5..10 句重叠
        self.assertEqual(m["message_count"], 15)
        self.assertEqual(m["unknown_sender"], 0)
        self.assertEqual(m["map_conflicts"], 0)
        self.assertEqual(m["source_count"], 2)
        self.assertIsNotNone(m["overlap"])

    def test_swapped_mapping_detected(self):
        """A/B 选反 → 去重失效，必须报出映射冲突而不是静默翻倍。"""
        from phase1_ingest import plan_merge
        m = plan_merge(self._specs(swap=True))
        self.assertEqual(m["duplicates_removed"], 0)
        self.assertEqual(m["map_conflicts"], 6)
        self.assertEqual(m["message_count"], 21)

    def test_ingest_many_writes_v2_columns_and_gates(self):
        import sqlite3
        import phase1_ingest as p1
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "merged.db"
            s = p1.ingest_many(self._specs(), db_path=db)
            self.assertEqual(s["message_count"], 15)
            self.assertEqual(s["source_count"], 2)
            self.assertEqual(s["duplicates_removed"], 6)
            self.assertTrue(s["gates_all_pass"])
            g8 = s["gates_detail"]["G8_跨源重复"]
            self.assertEqual((g8["removed"], g8["map_conflicts"], g8["sources"]), (6, 0, 2))

            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 15)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE source_id IS NULL").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(DISTINCT source_id) FROM messages").fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM (SELECT dedup_key FROM messages WHERE dedup_key IS NOT "
                "NULL GROUP BY dedup_key HAVING COUNT(*)>1)").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(DISTINCT message_id) FROM messages").fetchone()[0], 15)
            self.assertEqual(conn.execute(
                "SELECT value FROM meta WHERE key='spec_version'").fetchone()[0], "2")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM import_sources").fetchone()[0], 2)
            ts = [r[0] for r in conn.execute("SELECT ts FROM messages ORDER BY ts")]
            self.assertEqual(ts, sorted(ts))
            self.assertEqual(len(ts), 15)
            conn.close()

    def test_same_file_twice_is_idempotent(self):
        """同一份文件登记两次 → 同一个 source_id，不产生重复消息。"""
        import phase1_ingest as p1
        specs = self._specs()
        twice = specs + [dict(specs[0])]
        m = p1.plan_merge(twice)
        self.assertEqual(m["message_count"], 15)          # message_id 相同 → 覆盖而非翻倍
        self.assertEqual({s["source_id"] for s in m["sources"]},
                         {s["source_id"] for s in p1.plan_merge(specs)["sources"]})

    def test_media_rows_survive_chat_rebuild(self):
        """媒体索引不属于聊天数据，库重建后必须还在。"""
        import sqlite3
        import phase1_ingest as p1
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            p1.ingest_many(self._specs(), db_path=db)
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO media (media_id,sha256,filename,kind,rel_path,added_at)"
                         " VALUES ('m1','s1','x.gif','sticker','media/x.gif','now')")
            conn.execute("INSERT INTO message_media VALUES ('mid-1','m1','filename')")
            conn.commit()
            conn.close()
            p1.ingest_many(self._specs(), db_path=db)     # 再来一次 → 全量重建
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM message_media").fetchone()[0], 1)
            conn.close()


class TestMediaStore(unittest.TestCase):
    """媒体独立导入通道：存档/索引/去重/关联/删除，全程在临时 ROOT 里跑。"""

    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (4).to_bytes(4, "big")
           + (6).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)

    def setUp(self):
        self._real_root = cfg_mod.ROOT
        self.tmp = Path(tempfile.mkdtemp(prefix="ifwe_media_"))
        cfg_mod.ROOT = self.tmp
        cfg_mod.set_active_profile("")
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        import phase5_common as pc
        pc.refresh_paths()
        import media_store
        self.ms = media_store

    def tearDown(self):
        import shutil
        import phase5_common as pc
        cfg_mod.ROOT = self._real_root
        cfg_mod.set_active_profile("")
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        pc.refresh_paths()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _img(self, name="sticker_1.gif") -> Path:
        p = self.tmp / "inbox"
        p.mkdir(exist_ok=True)
        f = p / name
        f.write_bytes(self.PNG)
        return f

    def test_import_dedup_and_archive(self):
        import media_store as ms
        r = ms.import_file(self._img("a.gif"), kind="sticker")
        self.assertTrue(r["ok"])
        self.assertFalse(r["dedup"])
        self.assertEqual(r["kind"], "sticker")
        self.assertEqual(r["width"], 4)
        self.assertEqual(r["height"], 6)
        # 同内容不同名 → 按哈希去重，只留一条
        r2 = ms.import_file(self._img("b.gif"), kind="sticker")
        self.assertTrue(r2["dedup"])
        self.assertEqual(r2["media_id"], r["media_id"])
        self.assertEqual(len(ms.list_media()), 1)
        self.assertTrue(list(ms.media_dir().glob("*.gif")))      # 存档落在 data/media/

    def test_kind_guess(self):
        import media_store as ms
        self.assertEqual(ms.guess_kind("x.webp", "auto"), "sticker")
        self.assertEqual(ms.guess_kind("x.png", "auto"), "image")
        self.assertEqual(ms.guess_kind("x.png", "sticker"), "sticker")

    def test_unsupported_ext_rejected(self):
        import media_store as ms
        bad = self.tmp / "x.svg"
        bad.write_text("<svg/>", encoding="utf-8")
        r = ms.import_file(bad)
        self.assertFalse(r["ok"])
        self.assertIn("格式", r["reason"])

    def test_safe_name_blocks_traversal(self):
        import media_store as ms
        self.assertEqual(ms.safe_name("../../config.yaml"), "config.yaml")
        self.assertEqual(ms.safe_name("a\\b\\c.png"), "c.png")

    def test_resolve_and_delete(self):
        import media_store as ms
        r = ms.import_file(self._img("pic.png"), kind="image")
        self.assertIsNotNone(ms.resolve("pic.png"))
        self.assertIsNotNone(ms.resolve(r["media_id"]))
        self.assertIsNone(ms.resolve("nope.png"))
        out = ms.delete(r["media_id"])
        self.assertTrue(out["ok"])
        self.assertEqual(ms.list_media(), [])
        self.assertIsNone(ms.resolve("pic.png"))

    def test_link_messages_by_filename(self):
        import media_store as ms
        from phase1_ingest import ingest
        ingest(SAMPLES / "telegram_sample.json", {"user100": "A", "user111": "B"},
               db_path=cfg_mod.db_path())
        ms.import_file(self._img("sticker_1.webp"), kind="sticker")
        link = ms.link_messages()
        self.assertEqual(link["linked"], 1)               # 样本里那条贴纸消息
        self.assertEqual(link["candidates"], 3)           # 表情/图片/文件各一条
        self.assertEqual(link["unmatched"], 2)
        items = ms.list_media()
        self.assertEqual(items[0]["linked_messages"], 1)


@unittest.skipUnless(HAS_WEB, "需要 fastapi + httpx（打包 venv / CI 环境）")
class TestImportApiContract(unittest.TestCase):
    """前端 ↔ API 的载荷契约回归。

    教训（2026-09-14 实测复现）：界面发的是 `{import_id, a, b}`，而服务端
    `ImportSourceSel` 要 `sender_a/sender_b`；pydantic 默认忽略未知字段 → 映射被静默
    读成空串 → 用户看到的是「「telegram_result.json」还没选 A/B 双方账号」。
    之前的测试只按**服务端自己的命名**发请求，所以完全没覆盖到。

    所以这里断言的是**界面真实发送的形状**，而不是服务端觉得应该是什么形状。
    改前端载荷字段名，或者给 ImportSourceSel 改名，都必须同步这条测试。
    """

    def setUp(self):
        self._real_root = cfg_mod.ROOT
        self.tmp = Path(tempfile.mkdtemp(prefix="ifwe_api_"))
        cfg_mod.ROOT = self.tmp
        cfg_mod.set_active_profile("")
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        import phase5_common as pc
        import phase15_api as api
        pc.refresh_paths()
        api._schema_ready.clear()
        api._reset_caches()
        from fastapi.testclient import TestClient
        self.api = api
        self.c = TestClient(api.app)

    def tearDown(self):
        import shutil
        try:
            self.c.post("/api/import/cancel")
        except Exception:
            pass
        self.api._schema_ready.clear()
        self.api._reset_caches()
        cfg_mod.ROOT = self._real_root
        cfg_mod.set_active_profile("")
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        import phase5_common as pc
        pc.refresh_paths()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _batch(self):
        src = self.tmp / "t.csv"
        src.write_text("timestamp,content,accountName\n"
                       "1726010000,甲,阿明\n1726010060,乙,小夏\n"
                       "1726010120,丙,阿明\n1726010180,丁,小夏\n", encoding="utf-8")
        r = self.c.post("/api/import/upload", files=[
            ("files", ("t.csv", src.read_bytes(), "text/csv"))])
        self.assertEqual(r.status_code, 200, r.text[:200])
        return r.json()["batch_id"], r.json()["files"][0]["import_id"]

    def test_frontend_payload_shape_commits(self):
        """界面当前发送的形状（sender_a/sender_b）必须能提交成功。"""
        bid, iid = self._batch()
        r = self.c.post("/api/import/commit", json={
            "batch_id": bid, "exclude": [],
            "sources": [{"import_id": iid, "sender_a": "阿明", "sender_b": "小夏"}]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["summary"]["message_count"], 4)

    def test_frontend_payload_shape_merges(self):
        bid, iid = self._batch()
        r = self.c.post("/api/import/merge", json={
            "batch_id": bid, "exclude": [],
            "sources": [{"import_id": iid, "sender_a": "阿明", "sender_b": "小夏"}]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["merge"]["message_count"], 4)

    def test_legacy_ab_payload_shape_accepted(self):
        """旧的 {a, b} 写法仍收（别名）—— 已缓存的旧前端不至于整批失败。"""
        bid, iid = self._batch()
        r = self.c.post("/api/import/merge", json={
            "batch_id": bid, "sources": [{"import_id": iid, "a": "阿明", "b": "小夏"}]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["merge"]["message_count"], 4)

    def _write_config(self, a_match, b_match):
        """在临时 ROOT 里写一份 config.yaml（people.A/B.match 支持字符串或列表）。"""
        import json as _json
        a = _json.dumps(a_match, ensure_ascii=False) if isinstance(a_match, list) else f'"{a_match}"'
        b = _json.dumps(b_match, ensure_ascii=False) if isinstance(b_match, list) else f'"{b_match}"'
        (self.tmp / "config.yaml").write_text(
            "# 测试用\n"
            "people:\n"
            f"  A: {{ key: \"A\", display: \"你\", match: {a} }}\n"
            f"  B: {{ key: \"B\", display: \"TA\", match: {b} }}\n"
            "chat:\n  source: \"data/raw/chat.jsonl\"\n",
            encoding="utf-8")
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})     # 让 load() 重读

    def test_mapping_default_falls_back_to_guess(self):
        """没有配置时，默认值是体检的统计猜测（A=消息较多一方），并标明来源。"""
        bid, _iid = self._batch()
        it = self.c.post("/api/import/preview", json={"batch_id": bid}).json()["items"][0]
        md = it["report"].get("mapping_default") or {}
        self.assertEqual(md.get("source"), "guess", str(md))
        self.assertTrue(md.get("sender_a") and md.get("sender_b"))

    def test_mapping_default_prefers_configured_names(self):
        """config.yaml 里登记过的「我/对方」账号名必须优先于统计猜测。"""
        self._write_config(["阿明"], ["小夏"])
        bid, _iid = self._batch()
        it = self.c.post("/api/import/preview", json={"batch_id": bid}).json()["items"][0]
        md = it["report"].get("mapping_default") or {}
        self.assertEqual(md.get("source"), "config", str(md))
        self.assertEqual((md.get("sender_a"), md.get("sender_b")), ("阿明", "小夏"))

    def test_commit_records_sender_matches_into_config(self):
        """提交成功后应把本次的「我/对方」账号名记进 config.yaml（供下次预选）。"""
        self._write_config("", "")
        bid, iid = self._batch()
        r = self.c.post("/api/import/commit", json={
            "batch_id": bid, "exclude": [],
            "sources": [{"import_id": iid, "sender_a": "阿明", "sender_b": "小夏"}]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertIn("config.yaml", r.json().get("config_note", ""))
        text = (self.tmp / "config.yaml").read_text(encoding="utf-8")
        self.assertIn('"阿明"', text)
        self.assertIn('"小夏"', text)
        self.assertIn("# 测试用", text)                 # 注释保住（最小替换而非整体重写）

    def test_persist_sender_matches_idempotent(self):
        """值本来就对 → 不写盘（返回 False），且列表形态可被二次读取。"""
        self._write_config(["阿明"], ["小夏"])
        self.assertFalse(cfg_mod.persist_sender_matches(["阿明"], ["小夏"]))
        self.assertTrue(cfg_mod.persist_sender_matches(["阿明", "wxid_me"], ["小夏", "wxid_ta"]))
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        people = cfg_mod.load()["people"]
        self.assertEqual(people["A"]["match"], ["阿明", "wxid_me"])
        self.assertEqual(people["B"]["match"], ["小夏", "wxid_ta"])

    def test_swap_mapping_fixes_reversed_source(self):
        """两份来源内容重叠：一份选反 → 去重失效且报出冲突；对调修回 → 恢复正常。

        这是「来源有存档所以不用重传」的核心价值场景，也是 A/B 选反最容易踩的坑。
        """
        csv = ("timestamp,content,accountName\n"
               "1726010000,同一句话,{a}\n1726010060,另一句话,{b}\n")
        p1 = self.tmp / "one.csv"
        p2 = self.tmp / "two.csv"
        p1.write_text(csv.format(a="阿明", b="小夏"), encoding="utf-8")
        p2.write_text(csv.format(a="A甲", b="B乙"), encoding="utf-8")
        r = self.c.post("/api/import/upload", files=[
            ("files", ("one.csv", p1.read_bytes(), "text/csv")),
            ("files", ("two.csv", p2.read_bytes(), "text/csv"))])
        self.assertEqual(r.status_code, 200, r.text[:200])
        bid = r.json()["batch_id"]
        # 注意：服务端保存的文件顺序不保证等于上传顺序（多段 multipart 由浏览器决定次序），
        # 所以必须按文件名取 import_id，不能按数组下标。
        ids = {f["filename"]: f["import_id"] for f in r.json()["files"]}
        self.assertEqual(set(ids), {"one.csv", "two.csv"})

        def payload(second_reversed):
            return [{"import_id": ids["one.csv"], "sender_a": "阿明", "sender_b": "小夏"},
                    {"import_id": ids["two.csv"],
                     "sender_a": "B乙" if second_reversed else "A甲",
                     "sender_b": "A甲" if second_reversed else "B乙"}]

        m = self.c.post("/api/import/merge", json={
            "batch_id": bid, "sources": payload(False)}).json()["merge"]
        self.assertEqual((m["duplicates_removed"], m["map_conflicts"]), (2, 0), str(m))

        m2 = self.c.post("/api/import/merge", json={
            "batch_id": bid, "sources": payload(True)}).json()["merge"]
        self.assertEqual((m2["duplicates_removed"], m2["map_conflicts"]), (0, 2), str(m2))

        r = self.c.post("/api/import/commit", json={
            "batch_id": bid, "sources": payload(False), "exclude": []})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["summary"]["message_count"], 2)

        src = {s["name"]: s for s in self.c.get("/api/import/sources").json()["sources"]}
        two = src["two.csv"]
        self.assertEqual(two["sender_map"], {"A甲": "A", "B乙": "B"})

        # 对调 → 去重失效并报出冲突（问题能被看见）
        sw = self.c.post(f"/api/import/sources/{two['source_id']}/mapping", json={})
        self.assertEqual(sw.status_code, 200, sw.text[:300])
        g8 = sw.json()["summary"]["gates_detail"]["G8_跨源重复"]
        self.assertEqual((g8["removed"], g8["map_conflicts"]), (0, 2), str(g8))
        self.assertEqual(sw.json()["sender_map"], {"A甲": "B", "B乙": "A"})

        # 再对调 → 恢复
        sw2 = self.c.post(f"/api/import/sources/{two['source_id']}/mapping", json={})
        g8b = sw2.json()["summary"]["gates_detail"]["G8_跨源重复"]
        self.assertEqual((g8b["removed"], g8b["map_conflicts"]), (2, 0), str(g8b))

    def test_mapping_endpoint_validates(self):
        bid, iid = self._batch()
        c = self.c.post("/api/import/commit", json={
            "batch_id": bid, "exclude": [],
            "sources": [{"import_id": iid, "sender_a": "阿明", "sender_b": "小夏"}]})
        sid = c.json()["registered"][0]["source_id"]
        r = self.c.post(f"/api/import/sources/{sid}/mapping",
                        json={"sender_a": "阿明", "sender_b": "不存在的人"})
        self.assertEqual(r.status_code, 400, r.text[:300])
        self.assertIn("必须是这份来源出现的两个账号", r.json()["detail"])
        self.assertEqual(self.c.post("/api/import/sources/nope/mapping", json={}).status_code, 404)

    def test_unknown_field_is_rejected_loudly(self):
        """字段名写错必须当场 422，而不是被静默忽略成空串。"""
        bid, iid = self._batch()
        r = self.c.post("/api/import/merge", json={
            "batch_id": bid, "sources": [{"import_id": iid, "sender_x": "阿明", "b": "小夏"}]})
        self.assertEqual(r.status_code, 422, r.text[:300])
        r2 = self.c.post("/api/import/merge", json={"batch_id": bid, "nope": 1, "sources": []})
        self.assertEqual(r2.status_code, 422, r2.text[:300])

    def test_missing_mapping_still_gives_clear_400(self):
        bid, iid = self._batch()
        r = self.c.post("/api/import/commit", json={
            "batch_id": bid, "sources": [{"import_id": iid}]})
        self.assertEqual(r.status_code, 400, r.text[:300])
        self.assertIn("还没选 A/B", r.json()["detail"])

    def test_stale_lock_is_reclaimed_not_409(self):
        """上传后客户端没走完 → 再传应直接接管回收，而不是 409 卡住。"""
        self._batch()
        r2 = self.c.post("/api/import/upload", files=[
            ("files", ("t2.csv", b"timestamp,content,accountName\n1726010000,x,A\n",
                       "text/csv"))])
        self.assertEqual(r2.status_code, 200, r2.text[:200])
        self.assertTrue(r2.json().get("reclaimed"))

    def test_preview_error_releases_lock(self):
        """预览自身出错必须释放锁，否则用户被一个自己解除不了的锁挡在门外。"""
        import import_sources
        bid, _iid = self._batch()
        saved = import_sources.list_view
        import_sources.list_view = lambda: (_ for _ in ()).throw(RuntimeError("模拟异常"))
        try:
            r = self.c.post("/api/import/preview", json={"batch_id": bid})
        finally:
            import_sources.list_view = saved
        self.assertEqual(r.status_code, 500, r.text[:300])
        self.assertNotIn("导入正在进行中", json.dumps(self.c.get("/api/health").json()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
