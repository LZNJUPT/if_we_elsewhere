-- IfWe · IF 分支树元数据扩展 schema（只加表，不破 v1 / 各 Phase 已有表）
-- 蓝图: docs/ARCHITECTURE.md 分支隔离章节
-- 约定:
--   - 每个 IF 分支 = 一个 sim_id 的续演运行（数据隔离在 sim_* 命名空间）
--   - ifr_branch 只存分支树的「元数据/父子关系/改写标签」，不含任何对话/事件内容
--   - 分支删除 = 按 sim_id 清 sim_* 各表 + 删 ifr_branch 行

-- IF 分支树节点（一条续演分支）
CREATE TABLE IF NOT EXISTS ifr_branch (
    branch_id           TEXT PRIMARY KEY,       -- DIAL-xxx / IF-B / IF-C …
    sim_id              TEXT NOT NULL UNIQUE,   -- 关联 sim_runs
    parent_branch_id    TEXT DEFAULT 'main',    -- 父时间线（真实历史=main）
    divergence_point    TEXT,                   -- 决策点 YYYY-MM-DD
    rewritten_choice    TEXT,                   -- 该分支的改写选择（用户可读）
    divergence_desc     TEXT,                   -- 改写内容描述
    start_day           TEXT NOT NULL,
    end_day             TEXT,
    n_days              INTEGER,
    max_turns           INTEGER,
    pcc_mode            TEXT,
    clock_seed          INTEGER,
    status              TEXT DEFAULT 'running', -- running / done / aborted
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ifr_parent ON ifr_branch(parent_branch_id);
CREATE INDEX IF NOT EXISTS idx_ifr_status ON ifr_branch(status);
