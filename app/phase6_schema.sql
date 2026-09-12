-- IfWe Phase 6 Relationship Engine v1 扩展 schema（只加表，不破 v1 / Phase2/4/5 已有表）
-- 蓝图: 《IfWe技术调研报告-第一轮.md》§9 三元结构 + §16 Phase 6
-- 约定（R3 用户拍板: 只读+独立结果表）:
--   - 主库 relationship_state 44 期 calibrated 真值【只读不写】
--   - 引擎产出的快照/预测/贡献一律落本文件新表（rel_engine_*），可随时删除重建
--   - 续演分支的引擎状态仍写 sim_rel_state（method='engine'），不并入主库
-- 应用方式: phase6 各脚本幂等执行 under project root

-- 一次引擎运行（主库回放 / 分支回放 / 评测预测）
CREATE TABLE IF NOT EXISTS rel_engine_runs (
    run_id          TEXT PRIMARY KEY,       -- main-replay / sim-replay-{sim_id} / eval-one-step
    scope           TEXT NOT NULL,          -- main / sim / eval
    label           TEXT,                   -- 可读标签（如 主库44期回放）
    method          TEXT DEFAULT 'engine_v1',
    source_baseline TEXT,                   -- steady 基线来源说明
    config_json     TEXT,                   -- 超参数快照（echo_decay/alpha/compress…）
    created_at      TEXT NOT NULL
);

-- 引擎快照序列（R 快照）：R 稳态/动态合成的 engine_state，按 period/day
CREATE TABLE IF NOT EXISTS rel_engine_state (
    run_id          TEXT NOT NULL,
    period          TEXT NOT NULL,          -- 'YYYY-MM'（主库）或日（分支）
    closeness       REAL,
    conflict        REAL,
    trust           REAL,
    emotional_safety REAL,
    comm_quality    REAL,
    dyn_delta       TEXT,                   -- JSON {dim:月净增量}（R 动态）
    notes           TEXT,
    PRIMARY KEY (run_id, period)
);
CREATE INDEX IF NOT EXISTS idx_reng_state ON rel_engine_state(run_id);

-- 周期事件贡献（可解释链来源：R5 拐点标注 / R6 抽检 / R7 判定链）
CREATE TABLE IF NOT EXISTS rel_engine_contrib (
    run_id          TEXT NOT NULL,
    period          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    event_id        TEXT,
    day             TEXT,
    event_type      TEXT,
    summary         TEXT,                   -- 派生摘要，不含隐私
    severity        INTEGER,
    importance      INTEGER,
    deltas          TEXT,                   -- JSON {dim:delta}
    reason          TEXT,                   -- 解释链文本（为什么）
    PRIMARY KEY (run_id, period, seq)
);
CREATE INDEX IF NOT EXISTS idx_reng_contrib ON rel_engine_contrib(run_id, period);