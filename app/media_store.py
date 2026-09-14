# -*- coding: utf-8 -*-
"""媒体存档与索引（数据规范 v2）—— 表情包 / 图片的**独立导入通道**。

为什么独立：聊天记录里的表情包只是一个文件名，真正的图片文件在另一次导出里、
甚至散落在用户自己收集的文件夹里。所以媒体必须能单独导入，再与消息关联。

存储（随当前好友隔离）：
    data/profiles/<id>/media/<sha256 前16位><ext>   文件存档（按内容哈希去重）
    media 表                                         索引
    message_media 表                                 与消息的关联

关联策略：拿消息的 attachment_name（文件名）与 media.filename 精确匹配，
匹配不上就**留空**，绝不猜测归类。同时兼容旧的 `config media.emojis_dir`（32 位 hex 命名）。

⚠️ 红线：**媒体不做脱敏、也不会被送进 LLM**。聊天文字会被替换掉手机号/地址，
但一张身份证截图、一张快递单照片导入后就是原样躺在 data/ 里。口径见 docs/IMPORT.md。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import struct
import time
from pathlib import Path

import config as cfg_mod

MEDIA_DIRNAME = "media"
# 允许的图片扩展名（avi/webp 等交给浏览器解码，MIME 由 api 侧嗅探）
ALLOWED_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".jfif"}
# 判为「表情包」的扩展名（其余算「图片」）
STICKER_EXTS = {".gif", ".webp"}
# 文件名安全：不含路径分隔符与控制字符（索引表里的路径由我们自己生成，此校验只用于入参）
MAX_NAME_LEN = 120
MAX_BYTES = 20 * 1024 * 1024


def media_dir() -> Path:
    return cfg_mod.data_dir() / MEDIA_DIRNAME


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _rel(p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(cfg_mod.data_dir().resolve()).as_posix()
    except Exception:
        return Path(p).name


def safe_name(name: str) -> str:
    """外部传入的文件名净化：只取 basename，挡掉路径分隔与控制字符。"""
    n = os.path.basename((name or "").replace("\\", "/")).strip()
    n = "".join(ch for ch in n if ch.isprintable() and ch not in "\r\n\t")
    return n[:MAX_NAME_LEN]


def guess_kind(filename: str, kind: str = "auto") -> str:
    if kind in ("sticker", "image"):
        return kind
    return "sticker" if Path(filename).suffix.lower() in STICKER_EXTS else "image"


def mime_of(path: Path) -> str:
    ext = Path(path).suffix.lower()
    return {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp",
            ".jfif": "image/jpeg"}.get(ext, "application/octet-stream")


# ---------------------------------------------------------------- 图片尺寸（零依赖）
def image_size(path: Path) -> tuple[int | None, int | None]:
    """读 PNG / GIF / JPEG 的宽高；读不出返回 (None, None)（不阻塞导入）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
            if len(head) < 10:
                return None, None
            if head[:8] == b"\x89PNG\r\n\x1a\n":                      # PNG: IHDR
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):                    # GIF: 逻辑屏幕
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:2] == b"\xff\xd8":                               # JPEG: 扫 SOFn
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        return None, None
                    if marker[0] in (0xD8, 0xD9) or 0xD0 <= marker[0] <= 0xD7:
                        continue
                    seg = f.read(2)
                    if len(seg) < 2:
                        return None, None
                    ln = struct.unpack(">H", seg)[0]
                    if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        data = f.read(5)
                        if len(data) < 5:
                            return None, None
                        h, w = struct.unpack(">HH", data[1:5])
                        return int(w), int(h)
                    f.seek(ln - 2, 1)
    except Exception:
        return None, None
    return None, None


# ---------------------------------------------------------------- 导入
def import_file(src: Path, kind: str = "auto", original_name: str | None = None) -> dict:
    """把一张图片存档并按内容哈希登记进 media 表（重复内容只登记一次）。

    返回 {"ok", "media_id", "filename", "kind", "dedup", "reason"?}
    """
    from phase1_ingest import file_sha256

    src = Path(src)
    name = safe_name(original_name or src.name)
    ext = Path(name).suffix.lower() or src.suffix.lower()
    if ext not in ALLOWED_EXTS:
        return {"ok": False, "reason": f"不支持的图片格式 {ext or '(无扩展名)'}",
                "filename": name}
    size = src.stat().st_size
    if size > MAX_BYTES:
        return {"ok": False, "reason": "单张图片超过 20MB", "filename": name}
    if size == 0:
        return {"ok": False, "reason": "文件为空", "filename": name}

    sha = file_sha256(src)
    mid = sha[:32]
    d = media_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{sha[:16]}{ext}"

    from phase5_common import connect          # 延迟导入，避免循环依赖
    conn = connect()
    try:
        conn.executescript(_MEDIA_DDL)
        row = conn.execute("SELECT media_id FROM media WHERE sha256=?", (sha,)).fetchone()
        if row:
            return {"ok": True, "media_id": row[0], "filename": name,
                    "kind": guess_kind(name, kind), "dedup": True}
        if not dest.is_file():
            shutil.copy2(src, dest)
        w, h = image_size(dest)
        conn.execute(
            "INSERT OR REPLACE INTO media (media_id,sha256,filename,kind,ext,mime,"
            "size_bytes,width,height,rel_path,added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mid, sha, name, guess_kind(name, kind), ext, mime_of(dest), size, w, h,
             _rel(dest), _now()))
        conn.commit()
        return {"ok": True, "media_id": mid, "filename": name,
                "kind": guess_kind(name, kind), "dedup": False,
                "width": w, "height": h}
    finally:
        conn.close()


