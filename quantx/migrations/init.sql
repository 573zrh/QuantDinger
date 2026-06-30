-- QuantX PostgreSQL Schema 初始化
-- 首次启动 PostgreSQL 容器时自动执行（docker-entrypoint-initdb.d）
-- 所有语句使用 IF NOT EXISTS，确保幂等性

-- =============================================================================
-- 1. 用户表
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255),
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) DEFAULT 'user',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- 2. 策略表
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_strategies (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES qd_users(id),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    market VARCHAR(50) NOT NULL,          -- CNStock, HKStock, USStock, CNFutures
    symbol VARCHAR(100) NOT NULL,
    timeframe VARCHAR(10) DEFAULT '1D',
    status VARCHAR(20) DEFAULT 'draft',   -- draft, ready, running, stopped, error
    strategy_type VARCHAR(50) DEFAULT 'indicator',  -- indicator, script
    indicator_code TEXT,
    params JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- 3. 持仓表
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_positions (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER REFERENCES qd_strategies(id),
    symbol VARCHAR(100) NOT NULL,
    market VARCHAR(50) NOT NULL,
    side VARCHAR(10) NOT NULL,            -- long, short
    quantity DECIMAL(20,8) NOT NULL DEFAULT 0,
    avg_price DECIMAL(20,8) NOT NULL DEFAULT 0,
    unrealized_pnl DECIMAL(20,8) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'open',    -- open, closed
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP
);

-- =============================================================================
-- 4. 交易记录表
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_trades (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER REFERENCES qd_strategies(id),
    position_id INTEGER REFERENCES qd_positions(id),
    symbol VARCHAR(100) NOT NULL,
    market VARCHAR(50) NOT NULL,
    side VARCHAR(10) NOT NULL,            -- buy, sell
    quantity DECIMAL(20,8) NOT NULL,
    price DECIMAL(20,8) NOT NULL,
    fee DECIMAL(20,8) DEFAULT 0,
    pnl DECIMAL(20,8) DEFAULT 0,
    signal_type VARCHAR(50),
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- 5. 回测运行表
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_backtest_runs (
    id SERIAL PRIMARY KEY,
    strategy_id INTEGER REFERENCES qd_strategies(id),
    user_id INTEGER REFERENCES qd_users(id),
    market VARCHAR(50) NOT NULL,
    symbol VARCHAR(100) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    start_date TIMESTAMP NOT NULL,
    end_date TIMESTAMP NOT NULL,
    initial_capital DECIMAL(20,2) DEFAULT 100000,
    final_capital DECIMAL(20,2),
    total_return DECIMAL(10,4),
    max_drawdown DECIMAL(10,4),
    sharpe_ratio DECIMAL(10,4),
    win_rate DECIMAL(10,4),
    total_trades INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'pending',  -- pending, running, completed, failed
    params JSONB DEFAULT '{}',
    result JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- =============================================================================
-- 6. 回测交易记录
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_backtest_trades (
    id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES qd_backtest_runs(id) ON DELETE CASCADE,
    entry_time TIMESTAMP,
    exit_time TIMESTAMP,
    side VARCHAR(10),
    quantity DECIMAL(20,8),
    entry_price DECIMAL(20,8),
    exit_price DECIMAL(20,8),
    pnl DECIMAL(20,8),
    fee DECIMAL(20,8)
);

-- =============================================================================
-- 7. 回测权益曲线
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_backtest_equity (
    id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES qd_backtest_runs(id) ON DELETE CASCADE,
    time TIMESTAMP NOT NULL,
    equity DECIMAL(20,2) NOT NULL,
    drawdown DECIMAL(10,4)
);

-- =============================================================================
-- 8. Agent Token 表
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_agent_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES qd_users(id),
    token_hash VARCHAR(255) NOT NULL,
    name VARCHAR(200),
    scopes VARCHAR(100) DEFAULT 'R',      -- R,W,B,T 组合
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP
);

-- =============================================================================
-- 9. Agent 审计日志
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_agent_audit (
    id SERIAL PRIMARY KEY,
    token_id INTEGER REFERENCES qd_agent_tokens(id),
    action VARCHAR(100) NOT NULL,
    resource VARCHAR(200),
    details JSONB DEFAULT '{}',
    ip_address VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- 9b. Agent 异步任务
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_agent_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(40) NOT NULL UNIQUE,
    user_id INTEGER REFERENCES qd_users(id),
    agent_token_id INTEGER REFERENCES qd_agent_tokens(id),
    kind VARCHAR(40) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error TEXT,
    progress JSONB,
    idempotency_key VARCHAR(120),
    created_at TIMESTAMP DEFAULT NOW(),
    started_at TIMESTAMP,
    finished_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_user ON qd_agent_jobs(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_idem
    ON qd_agent_jobs(agent_token_id, kind, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- =============================================================================
-- 10. 自选列表
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_watchlist (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES qd_users(id),
    symbol VARCHAR(100) NOT NULL,
    market VARCHAR(50) NOT NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, symbol, market)
);

-- =============================================================================
-- 11. 订单表（交易执行引擎 + OrderWorker）
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_orders (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(100) UNIQUE NOT NULL,    -- 外部订单 ID（Alpaca UUID / CTP OrderRef / PAPER-xxx）
    strategy_id INTEGER REFERENCES qd_strategies(id),
    symbol VARCHAR(100) NOT NULL,
    market VARCHAR(50) NOT NULL,
    side VARCHAR(10) NOT NULL,                -- buy, sell
    quantity DECIMAL(20,8) NOT NULL,
    price DECIMAL(20,8) DEFAULT 0,            -- 下单价格
    status VARCHAR(20) DEFAULT 'pending',     -- pending, queued, submitted, filled, rejected, cancelled, failed
    adapter_type VARCHAR(20) DEFAULT 'paper', -- paper, alpaca, ctp
    filled_price DECIMAL(20,8) DEFAULT 0,    -- 成交均价
    fee DECIMAL(20,8) DEFAULT 0,             -- 手续费
    pnl DECIMAL(20,8) DEFAULT 0,             -- 盈亏（平仓时）
    error_msg TEXT,                           -- 错误信息
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- 12. 市场品种信息（原 11）
-- =============================================================================

CREATE TABLE IF NOT EXISTS qd_market_symbols (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(100) NOT NULL,
    market VARCHAR(50) NOT NULL,
    name VARCHAR(500),
    exchange VARCHAR(100),
    is_active BOOLEAN DEFAULT true,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, market)
);

-- =============================================================================
-- 13. 索引
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_orders_strategy ON qd_orders(strategy_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON qd_orders(status);
CREATE INDEX IF NOT EXISTS idx_strategies_user ON qd_strategies(user_id);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON qd_strategies(status);
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON qd_positions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON qd_trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON qd_backtest_runs(strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON qd_backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_backtest_equity_run ON qd_backtest_equity(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_hash ON qd_agent_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON qd_watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_market_symbols_lookup ON qd_market_symbols(market, symbol);

-- =============================================================================
-- 完成通知
-- =============================================================================
DO $$
BEGIN
    RAISE NOTICE 'QuantX PostgreSQL schema 初始化完成!';
END $$;
