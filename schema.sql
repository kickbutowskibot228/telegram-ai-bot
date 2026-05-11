
-- ============================================================
-- PostgreSQL schema для Patriot AI Bot
-- ============================================================

-- Таблица пользователей
CREATE TABLE IF NOT EXISTS users (
    user_id                 BIGINT PRIMARY KEY,
    model                   TEXT NOT NULL DEFAULT 'google/gemini-3-flash-preview',
    free_tokens             INTEGER NOT NULL DEFAULT 40,
    paid_tokens             INTEGER NOT NULL DEFAULT 0,
    image_mode              INTEGER NOT NULL DEFAULT 0,
    image_model             TEXT NOT NULL DEFAULT 'google/gemini-3-pro-image-preview',
    image_flow              TEXT DEFAULT '',
    pending_image_prompt    TEXT DEFAULT '',
    video_mode              INTEGER NOT NULL DEFAULT 0,
    video_model             TEXT NOT NULL DEFAULT 'kwaivgi/kling-v3.0-pro',
    video_flow              TEXT NOT NULL DEFAULT 'prompt_only',
    video_duration          INTEGER NOT NULL DEFAULT 5,
    video_aspect_ratio      TEXT NOT NULL DEFAULT '16:9',
    last_free_reset_at      TEXT DEFAULT NULL,
    is_banned               INTEGER DEFAULT 0
);

-- Таблица платежей
CREATE TABLE IF NOT EXISTS payments (
    id                  SERIAL PRIMARY KEY,
    payment_id          TEXT UNIQUE,
    idempotence_key     TEXT UNIQUE,
    user_id             BIGINT NOT NULL REFERENCES users(user_id),
    plan_key            TEXT NOT NULL,
    amount              INTEGER NOT NULL,
    tokens_count        INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- История чата
CREATE TABLE IF NOT EXISTS chat_history (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(user_id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Задачи генерации (изображения/видео)
CREATE TABLE IF NOT EXISTS generation_jobs (
    id                      SERIAL PRIMARY KEY,
    job_uuid                TEXT UNIQUE,
    user_id                 BIGINT NOT NULL REFERENCES users(user_id),
    provider                TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    model                   TEXT NOT NULL,
    flow                    TEXT NOT NULL DEFAULT '',
    prompt_text             TEXT DEFAULT '',
    cost                    INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'created',
    provider_generation_id  TEXT DEFAULT '',
    polling_url             TEXT DEFAULT '',
    file_path               TEXT DEFAULT '',
    error_text              TEXT DEFAULT '',
    charged                 INTEGER NOT NULL DEFAULT 0,
    chat_id                 BIGINT DEFAULT 0,
    wait_msg_id             BIGINT DEFAULT 0,
    next_poll_at            DOUBLE PRECISION DEFAULT 0,
    attempts                INTEGER DEFAULT 0,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Блокировки пользователей
CREATE TABLE IF NOT EXISTS user_locks (
    user_id     BIGINT PRIMARY KEY,
    locked_at   DOUBLE PRECISION NOT NULL,
    reason      TEXT DEFAULT ''
);

-- ============================================================
-- Индексы
-- ============================================================
CREATE INDEX IF NOT EXISTS ix_chat_user    ON chat_history(user_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_poll    ON generation_jobs(kind, status, next_poll_at);
CREATE INDEX IF NOT EXISTS ix_jobs_user    ON generation_jobs(user_id);
CREATE INDEX IF NOT EXISTS ix_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS ix_locks_age    ON user_locks(locked_at);