_MEDIA_DDL = """
CREATE TABLE IF NOT EXISTS media (
    media_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, filename TEXT, kind TEXT NOT NULL,
    ext TEXT, mime TEXT, size_bytes INTEGER, width INTEGER, height INTEGER,
    rel_path TEXT NOT NULL, added_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_sha ON media(sha256);
CREATE INDEX IF NOT EXISTS idx_media_kind ON media(kind);
CREATE TABLE IF NOT EXISTS message_media (
    message_id TEXT NOT NULL, media_id TEXT NOT NULL, link_by TEXT,
    PRIMARY KEY (message_id, media_id));
CREATE INDEX IF NOT EXISTS idx_msgmedia_media ON message_media(media_id);
"""


def import_paths(paths: list[Path], kind: str = "auto") -> dict:
    """批量导入（目录会被展开一层，只收允许的图片扩展名）。"""
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files.extend(sorted(x for x in p.iterdir()
                                if x.is_file() and x.suffix.lower() in ALLOWED_EXTS))
        elif p.is_file():
            files.append(p)
    added, dedup, failed = [], [], []
    for f in files:
        r = import_file(f, kind=kind)
        if not r.get("ok"):
            failed.append({"name": r.get("filename") or f.name, "reason": r.get("reason")})
        elif r.get("dedup"):
            dedup.append(r["filename"])
        else:
            added.append(r["filename"])
    return {"total": len(files), "added": added, "dedup": dedup, "failed": failed,
            "added_count": len(added), "dedup_count": len(dedup),
            "failed_count": len(failed)}


# ---------------------------------------------------------------- 查询 / 删除
def list_media() -> list[dict]:
    from phase5_common import connect
    conn = connect()
    try:
        conn.executescript(_MEDIA_DDL)
        rows = conn.execute(
            "SELECT media_id,filename,kind,ext,size_bytes,width,height,added_at "
            "FROM media ORDER BY added_at DESC, filename").fetchall()
        linked = dict(conn.execute(
            "SELECT media_id, COUNT(*) FROM message_media GROUP BY media_id").fetchall())
        return [{"media_id": r[0], "filename": r[1], "kind": r[2], "ext": r[3],
                 "size": r[4], "width": r[5], "height": r[6], "added_at": r[7],
                 "linked_messages": linked.get(r[0], 0)} for r in rows]
    finally:
        conn.close()


def delete(media_id: str) -> dict:
    from phase5_common import connect
    conn = connect()
    try:
        conn.executescript(_MEDIA_DDL)
        row = conn.execute("SELECT rel_path,filename FROM media WHERE media_id=?",
                           (media_id,)).fetchone()
        if not row:
            raise KeyError(media_id)
        conn.execute("DELETE FROM media WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM message_media WHERE media_id=?", (media_id,))
        conn.commit()
    finally:
        conn.close()
    p = cfg_mod.data_dir() / (row[0] or "")
    err = ""
    if row[0] and p.is_file():
        try:
            p.unlink()
        except OSError as e:
            err = str(e)
    return {"ok": not err, "removed": row[1] or media_id, "error": err}


def resolve(name: str) -> Path | None:
    """把「文件名」或「media_id」解析成本地文件路径（只走索引表，天然无路径穿越）。"""
    from phase5_common import connect
    n = safe_name(name)
    if not n:
        return None
    conn = connect()
    try:
        conn.executescript(_MEDIA_DDL)
        row = conn.execute(
            "SELECT rel_path FROM media WHERE filename=? ORDER BY added_at DESC LIMIT 1",
            (n,)).fetchone()
        if row is None:
            row = conn.execute("SELECT rel_path FROM media WHERE media_id=?",
                               (n,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    p = cfg_mod.data_dir() / row[0]
    return p if p.is_file() else None


def link_messages() -> dict:
    """把消息的 attachment_name 与媒体文件名精确匹配后写入 message_media。

    只做**精确同名**匹配（含 media_id 前缀形式），匹配不上的留空不猜。
    返回 {linked, candidates, unmatched}，供导入结果页展示关联率。
    """
    from phase5_common import connect
    conn = connect()
    try:
        conn.executescript(_MEDIA_DDL)
        rows = conn.execute(
            "SELECT message_id, attachment_name FROM messages "
            "WHERE attachment_name IS NOT NULL AND attachment_name <> ''").fetchall()
        media = dict((f, mid) for mid, f in conn.execute(
            "SELECT media_id, filename FROM media WHERE filename IS NOT NULL").fetchall())
        pairs, unmatched = [], 0
        for message_id, att in rows:
            mid = media.get(safe_name(att))
            if mid:
                pairs.append((message_id, mid, "filename"))
            else:
                unmatched += 1
        # 清掉指向已不存在消息的旧关联（库被重建后 message_id 可能整批变化）
        conn.execute("DELETE FROM message_media WHERE message_id NOT IN "
                     "(SELECT message_id FROM messages)")
        conn.executemany("INSERT OR REPLACE INTO message_media (message_id,media_id,link_by) "
                         "VALUES (?,?,?)", pairs)
        conn.commit()
        return {"linked": len(pairs), "candidates": len(rows), "unmatched": unmatched}
    finally:
        conn.close()


def stats() -> dict:
    from phase5_common import connect
    conn = connect()
    try:
        conn.executescript(_MEDIA_DDL)
        n = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        k = dict(conn.execute("SELECT kind, COUNT(*) FROM media GROUP BY kind").fetchall())
        total = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM media").fetchone()[0]
        return {"count": n, "by_kind": k, "total_bytes": total,
                "needs_attention": sum(1 for m in list_media()
                                       if not m["linked_messages"])}
    finally:
        conn.close()
