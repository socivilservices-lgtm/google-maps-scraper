CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    invite_id TEXT,
    trial_ends_at TEXT,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    subscription_status TEXT NOT NULL DEFAULT 'none',
    plan TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    status TEXT NOT NULL,
    query TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    max_results INTEGER NOT NULL,
    backend TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    error_type TEXT,
    result_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_months (
    user_id TEXT NOT NULL,
    period TEXT NOT NULL,
    searches_used INTEGER NOT NULL DEFAULT 0,
    results_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, period)
);

CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);
