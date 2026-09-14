# -*- coding: utf-8 -*-
"""已导入来源登记与源存档（数据规范 v2 · 多源合并）。

每个好友的数据目录下（`config.data_dir()`，data/ 已 gitignore）：

    sources/<sha256 前16位>.<ext>     源文件存档（按内容哈希命名 → 天然去重）
    sources.json                      登记表

语义：**库永远是「全部已登记来源」重建出来的结果**。
追加一份新来源 = 登记进 sources.json → 从全部来源整体重建一次库。
好处：语义干净（同源同参 → 结果确定）、可移除、可替换、不用每次重传旧文件。

为什么留档：用户的实际场景是「不同应用在不同时间各自导出」，不重传就得留档。
源文件只存本机 data/ 下，界面可查看/移除；风险与口径写在 docs/IMPORT.md。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import config as cfg_mod

ARCHIVE_DIRNAME = "sources"
REGISTRY_NAME = "sources.json"


# ---------------------------------------------------------------- 路径
def archive_dir() -> Path:
    """随当前好友切换（data_dir() 已按 profile 隔离）"""
    return cfg_mod.data_dir() / ARCHIVE_DIRNAME


def registry_path() -> Path:
    return cfg_mod.data_dir() / REGISTRY_NAME


def _rel(p: Path) -> str:
    """相对当前数据目录的路径（绝不把绝对路径写进登记表 / 回传界面）"""
    try:
        return Path(p).resolve().relative_to(cfg_mod.data_dir().resolve()).as_posix()
    except Exception:
        return Path(p).name


# ---------------------------------------------------------------- 登记表读写
def load() -> list[dict]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []


def save(items: list[dict]) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def get(source_id: str) -> dict | None:
    for it in load():
        if it.get("source_id") == source_id:
            return it
    return None


def set_sender_map(source_id: str, sender_map: dict[str, str]) -> bool:
    items = load()
    hit = False
    for it in items:
        if it.get("source_id") == source_id:
            it["sender_map"] = dict(sender_map or {})
            hit = True
    if hit:
        save(items)
    return hit


# ---------------------------------------------------------------- 增 / 删
def add_source(tmp_file: Path, name: str, importer: str,
               sender_map: dict[str, str], message_count: int = 0,
               first_ts: int | None = None, last_ts: int | None = None) -> dict:
    """把一份源文件存档并登记（同内容重复导入 → 复用同一条登记，只更新映射与统计）。

    存档走 copy2（源来自本机 tmp_import，调用方负责清理临时文件）。
    """
    from phase1_ingest import file_sha256, source_id_for

    tmp_file = Path(tmp_file)
    sha = file_sha256(tmp_file)
    sid = source_id_for(tmp_file)
    ext = tmp_file.suffix.lower() or ".bin"
    d = archive_dir()
    d.mkdir(parents=True, exist_ok=True)
    archive_name = f"{sid[4:]}{ext}"
    dest = d / archive_name
    if not dest.is_file() or file_sha256(dest) != sha:
        shutil.copy2(tmp_file, dest)

    items = load()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = None
    for it in items:
        if it.get("source_id") == sid:
            entry = it
    if entry is None:
        entry = {"source_id": sid, "added_at": now}
        items.append(entry)
    entry.update({
        "name": Path(name or tmp_file.name).name,
        "importer": importer or "",
        "archive": _rel(dest),
        "sha256": sha,
        "size": tmp_file.stat().st_size,
        "sender_map": dict(sender_map or {}),
        "message_count": int(message_count or 0),
        "first_ts": first_ts, "last_ts": last_ts,
        "last_import_at": now,
    })
    save(items)
    return entry


def remove_source(source_id: str) -> dict:
    """移除一条来源登记 + 删掉其存档（逐文件 unlink，规避本机 safe-delete 目录级删除）。"""
    items = load()
    hit = None
    for it in items:
        if it.get("source_id") == source_id:
            hit = it
    if hit is None:
        raise KeyError(source_id)
    p = cfg_mod.data_dir() / (hit.get("archive") or "")
    err = ""
    if hit.get("archive") and p.is_file():
        try:
            p.unlink()
        except OSError as e:
            err = str(e)
    save([it for it in items if it.get("source_id") != source_id])
    return {"ok": not err, "removed": hit.get("name") or source_id, "error": err}


def clear() -> None:
    """清空登记与存档（删好友/重建库时用；逐文件 unlink）"""
    d = archive_dir()
    if d.is_dir():
        for p in d.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        registry_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 出参
def build_source_specs() -> list[dict]:
    """构造 ingest_many() 的输入：全部已登记且存档仍在的来源。

    存档缺失（被手动删掉）的来源会跳过，并在返回值第二项里报出来。
    """
    d = cfg_mod.data_dir()
    specs: list[dict] = []
    missing: list[str] = []
    for it in load():
        p = d / (it.get("archive") or "")
        if not it.get("archive") or not p.is_file():
            missing.append(it.get("name") or it.get("source_id") or "?")
            continue
        specs.append({
            "path": p,
            "sender_map": it.get("sender_map") or {},
            "source_id": it.get("source_id"),
            "name": it.get("name") or p.name,
            "importer": None,       # 让 registry 按内容自动探测（存档扩展名已保留）
            "sha256": it.get("sha256"),
        })
    return specs, missing


def list_view() -> list[dict]:
    """给界面看的登记清单（不含绝对路径）。"""
    out = []
    d = cfg_mod.data_dir()
    for it in load():
        p = d / (it.get("archive") or "")
        out.append({
            "source_id": it.get("source_id"),
            "name": it.get("name") or "",
            "importer": it.get("importer") or "",
            "message_count": it.get("message_count") or 0,
            "sender_map": it.get("sender_map") or {},
            "size": it.get("size") or 0,
            "added_at": it.get("added_at") or "",
            "last_import_at": it.get("last_import_at") or "",
            "first_ts": it.get("first_ts"), "last_ts": it.get("last_ts"),
            "archive_exists": bool(it.get("archive") and p.is_file()),
        })
    return out
