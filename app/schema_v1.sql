-- IfWe 数据规范 v1 (SQLite DDL)
-- 字段说明见 docs/IMPORT.md
-- 应用方式: 由 scripts/import_chat.py 在 --reset 时执行

PRAGMA journal_mode = WAL;

-- 元数据(规范版本/来源/门禁结果)
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Layer 1: 规范化消息(一行一消息, 脱敏/去噪后为 content_clean)
CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    ts              INTEGER NOT NULL,        -- 原始秒级时间戳
    ts_local        TEXT    NOT NULL,        -- 东八区 ISODate 可读
    day             TEXT    NOT NULL,        -- YYYY-MM-DD 按天聚合键
    sender_key      TEXT    NOT NULL,        -- A / B(映射见 meta.sender_map)
    sender_orig     TEXT,                    -- 原始昵称(仅本地, 对外输出用代号)
    type            INTEGER NOT NULL,        -- 0/4/7/23/24/25/27/80/99 保留原始编码
    subtype         TEXT,                    -- text/image/emoji_gif/file/call/miniprogram/quote_text/namecard/recall/transfer
    content_orig    TEXT,                    -- 原始文本(纯净副本, Layer0 只读)
    content_clean   TEXT,                    -- 去噪+脱敏后文本(分析/LLM 唯一输入)
    quoted_msg_id   TEXT,                    -- type=25 引用消息 ID(当前导出无, 预留)
    quoted_text     TEXT,                    -- type=25 引用内容(含来源昵称)
    amount          REAL,                    -- type=99 转账金额
    attachment_name TEXT,                    -- type=4/7/24 文件名/小程序标题
    is_system       INTEGER NOT NULL DEFAULT 0, -- 撤回等系统事件
    has_privacy     INTEGER NOT NULL DEFAULT 0  -- 命中隐私脱敏规则
);
CREATE INDEX IF NOT EXISTS idx_messages_ts     ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_day    ON messages(day);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_key);

-- Layer 2: 会话(30 分钟内相邻消息为一会话, 至少 2 条)
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    start_ts   INTEGER NOT NULL,
    end_ts     INTEGER NOT NULL,
    day        TEXT,
    msg_count  INTEGER NOT NULL,
    text_count INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    a_count    INTEGER NOT NULL DEFAULT 0,
    b_count    INTEGER NOT NULL DEFAULT 0,
    dominant   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);

-- Layer 2: 每日聚合
CREATE TABLE IF NOT EXISTS daily_stats (
    day            TEXT PRIMARY KEY,
    msg_count      INTEGER NOT NULL,
    text_count     INTEGER NOT NULL,
    image_count    INTEGER NOT NULL,
    recall_count   INTEGER NOT NULL,
    transfer_count INTEGER NOT NULL,
    session_count  INTEGER NOT NULL,
    char_count     INTEGER NOT NULL,
    a_count        INTEGER NOT NULL DEFAULT 0,
    b_count        INTEGER NOT NULL DEFAULT 0
);

-- 事件表(Phase 2 填充; v1 仅建表锁定 schema)
CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    day                 TEXT,
    start_ts            INTEGER,
    end_ts              INTEGER,
    event_type          TEXT NOT NULL,
    summary             TEXT,
    severity            INTEGER,
    evidence_message_ids TEXT,
    created_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);