-- IfWe Phase 4 记忆引擎 v1 扩展 schema（只加表，不破 v1 / Phase2 已有表）
-- 蓝图: docs/ARCHITECTURE.md 记忆架构章节
-- 应用方式: 由 run.py analyze 幂等执行(CREATE TABLE IF NOT EXISTS)
-- 记忆层红线: 只存 content_clean 派生结论，不含原文；涉及时效的事实带 valid_at/invalid_at 双时态

-- 双时态事实表（Memory v1 核心）
-- memory_type 四类(单一表 + 类型列，简单优先，理由见 M1 报告):
--   episodic   = 情景记忆（events 派生，具体事件）
--   semantic   = 语义记忆（entities / persona 稳定特征 / 用户校准真值）
--   procedural = 程序性记忆（persona S 层应对套路："吵架后她通常先…"）
--   state      = 关系状态（relationship_state 按月快照）
CREATE TABLE IF NOT EXISTS facts (
    fact_id         TEXT PRIMARY KEY,           -- F{seq}
    memory_type     TEXT NOT NULL,              -- episodic / semantic / procedural / state
    subject         TEXT NOT NULL,              -- 主体（人物代号/entity/relationship/persona）
    relation        TEXT NOT NULL,              -- 谓词（event_type / 层L|M|S|U / monthly_state / category…）
    object          TEXT,                       -- 客体
    conclusion_text TEXT NOT NULL,              -- 结论文本（派生结论，不含原文）
    valid_at        TEXT,                       -- YYYY-MM-DD 生效起始
    invalid_at      TEXT,                       -- YYYY-MM-DD 失效(NULL=当前仍有效)
    source          TEXT NOT NULL,              -- events / entities / relationship_state / persona_v1_A.json…
    source_id       TEXT NOT NULL,              -- 事件id / 实体id / YYYY-MM / JSON层-序号
    confidence      REAL NOT NULL DEFAULT 0.5,  -- 0-1 证据置信度(派生)
    strength        REAL NOT NULL DEFAULT 1.0,  -- M5 记忆强度(可衰减/强化)
    last_access     TEXT,                       -- M5 最近访问时间
    access_count    INTEGER NOT NULL DEFAULT 0, -- M5 访问次数
    keywords        TEXT,                       -- 检索用词(空格分隔字符 bigram + 实体名，供纯 SQL 召回)
    review_status   TEXT NOT NULL DEFAULT 'pending', -- pending / llm_checked / user_checked
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_source  ON facts(source);
CREATE INDEX IF NOT EXISTS idx_facts_type    ON facts(memory_type);
CREATE INDEX IF NOT EXISTS idx_facts_valid   ON facts(valid_at);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);

-- 时间戳化 Persona 快照时间线（承接 Phase 3 已产出的 8 阶段快照概念）
CREATE TABLE IF NOT EXISTS persona_snapshot (
    snapshot_id   TEXT PRIMARY KEY,             -- SA{seq} / SB{seq}
    person        TEXT NOT NULL,                -- A / B
    granularity   TEXT NOT NULL DEFAULT 'stage',-- stage / month
    stage_label   TEXT NOT NULL,                -- 阶段名
    date_range    TEXT,                         -- YYYY-MM-DD~YYYY-MM-DD
    as_of         TEXT,                         -- 快照代表日期(阶段起始)
    n_events      INTEGER,
    rel_state_avg TEXT,                         -- JSON {closeness,conflict,trust,emotional_safety,comm_quality}
    top_event     TEXT,
    source_ref    TEXT NOT NULL,                -- persona_v1_A.json / persona_v1_B.json
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_psn_person ON persona_snapshot(person);
CREATE INDEX IF NOT EXISTS idx_psn_asof   ON persona_snapshot(as_of);

-- 向量召回（M4 可选增强，用户已拍板启用）：fastembed bge-small-zh-v1.5 本地 512 维
-- vec 存 float32 numpy 数组的二进制（供余弦相似度）
CREATE TABLE IF NOT EXISTS fact_embedding (
    fact_id     TEXT PRIMARY KEY,
    vec         BLOB,                     -- float32 数组字节
    dim         INTEGER,
    model       TEXT,                     -- bge-small-zh-v1.5
    created_at  TEXT
);
