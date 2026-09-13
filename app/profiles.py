# -*- coding: utf-8 -*-
"""
IfWe · 多好友（profile）体系 —— 每个好友 = 一个完全独立的数据目录

设计（见 GUI 路线图 P2）：
  - 注册表 `data/profiles.json`：`[{id, name, created, data_dir, last_active}]`
    （`data_dir` 相对基础数据目录，如 `profiles/ada`）
  - 每个好友的数据目录里放整套东西：库、persona、表情包缓存、对话线，
    由 `config.set_active_profile(id)` 切换 `data_dir()` 实现隔离，**不动任何表结构**
  - 老用户首次启动自动迁移：`data/` 内容 → `data/profiles/default/`，
    迁移前整目录复制备份到 `data_backup_<date>/`，中途失败自动回滚

本机注意：safe-delete 会把目录级删除重定向回收站并可能 fail-closed，
因此删除好友目录一律 **逐文件 unlink + 自底向上 rmdir**（同导入向导的清理模式）。
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from pathlib import Path

import config as cfg_mod

DEFAULT_ID = "default"
DEFAULT_NAME = "默认好友"
DEMO_ID = "demo"
DEMO_NAME = "示例好友（虚构数据）"
ID_RE = re.compile(cfg_mod.PROFILE_ID_RE_STR)
BASE_NAME = "profiles.json"


# ---------------------------------------------------------------- 小工具
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _rel(p: Path | None) -> str:
    """给界面看的相对路径（绝不回传绝对路径，避免泄露本机目录结构）"""
    if p is None:
        return ""
    try:
        return p.resolve().relative_to(cfg_mod.root().resolve()).as_posix()
    except Exception:
        return p.name


def slugify(name: str) -> str:
    """好友名 → id（纯中文名等拿不到 ASCII 片段时退回 `friend-<随机>`，保证可读且唯一）"""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name or "").strip("-").lower()
    if not s:
        s = "friend-" + secrets.token_hex(3)
    return s[:28]


def _unique_id(base: str, items: list[dict]) -> str:
    used = {it.get("id") for it in items}
    pid = base
    n = 2
    while pid in used:
        pid = f"{base}-{n}"
        n += 1
    return pid


def _entry(pid: str, name: str, data_dir: str, demo: bool = False) -> dict:
    return {"id": pid, "name": name, "created": _now(),
            "data_dir": data_dir, "last_active": "", **({"demo": True} if demo else {})}


def remove_tree(root: Path) -> list[str]:
    """逐文件 unlink + 自底向上 rmdir（本机 safe-delete 对目录级删除 fail-closed）。
    返回错误信息列表（空 = 全部删除成功）。"""
    errs: list[str] = []
    root = Path(root)
    if not root.exists():
        return errs
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                p.unlink()
            except OSError as e:
                errs.append(f"{p.name}: {e}")
        try:
            os.rmdir(dirpath)
        except OSError as e:
            errs.append(f"{Path(dirpath).name}/: {e}")
    return errs


def _is_protected_dir(d: Path) -> bool:
    """禁止删除基础数据目录本身（或它的祖先/同级仓库根）"""
    try:
        r = d.resolve()
    except Exception:
        return True
    for keep in (cfg_mod.root(), cfg_mod.base_data_dir(), cfg_mod.profiles_base()):
        try:
            k = keep.resolve()
        except Exception:
            continue
        if r == k or r in k.parents:
            return True
    return False


# ---------------------------------------------------------------- 注册表 CRUD
def list_profiles() -> list[dict]:
    return cfg_mod.read_registry()


def get(pid: str) -> dict | None:
    return cfg_mod.profile_entry(pid)


def touch(pid: str) -> None:
    items = cfg_mod.read_registry()
    hit = False
    for it in items:
        if it.get("id") == pid:
            it["last_active"] = _now()
            hit = True
    if hit:
        cfg_mod.write_registry(items)


def create(name: str, pid: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("好友名不能为空")
    if len(name) > 24:
        raise ValueError("好友名过长（最多 24 个字）")
    items = cfg_mod.read_registry()
    pid = (pid or "").strip() or slugify(name)
    if pid == DEMO_ID and not any(it.get("id") == DEMO_ID for it in items):
        raise ValueError("该 id 为示例好友预留")
    pid = _unique_id(pid, items)
    if not ID_RE.match(pid):
        raise ValueError("非法好友 id")
    d = cfg_mod.profiles_base() / pid
    d.mkdir(parents=True, exist_ok=True)
    entry = _entry(pid, name, f"profiles/{pid}")
    items.append(entry)
    cfg_mod.write_registry(items)
    return entry


def rename(pid: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("好友名不能为空")
    if len(name) > 24:
        raise ValueError("好友名过长（最多 24 个字）")
    items = cfg_mod.read_registry()
    hit = None
    for it in items:
        if it.get("id") == pid:
            it["name"] = name
            hit = it
    if hit is None:
        raise KeyError(pid)
    cfg_mod.write_registry(items)
    return hit


def delete(pid: str) -> dict:
    """删除好友 = 删除其数据目录（逐文件 unlink）；至少保留一个好友"""
    items = cfg_mod.read_registry()
    if not any(it.get("id") == pid for it in items):
        raise KeyError(pid)
    if len(items) <= 1:
        raise ValueError("至少需要保留一个好友")
    d = cfg_mod.profile_data_dir(pid)
    if d is not None and _is_protected_dir(d):
        raise ValueError("该目录受保护，已拒绝删除")
    errs = remove_tree(d) if d is not None else []
    if errs:
        return {"ok": False, "errors": errs[:6], "kept": True}
    cfg_mod.write_registry([it for it in items if it.get("id") != pid])
    return {"ok": True, "removed": _rel(d)}


# ---------------------------------------------------------------- 首次迁移
def ensure_migrated() -> dict:
    """首次启动把 `data/` 内容迁到 `data/profiles/default/`（幂等；仅默认数据目录生效）。

    返回 {ok, migrated, backup?, error?}；任何失败都不改动原数据（或已回滚）。
    """
    base = cfg_mod.base_data_dir()
    default_base = cfg_mod.root() / "data"
    try:
        same = base.resolve() == default_base.resolve()
    except Exception:
        same = False
    if not same:
        return {"ok": True, "migrated": False,
                "skipped": "自定义数据目录（IFWE_DATA_DIR）下不启用好友迁移"}

    if cfg_mod.profiles_registry_path().exists():
        return {"ok": True, "migrated": False}

    entries = []
    if base.exists():
        entries = sorted((p for p in base.iterdir()
                          if p.name not in ("profiles", BASE_NAME)), key=lambda p: p.name)
    dest = cfg_mod.profiles_base() / DEFAULT_ID

    if not entries:                                    # 全新安装：建一个空的默认好友
        try:
            dest.mkdir(parents=True, exist_ok=True)
            cfg_mod.write_registry([_entry(DEFAULT_ID, DEFAULT_NAME, f"profiles/{DEFAULT_ID}")])
        except OSError as e:
            return {"ok": False, "error": f"初始化好友注册表失败: {e}"}
        return {"ok": True, "migrated": False, "created_default": True}

    backup = cfg_mod.root() / f"data_backup_{time.strftime('%Y%m%d')}"
    try:
        backup.mkdir(parents=True, exist_ok=True)
        for p in entries:
            target = backup / p.name
            if target.exists():
                continue
            if p.is_dir():
                shutil.copytree(p, target)
            else:
                shutil.copy2(p, target)
    except Exception as e:
        return {"ok": False, "backup": _rel(backup),
                "error": f"迁移前备份失败（未改动任何数据）：{e}"}

    dest.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    try:
        for p in entries:
            shutil.move(str(p), str(dest / p.name))
            moved.append(p.name)
    except Exception as e:
        for name in moved:                             # 回滚已移动的内容
            try:
                shutil.move(str(dest / name), str(base / name))
            except Exception:
                pass
        return {"ok": False, "backup": _rel(backup), "error": f"迁移失败并已回滚：{e}"}

    try:
        cfg_mod.write_registry([_entry(DEFAULT_ID, DEFAULT_NAME, f"profiles/{DEFAULT_ID}")])
    except OSError as e:
        return {"ok": False, "backup": _rel(backup), "error": f"写入注册表失败：{e}"}
    return {"ok": True, "migrated": True, "moved": moved, "backup": _rel(backup)}


# ---------------------------------------------------------------- 示例好友
def ensure_demo_profile() -> dict:
    items = cfg_mod.read_registry()
    for it in items:
        if it.get("id") == DEMO_ID:
            return it
    entry = _entry(DEMO_ID, DEMO_NAME, f"profiles/{DEMO_ID}", demo=True)
    items.append(entry)
    cfg_mod.write_registry(items)
    return entry


def build_demo_library() -> dict:
    """在「当前生效数据目录」里构建虚构演示库（等价 run.py demo 的数据部分，不起服务）。
    调用方需先 set_active_profile(DEMO_ID)。"""
    import analyze_pipeline
    from phase1_ingest import ingest

    sample = cfg_mod.root() / "sample_data" / "chat.sample.jsonl"
    if not sample.is_file():
        raise FileNotFoundError("缺少 sample_data/chat.sample.jsonl")
    cfg_mod.db_path().parent.mkdir(parents=True, exist_ok=True)
    summary = ingest(sample, {"沈星然": "A", "林晚语": "B"}, db_path=cfg_mod.db_path())
    analysis = analyze_pipeline.run_all(skip_llm=True)

    pdir = cfg_mod.persona_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    for person in ("A", "B"):
        src = cfg_mod.root() / "sample_data" / f"persona_v1_{person}.json"
        if src.is_file():
            shutil.copy(src, pdir / f"persona_v1_{person}.json")
    return {"message_count": summary.get("message_count"),
            "first_day": summary.get("first_day"), "last_day": summary.get("last_day"),
            "events": analysis.get("events"), "turning_points": analysis.get("turning_points")}


# ---------------------------------------------------------------- 给 API 的状态
def status() -> dict:
    items = cfg_mod.read_registry()
    active = cfg_mod.active_profile()
    out: list[dict] = []
    for it in items:
        d = cfg_mod.profile_data_dir(it.get("id"))
        out.append({
            "id": it.get("id"), "name": it.get("name") or it.get("id"),
            "created": it.get("created", ""), "last_active": it.get("last_active", ""),
            "demo": bool(it.get("demo")),
            "dir": _rel(d), "active": it.get("id") == active,
            "exists": bool(d and d.exists()),
            "has_db": bool(d and (d / "ifwe_v1.db").is_file()),
        })
    # 没有启用任何好友（自定义数据目录 / demo，或注册表尚未生效）：
    # 把「当前数据目录」作为一行显式列出来，界面不至于空白或误报
    if not active or not any(p["active"] for p in out):
        d = cfg_mod.data_dir()
        out.insert(0, {"id": "", "name": "当前数据目录", "created": "", "last_active": "",
                       "synthetic": True, "dir": _rel(d), "active": True,
                       "exists": d.exists(), "has_db": (d / "ifwe_v1.db").is_file()})
    return {"profiles": out, "active": active,
            "registry": _rel(cfg_mod.profiles_registry_path()),
            "base": _rel(cfg_mod.base_data_dir())}
