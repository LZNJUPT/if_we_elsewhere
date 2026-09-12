-- IfWe Phase 2 关系分析 扩展 schema（只加表，不破 v1）
-- 数据规范 v1 见 docs/IMPORT.md
-- 应用方式: 由 run.py analyze 幂等执行(CREATE TABLE IF NOT EXISTS)

-- M3 会话级情绪（snownlp 本地打分聚合）
CREATE TABLE IF NOT EXISTS session_emotion (
    session_id TEXT PRIMARY KEY,
    day        TEXT,
    n_text     INTEGER DEFAULT 0,   -- 参与情绪打分的文本消息数
    score_a    REAL,                -- A 均分 ∈ [-1,1]
    score_b    REAL,                -- B 均分 ∈ [-1,1]
    score      REAL,                -- 会话整体均分
    pos_a      INTEGER DEFAULT 0,   -- A 正向(>0.15)消息数
    neg_a      INTEGER DEFAULT 0,   -- A 负向(<-0.15)消息数
    pos_b      INTEGER DEFAULT 0,
    neg_b      INTEGER DEFAULT 0
);

-- M3 按天情绪聚合
CREATE TABLE IF NOT EXISTS daily_emotion (
    day          TEXT PRIMARY KEY,
    n_text       INTEGER DEFAULT 0,
    score_a      REAL,
    score_b      REAL,
    score        REAL,
    pos_ratio_a  REAL,              -- A 正向消息占比
    pos_ratio_b  REAL
);

-- M4 实体（跨会话聚簇）
CREATE TABLE IF NOT EXISTS entities (
    entity_id     TEXT PRIMARY KEY, -- E{seq}
    entity_name   TEXT,
    category      TEXT,             -- person/pet/place/study/work/family/leisure/other
    mention_count INTEGER DEFAULT 0,
    first_day     TEXT,
    last_day      TEXT,
    monthly       TEXT              -- JSON: {YYYY-MM: n}
);

-- M4 断点续跑检查点
CREATE TABLE IF NOT EXISTS m4_checkpoint (
    session_id   TEXT PRIMARY KEY,
    status       TEXT,              -- done / no_text / failed
    attempts     INTEGER DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    note         TEXT
);

-- M5 转折点
CREATE TABLE IF NOT EXISTS turning_points (
    tp_id       TEXT PRIMARY KEY,   -- TP{seq}
    day         TEXT,
    date_range  TEXT,               -- "YYYY-MM-DD~YYYY-MM-DD"
    tp_type     TEXT,               -- activity_gap/activity_anomaly/emotion/event/other
    title       TEXT,
    description TEXT,
    before_after TEXT,              -- JSON 前后对比证据
    evidence    TEXT                -- 事件/消息引用
);

-- M7 关系状态维度（按月）
CREATE TABLE IF NOT EXISTS relationship_state (
    period          TEXT PRIMARY KEY, -- YYYY-MM
    closeness       REAL,             -- 亲密度
    conflict        REAL,             -- 冲突指数(高=多冲突)
    trust           REAL,             -- 信任
    emotional_safety REAL,            -- 情绪安全
    comm_quality    REAL,             -- 沟通质量
    confidence      REAL,             -- 置信度
    calibrated      INTEGER DEFAULT 0 -- 用户校准标记
);