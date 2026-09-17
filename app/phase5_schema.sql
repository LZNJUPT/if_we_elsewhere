-- IfWe Phase 5 Agent & Simulation v1 扩展 schema（只加表，不破 v1 / Phase2 / Phase4 已有表）
-- 蓝图: 《IfWe技术调研报告-第一轮.md》§12 Agent Model + §13 记忆架构 + §10.2/10.3 分支隔离
-- 约定: 模拟/续演产物一律落「独立分支命名空间」(sim_*) 表，绝不写入主 facts/events；
--       sim_facts 只存派生结论（LLM 生成/总结），不含任何原文；双时态 valid_at/invalid_at
-- 应用方式: phase5_a1_world.py 幂等执行

-- 一次模拟/续演运行（一条可续演时间线 = 一个 sim_id）
CREATE TABLE IF NOT EXISTS sim_runs (
    sim_id            TEXT PRIMARY KEY,        -- SIM-YYYYMMDD-HHMMSS
    branch_name       TEXT NOT NULL,           -- 用户可读分支标签（如 2026-07复合改写）
    branch_of         TEXT DEFAULT 'main',     -- 父时间线
    divergence_point  TEXT,                    -- 反事实改写点 YYYY-MM-DD
    divergence_desc   TEXT,                    -- 改写内容描述（不含原文）
    rewritten_choice  TEXT,                    -- 用户改写后的选择
    start_day         TEXT NOT NULL,           -- 模拟起点 YYYY-MM-DD
    end_day           TEXT,                    -- 模拟终点（NULL=运行中）
    n_rounds          INTEGER DEFAULT 0,
    clock_seed        INTEGER,                 -- 随机种子（可复现）
    config_json       TEXT,                    -- 超参数（模型/回合数/反思开关…）
    status            TEXT DEFAULT 'running',  -- running / done / aborted
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simrun_status ON sim_runs(status);

-- 模拟中的会话（= 一个连续对话块）
CREATE TABLE IF NOT EXISTS sim_sessions (
    session_id   TEXT PRIMARY KEY,             -- {sim_id}-S{seq}
    sim_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    day          TEXT NOT NULL,                -- 该会话发生日
    n_turns      INTEGER DEFAULT 0,
    initiator    TEXT,                         -- A / B
    topic        TEXT,                         -- LLM 一句话主题
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simsess_sim ON sim_sessions(sim_id, day);

-- 模拟对话消息（LLM 生成的派生文本，不对接任何原文）
CREATE TABLE IF NOT EXISTS sim_messages (
    msg_id     TEXT PRIMARY KEY,               -- {session_id}-T{turn}
    sim_id     TEXT NOT NULL,
    session_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    turn_idx   INTEGER NOT NULL,
    sender     TEXT NOT NULL,                  -- A / B
    content    TEXT NOT NULL,                  -- 生成回复/行动文本
    emotion_s  TEXT,                           -- 该 Agent 当前 S 层情绪标签
    meta       TEXT,                           -- 附加 JSON（行动/notice/检索来源）
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simmsg_sim ON sim_messages(sim_id);
CREATE INDEX IF NOT EXISTS idx_simmsg_sess ON sim_messages(session_id);

-- 模拟会话抽取的事件（结果隔离；事件本体同 Phase2）
CREATE TABLE IF NOT EXISTS sim_events (
    event_id       TEXT PRIMARY KEY,           -- {sim_id}-E{seq}
    sim_id         TEXT NOT NULL,
    day            TEXT NOT NULL,
    event_type     TEXT,
    summary        TEXT NOT NULL,              -- <=30字 派生摘要，不含隐私
    severity       INTEGER DEFAULT 0,          -- 1-5（同 Phase2 口径）
    importance     INTEGER DEFAULT 0,          -- 1-5
    source_session TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simevt_sim ON sim_events(sim_id, day);

-- 模拟分支记忆（memory_type 同 facts 四类；source 注明生成通道）
CREATE TABLE IF NOT EXISTS sim_facts (
    fact_id         TEXT PRIMARY KEY,          -- {sim_id}-F{seq}
    sim_id          TEXT NOT NULL,
    memory_type     TEXT NOT NULL,             -- episodic / semantic / procedural / state
    subject         TEXT NOT NULL,
    relation        TEXT,
    object          TEXT,
    conclusion_text TEXT NOT NULL,             -- 派生结论，不含原文
    valid_at        TEXT,
    invalid_at      TEXT,
    source          TEXT NOT NULL,             -- simulation_event / simulation_smm / simulation_agent / simulation_rel
    source_id       TEXT,
    confidence      REAL DEFAULT 0.5,
    keywords        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simfact_sim ON sim_facts(sim_id);
CREATE INDEX IF NOT EXISTS idx_simfact_type ON sim_facts(memory_type);

-- A5 防人格漂移：反思 / 校准 / 规划日志
CREATE TABLE IF NOT EXISTS sim_pcc_log (
    log_id      TEXT PRIMARY KEY,
    sim_id      TEXT NOT NULL,
    day         TEXT,
    kind        TEXT NOT NULL,                 -- reflection / calibration / plan / check
    target      TEXT,                          -- A / B / both
    content     TEXT NOT NULL,                 -- 反思结论/校准指令/日规划（派生）
    deviations  TEXT,                          -- JSON 偏差清单 [{layer, item, deviation}]
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simpcc_sim ON sim_pcc_log(sim_id);

-- 分支内关系状态估计（Phase 6 关系引擎前的最小估计量，明确标注 estimate）
CREATE TABLE IF NOT EXISTS sim_rel_state (
    sim_id          TEXT NOT NULL,
    day             TEXT NOT NULL,
    closeness       REAL,
    conflict        REAL,
    trust           REAL,
    emotional_safety REAL,
    comm_quality    REAL,
    confidence      REAL DEFAULT 0.4,
    method          TEXT DEFAULT 'heuristic',  -- heuristic / llm
    notes           TEXT,
    PRIMARY KEY (sim_id, day)
);

-- A3 短期/工作记忆缓冲快照（窗口摘要压缩的轨迹留档）
CREATE TABLE IF NOT EXISTS sim_working_mem (
    wm_id      TEXT PRIMARY KEY,
    sim_id     TEXT NOT NULL,
    day        TEXT,
    kind       TEXT NOT NULL,                  -- buffer_snapshot / compressed_summary
    content    TEXT NOT NULL,                  -- 当前窗口摘要/压缩结果（派生）
    meta       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simwm_sim ON sim_working_mem(sim_id);
-- ============================================================
-- 决策观测日志（二期 Phase A · §11：观测与路径一致性，只加法不碰旧表）
-- 与私有树 pipeline/phase5_schema.sql 尾部定义同名同源（勿单边改动）。
-- ============================================================
CREATE TABLE IF NOT EXISTS sim_decision_log (
    log_id         TEXT PRIMARY KEY,
    sim_id         TEXT NOT NULL,
    session_id     TEXT,
    day            TEXT,
    turn_idx       INTEGER,
    path           TEXT NOT NULL,
    decided        INTEGER NOT NULL,
    p_reply        REAL,
    layer          TEXT,
    samples        INTEGER,
    n_branch       INTEGER,
    p_real_only    REAL,
    sfb_flag       INTEGER DEFAULT 0,
    anchor_day     TEXT,
    m              REAL,
    reply_mode     TEXT,
    silent_reason  TEXT,
    stance_version TEXT,
    evidence_ids   TEXT,
    prompt_digest  TEXT,
    state_before   TEXT,
    state_after    TEXT,
    source         TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simdec_sim ON sim_decision_log(sim_id, day, turn_idx);

-- ============================================================
-- 分支状态持久化（二期 Phase B · §5.10；与私有树 pipeline/phase5_schema.sql 同名同源）
-- ============================================================
CREATE TABLE IF NOT EXISTS sim_branch_state (
  sim_id TEXT PRIMARY KEY, rel_current TEXT, s_state TEXT, scene_state TEXT,
  stance TEXT, calib TEXT, recent_texts TEXT, decision_tail TEXT,
  version INTEGER DEFAULT 1, updated_at TEXT
);

-- ============================================================
-- sim_messages 对话时间字段（二期 Phase B · §5.9 · 加法迁移）
-- 幂等迁移由 phase5_common.ensure_time_columns() 负责（DialEngine.__init__ 调用）。
--   ALTER TABLE sim_messages ADD COLUMN sent_at     TEXT;
--   ALTER TABLE sim_messages ADD COLUMN time_source TEXT;
-- 排序键统一 (day, turn_idx, rowid)；created_at 只作审计。
-- ============================================================


-- ============================================================
-- 行动决策日志（二期 Phase C · §6.1E；与私有树 pipeline/phase5_schema.sql 同名同源）
-- ============================================================
CREATE TABLE IF NOT EXISTS sim_action_log (
  action_id TEXT PRIMARY KEY, sim_id TEXT NOT NULL, session_id TEXT,
  day TEXT, turn_idx INTEGER, path TEXT,
  reply_mode TEXT, should_generate INTEGER, p_reply REAL,
  scene_mode TEXT, boundary_pressure TEXT,
  event_type TEXT, rel_delta TEXT,
  must_not_do TEXT, decision_reason TEXT,
  generation_violation INTEGER DEFAULT 0, violation_note TEXT,
  model_version TEXT, evidence_ids TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_simact_sim ON sim_action_log(sim_id, day, turn_idx);
