-- IfWe 数据规范 v2 (SQLite DDL) —— 增量部分
-- 字段说明见 docs/IMPORT.md；应用方式见 phase1_ingest.ensure_v2_schema()
--
-- v1 → v2 变更：
--   1. messages 表增两列（由 ensure_v2_schema() 幂等 ALTER，不在本文件里）：
--        source_id  来源登记 id（import_sources.source_id）
--        dedup_key  跨源去重键（ts|发送者|内容 的 sha1；无确定性标识时为 NULL）
--   2. 新增 import_sources：已导入来源登记（源存档 + 逐源 A/B 映射 + 条数/跨度）
--   3. 新增 media / message_media：媒体（表情包、图片）独立导入通道的索引与关联
--
-- 本文件只含「新表」，不触碰 messages —— 旧库由 ensure_v2_schema() 补列后再建索引，
-- 避免 CREATE INDEX 早于 ALTER 导致的老库升级失败。

PRAGMA journal_mode = WAL;

-- 已导入来源登记：每次导入 = 从「全部已登记来源」重建一次库（幂等）
CREATE TABLE IF NOT EXISTS import_sources (
    source_id      TEXT PRIMARY KEY,   -- 稳定 id：src-<源文件 sha256 前 16 位>
    name           TEXT,               -- 原始文件名（仅本机展示）
    importer       TEXT,               -- 格式标识 chatlab/wecomsg/telegram/plaintext/docx
    archive_path   TEXT,               -- 源存档相对 data_dir 的路径（sources/<hash>.<ext>）
    sha256         TEXT,               -- 源文件内容哈希（去重与稳定 id 的依据）
    size_bytes     INTEGER,
    sender_map     TEXT,               -- JSON {原始账号名: "A"/"B"}（逐源映射）
    message_count  INTEGER,            -- 最近一次解析出的有效消息数
    first_ts       INTEGER,
    last_ts        INTEGER,
    added_at       TEXT,               -- 首次登记时间（ISO）
    last_import_at TEXT                -- 最近参与重建的时间（ISO）
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_sha ON import_sources(sha256);

-- 媒体索引（表情包 / 图片）：独立于聊天记录导入，按内容哈希去重
CREATE TABLE IF NOT EXISTS media (
    media_id    TEXT PRIMARY KEY,      -- sha256 前 32 位
    sha256      TEXT NOT NULL,         -- 完整内容哈希
    filename    TEXT,                  -- 导入时的原始文件名
    kind        TEXT NOT NULL,         -- sticker=表情包 / image=图片
    ext         TEXT,                  -- 小写扩展名（含点）
    mime        TEXT,
    size_bytes  INTEGER,
    width       INTEGER,               -- 图片尺寸（能读到时填，否则 NULL）
    height      INTEGER,
    rel_path    TEXT NOT NULL,         -- 相对当前好友 data_dir 的存储路径
    added_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_sha  ON media(sha256);
CREATE INDEX        IF NOT EXISTS idx_media_kind ON media(kind);

-- 消息 ↔ 媒体关联：靠文件名/哈希匹配，匹配不上就留空（绝不猜测归类）
CREATE TABLE IF NOT EXISTS message_media (
    message_id TEXT NOT NULL,
    media_id   TEXT NOT NULL,
    link_by    TEXT,                   -- filename / sha256
    PRIMARY KEY (message_id, media_id)
);
CREATE INDEX IF NOT EXISTS idx_msgmedia_media ON message_media(media_id);
