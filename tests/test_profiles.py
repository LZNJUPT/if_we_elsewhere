# -*- coding: utf-8 -*-
"""
v0.3 多好友（profile）/ 配置写回 / 密钥存储的单元测试。

只用标准库 + config / profiles / secret_store（不依赖 fastapi、numpy），
全程在临时目录里跑：把 cfg_mod.ROOT 指向临时项目根，永不碰仓库 data/。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "app")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config as cfg_mod          # noqa: E402
import profiles as pmod           # noqa: E402
import secret_store               # noqa: E402


class ProfileBase(unittest.TestCase):
    """把「项目根」搬到临时目录，隔离跑；每个用例一份干净的 data/"""

    def setUp(self) -> None:
        self._real_root = cfg_mod.ROOT
        self.tmp = Path(tempfile.mkdtemp(prefix="ifwe_test_"))
        cfg_mod.ROOT = self.tmp
        self._reset_caches()

    def tearDown(self) -> None:
        cfg_mod.ROOT = self._real_root
        self._reset_caches()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------
    def _reset_caches(self) -> None:
        cfg_mod.set_active_profile("")
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        cfg_mod._REG_CACHE.update({"path": None, "mtime": -1.0, "items": []})
        cfg_mod._PF_CACHE.update({"path": None, "mtime": -1.0, "data": {}})

    def base(self) -> Path:
        return cfg_mod.base_data_dir()

    def seed_legacy_data(self) -> None:
        """造一份「老用户」数据：库 + persona + raw 子目录"""
        b = self.base()
        (b / "persona").mkdir(parents=True, exist_ok=True)
        (b / "raw").mkdir(parents=True, exist_ok=True)
        (b / "ifwe_v1.db").write_bytes(b"SQLite format 3\x00")
        (b / "persona" / "persona_v1_A.json").write_text('{"display_name": "x"}',
                                                         encoding="utf-8")
        (b / "raw" / "chat.jsonl").write_text("{}\n", encoding="utf-8")


class TestMigration(ProfileBase):

    def test_fresh_install_creates_default_profile(self):
        res = pmod.ensure_migrated()
        self.assertTrue(res["ok"])
        self.assertFalse(res["migrated"])
        self.assertTrue(res.get("created_default"))
        self.assertEqual([p["id"] for p in cfg_mod.read_registry()], ["default"])
        self.assertTrue((self.base() / "profiles" / "default").is_dir())

    def test_legacy_migration_moves_and_backs_up(self):
        self.seed_legacy_data()
        res = pmod.ensure_migrated()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["migrated"])
        dest = self.base() / "profiles" / "default"
        self.assertTrue((dest / "ifwe_v1.db").is_file())
        self.assertTrue((dest / "persona" / "persona_v1_A.json").is_file())
        self.assertTrue((dest / "raw" / "chat.jsonl").is_file())
        self.assertFalse((self.base() / "ifwe_v1.db").exists())
        backup = self.tmp / res["backup"]
        self.assertTrue((backup / "ifwe_v1.db").is_file())
        self.assertTrue((backup / "persona" / "persona_v1_A.json").is_file())

    def test_migration_is_idempotent(self):
        self.seed_legacy_data()
        pmod.ensure_migrated()
        again = pmod.ensure_migrated()
        self.assertTrue(again["ok"])
        self.assertFalse(again["migrated"])

    def test_migration_skips_runtime_artifacts(self):
        """logs/ 与 .ifwe.lock 是运行期产物：不参与迁移，也不能把迁移搞失败
        （回归：打包版双击启动会先创建 data/logs/desktop.log 并持有句柄）"""
        self.seed_legacy_data()
        (self.base() / "logs").mkdir(exist_ok=True)
        (self.base() / "logs" / "desktop.log").write_text("x", encoding="utf-8")
        (self.base() / ".ifwe.lock").write_text("1 8015", encoding="utf-8")
        res = pmod.ensure_migrated()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["migrated"], res)
        self.assertNotIn("logs", res.get("moved", []))
        self.assertNotIn(".ifwe.lock", res.get("moved", []))
        self.assertTrue((self.base() / "logs" / "desktop.log").is_file())   # 留在原地
        self.assertTrue((self.base() / ".ifwe.lock").is_file())
        self.assertTrue((self.base() / "profiles" / "default" / "ifwe_v1.db").is_file())

    def test_fresh_install_with_only_runtime_artifacts(self):
        """全新安装但 data/ 里已有 logs/（打包版先建日志）：不该触发迁移也不该报错"""
        (self.base() / "logs").mkdir(parents=True, exist_ok=True)
        (self.base() / "logs" / "desktop.log").write_text("x", encoding="utf-8")
        res = pmod.ensure_migrated()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res.get("created_default"), res)
        self.assertFalse(res["migrated"])
        self.assertTrue((self.base() / "logs" / "desktop.log").is_file())

    def test_custom_data_dir_is_not_migrated(self):
        """IFWE_DATA_DIR 场景（如 run.py demo）不参与好友迁移"""
        import os
        os.environ["IFWE_DATA_DIR"] = "data_demo"
        try:
            self._reset_caches()
            (self.tmp / "data_demo").mkdir(parents=True, exist_ok=True)
            (self.tmp / "data_demo" / "ifwe_v1.db").write_bytes(b"x")
            res = pmod.ensure_migrated()
            self.assertTrue(res["ok"])
            self.assertIn("skipped", res)
            self.assertFalse(cfg_mod.profiles_registry_path().exists())
        finally:
            os.environ.pop("IFWE_DATA_DIR", None)
            self._reset_caches()


class TestProfileSwitch(ProfileBase):

    def setUp(self):
        super().setUp()
        pmod.ensure_migrated()

    def test_paths_follow_active_profile(self):
        cfg_mod.set_active_profile("default")
        b = self.base() / "profiles" / "default"
        self.assertEqual(cfg_mod.data_dir(), b)
        self.assertEqual(cfg_mod.db_path(), b / "ifwe_v1.db")
        self.assertEqual(cfg_mod.persona_dir(), b / "persona")
        self.assertEqual(cfg_mod.cache_dir(), b / "cache")

    def test_create_unique_ids_and_rename(self):
        p1 = pmod.create("小林")
        self.assertRegex(p1["id"], r"^friend-[0-9a-f]{6}$")       # 中文名 → 随机后缀
        self.assertTrue((self.base() / "profiles" / p1["id"]).is_dir())
        ada = pmod.create("Ada Lovelace")
        self.assertEqual(ada["id"], "ada-lovelace")
        dup = pmod.create("Ada Lovelace")
        self.assertNotEqual(dup["id"], ada["id"])
        pmod.rename(ada["id"], "Ada L.")
        self.assertEqual(pmod.get(ada["id"])["name"], "Ada L.")
        with self.assertRaises(ValueError):
            pmod.create("")
        with self.assertRaises(ValueError):
            pmod.rename(ada["id"], "x" * 30)

    def test_profile_yaml_overrides_global_only_for_that_friend(self):
        pid = pmod.create("Ada")["id"]
        d = cfg_mod.profile_data_dir(pid)
        emoji_dir = self.tmp / "emojis"                 # 用平台中立的绝对路径，跨平台可跑
        emoji_dir.mkdir(exist_ok=True)
        (d / "profile.yaml").write_text(
            f'media:\n  emojis_dir: "{emoji_dir.as_posix()}"\n'
            "people:\n  B: { key: 'B', display: 'Ada' }\n", encoding="utf-8")
        cfg_mod.set_active_profile(pid)
        self.assertEqual(cfg_mod.emojis_dir(), emoji_dir)
        self.assertEqual(cfg_mod.sender_names()["B"], "Ada")
        self.assertEqual(cfg_mod.load()["defaults"]["port"], 8015)     # 全局值不被污染
        cfg_mod.set_active_profile("default")
        self.assertNotEqual(cfg_mod.sender_names()["B"], "Ada")

    def test_delete_removes_dir_and_keeps_others(self):
        pid = pmod.create("Ada")["id"]
        (cfg_mod.profile_data_dir(pid) / "ifwe_v1.db").write_bytes(b"x")
        res = pmod.delete(pid)
        self.assertTrue(res["ok"], res)
        self.assertFalse((self.base() / "profiles" / pid).exists())
        self.assertTrue((self.base() / "profiles" / "default").is_dir())
        self.assertNotIn(pid, [p["id"] for p in cfg_mod.read_registry()])

    def test_last_profile_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            pmod.delete("default")

    def test_protected_dirs_are_refused(self):
        self.assertTrue(pmod._is_protected_dir(cfg_mod.root()))
        self.assertTrue(pmod._is_protected_dir(self.base()))
        self.assertTrue(pmod._is_protected_dir(cfg_mod.profiles_base()))
        self.assertFalse(pmod._is_protected_dir(cfg_mod.profiles_base() / "somebody"))

    def test_status_shape(self):
        st = pmod.status()
        self.assertEqual(st["active"], "")
        # 未启用 profile 时，第一行是「当前数据目录」占位（truthful：data_dir 就是基础目录）
        self.assertTrue(st["profiles"][0]["synthetic"])
        self.assertTrue(st["profiles"][0]["active"])
        self.assertEqual(st["profiles"][1]["id"], "default")
        self.assertFalse(st["profiles"][1]["active"])
        self.assertIn("profiles.json", st["registry"])
        self.assertEqual(st["profiles"][1]["dir"], "data/profiles/default")

    def test_status_marks_active_profile(self):
        pid = pmod.create("Ada")["id"]
        cfg_mod.set_active_profile(pid)
        st = pmod.status()
        self.assertEqual(st["active"], pid)
        rows = {p["id"]: p for p in st["profiles"]}
        self.assertTrue(rows[pid]["active"])
        self.assertFalse(rows["default"]["active"])
        self.assertNotIn("", rows)                                  # 有了激活好友就不再有占位行

    def test_status_placeholder_without_registry(self):
        """demo / 自定义数据目录：没有注册表时给一个占位项，界面不至于空白"""
        import os
        os.environ["IFWE_DATA_DIR"] = "data_demo"
        try:
            self._reset_caches()
            st = pmod.status()
            self.assertTrue(st["profiles"][0].get("synthetic"))
            self.assertEqual(st["profiles"][0]["name"], "当前数据目录")
        finally:
            os.environ.pop("IFWE_DATA_DIR", None)
            self._reset_caches()


class TestPersistSettings(ProfileBase):

    def _write_cfg(self) -> Path:
        src = cfg_mod.root() / "config.example.yaml"
        if not src.is_file():                                     # 临时根下没有模板
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(
                "llm:\n"
                "  provider: \"deepseek\"            # deepseek | 任意 OpenAI 兼容端点\n"
                "  model: \"deepseek-chat\"\n"
                "  api_key_env: \"LLM_API_KEY\"      # 只从环境变量读密钥\n"
                "  base_url: \"https://api.deepseek.com\"\n"
                "  max_tokens: 8192\n"
                "defaults:\n  port: 8015\n", encoding="utf-8")
        dst = cfg_mod.root() / "config.yaml"
        shutil.copy(src, dst)
        return dst

    def test_targeted_write_keeps_comments_and_other_fields(self):
        cfg_path = self._write_cfg()
        ok, note = cfg_mod.persist_settings({"model": "deepseek-reasoner",
                                             "base_url": "https://example.com/v1"})
        self.assertTrue(ok, note)
        text = cfg_path.read_text(encoding="utf-8")
        self.assertIn("# 只从环境变量读密钥", text)                # 注释保留
        self.assertIn('model: "deepseek-reasoner"', text)
        self.assertIn("https://example.com/v1", text)
        self.assertIn('provider: "deepseek"', text)                # 未指定字段不动
        self.assertIn("port: 8015", text)
        cfg_mod._CFG_CACHE.update({"key": None, "cfg": {}})
        self.assertEqual(cfg_mod.load()["llm"]["model"], "deepseek-reasoner")

    def test_no_change_returns_false(self):
        self._write_cfg()
        cfg_mod.persist_settings({"model": "x"})
        ok, note = cfg_mod.persist_settings({"model": "x"})
        self.assertFalse(ok)

    def test_creates_config_from_example_when_missing(self):
        cfg_path = self._write_cfg()
        cfg_path.unlink()
        ok, _ = cfg_mod.persist_settings({"model": "m"})
        self.assertTrue(ok)
        self.assertTrue((cfg_mod.root() / "config.yaml").is_file())


class TestSecretStore(ProfileBase):

    def setUp(self):
        super().setUp()
        # 不碰真实的凭据管理器：强制走 DPAPI 回退路径
        self._real_ok = secret_store.keyring_ok
        secret_store.keyring_ok = lambda: False
        secret_store._cache["backend"] = ""

    def tearDown(self):
        secret_store.keyring_ok = self._real_ok
        secret_store._cache.update({"keyring_ok": None, "backend": ""})
        super().tearDown()

    def test_dpapi_roundtrip_and_no_plaintext(self):
        key = "sk-" + "a1b2c3d4" * 4
        if sys.platform != "win32":
            self.skipTest("DPAPI 仅 Windows 可用")
        backend = secret_store.save_key(key)
        self.assertEqual(backend, "dpapi-file")
        self.assertEqual(secret_store.read_key(), key)
        blob = secret_store.fallback_path().read_bytes()
        self.assertNotIn(key.encode(), blob)                     # 落盘的是密文
        self.assertEqual(secret_store.hint(key), key[:3] + "***" + key[-3:])
        self.assertNotIn(key, secret_store.hint(key))
        self.assertTrue(secret_store.clear_key())
        self.assertIsNone(secret_store.read_key())

    def test_hint_of_empty_or_short_key(self):
        self.assertEqual(secret_store.hint(""), "")
        self.assertEqual(secret_store.hint("abc"), "***")

    def test_describe_never_leaks_key(self):
        if sys.platform != "win32":
            self.skipTest("DPAPI 仅 Windows 可用")
        secret_store.save_key("sk-" + "z" * 20)
        desc = secret_store.describe()
        self.assertEqual(desc["key_state"], "set")
        self.assertNotIn("z" * 20, str(desc))
        secret_store.clear_key()


class TestRemoveTree(ProfileBase):

    def test_removes_files_first_then_dirs(self):
        root = self.base() / "profiles" / "victim"
        (root / "sub").mkdir(parents=True, exist_ok=True)
        (root / "a.txt").write_text("a", encoding="utf-8")
        (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
        errs = pmod.remove_tree(root)
        self.assertEqual(errs, [])
        self.assertFalse(root.exists())

    def test_missing_dir_is_noop(self):
        self.assertEqual(pmod.remove_tree(self.base() / "nope"), [])


class TestSchemas(ProfileBase):
    """回归：新库/新好友首次访问必须把结构建齐（曾出现空库上缺表报错）"""

    WANT = ("meta", "messages", "sessions", "daily_stats", "events",
            "turning_points", "relationship_state", "facts", "persona_snapshot",
            "sim_runs", "sim_messages", "sim_rel_state", "ifr_branch")

    def test_apply_all_schemas_creates_every_table_and_is_idempotent(self):
        import phase5_common as pc
        conn = pc.connect()
        pc.apply_all_schemas(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in self.WANT if t not in tables]
        self.assertEqual(missing, [], f"缺表：{missing}（tables={sorted(tables)}）")
        pc.apply_all_schemas(conn)          # 幂等：再来一次不报错、表还在
        tables2 = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(self.WANT) <= tables2)
        conn.close()

    def test_rel_state_at_on_empty_db_does_not_raise(self):
        """空库上取「最近一期关系状态」应返回 None，而不是 OperationalError"""
        import phase5_common as pc
        conn = pc.connect()
        pc.apply_all_schemas(conn)
        self.assertIsNone(pc.rel_state_at(conn, "2026-09-13"))
        conn.close()


class TestSafeStdio(unittest.TestCase):
    """回归：双击启动的窗口 exe 没有任何标准句柄（sys.stdout=None），
    uvicorn 日志配置调 sys.stdout.isatty() 直接崩（2026-09-13 用户实测）。"""

    def test_none_streams_replaced_by_usable_sink(self):
        old = (sys.stdout, sys.stderr)
        try:
            sys.stdout = None
            sys.stderr = None
            cfg_mod._safe_stdio()
            self.assertIsNotNone(sys.stdout)
            self.assertIsNotNone(sys.stderr)
            self.assertFalse(sys.stdout.isatty())            # uvicorn 日志初始化需要它
            self.assertTrue(sys.stdout.writable())
            sys.stdout.write("probe")                        # 不能抛
            sys.stderr.write("probe")
        finally:
            sys.stdout, sys.stderr = old

    def test_real_streams_keep_reconfigure_only(self):
        """已有真实流（重定向场景）时只做编码固定，不替换对象。"""
        import io
        old = (sys.stdout, sys.stderr)
        buf = io.StringIO()
        try:
            sys.stdout = buf
            sys.stderr = buf
            cfg_mod._safe_stdio()
            self.assertIs(sys.stdout, buf)
        finally:
            sys.stdout, sys.stderr = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
