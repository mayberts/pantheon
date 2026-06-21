-- Pantheon schema. Single-user, cross-platform achievement aggregator.
-- Idempotent: safe to run on every startup.

CREATE TABLE IF NOT EXISTS linked_accounts (
    id              SERIAL PRIMARY KEY,
    platform        TEXT NOT NULL,
    external_id     TEXT NOT NULL,          -- steamid64, RA username, psn account id, etc.
    display_name    TEXT,
    enabled         BOOLEAN DEFAULT TRUE,
    last_synced_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (platform, external_id)
);

CREATE TABLE IF NOT EXISTS igdb_games (
    id                  BIGINT PRIMARY KEY,  -- IGDB id
    name                TEXT NOT NULL,
    slug                TEXT,
    cover_url           TEXT,
    first_release_date  DATE
);

CREATE TABLE IF NOT EXISTS platform_games (
    id                  SERIAL PRIMARY KEY,
    platform            TEXT NOT NULL,
    platform_app_id     TEXT NOT NULL,       -- steam appid, RA game id, np comm id
    name                TEXT NOT NULL,
    icon_url            TEXT,
    igdb_id             BIGINT REFERENCES igdb_games(id),
    total_achievements  INT DEFAULT 0,
    hltb_main           NUMERIC,             -- How Long To Beat: main story hours
    hltb_extra          NUMERIC,             -- main + extras hours
    hltb_complete       NUMERIC,             -- completionist hours
    UNIQUE (platform, platform_app_id)
);
-- safe migrations for existing deployments
ALTER TABLE platform_games ADD COLUMN IF NOT EXISTS hltb_main     NUMERIC;
ALTER TABLE platform_games ADD COLUMN IF NOT EXISTS hltb_extra    NUMERIC;
ALTER TABLE platform_games ADD COLUMN IF NOT EXISTS hltb_complete NUMERIC;

CREATE TABLE IF NOT EXISTS achievements (
    id                  SERIAL PRIMARY KEY,
    platform_game_id    INT NOT NULL REFERENCES platform_games(id) ON DELETE CASCADE,
    platform_ach_id     TEXT NOT NULL,       -- steam apiname, RA achievement id
    name                TEXT,
    description         TEXT,
    icon_url            TEXT,
    points              INT,                 -- gamerscore / RA points / trophy weight
    rarity_pct          NUMERIC,             -- global unlock percentage
    UNIQUE (platform_game_id, platform_ach_id)
);

CREATE TABLE IF NOT EXISTS user_achievements (
    id                  SERIAL PRIMARY KEY,
    linked_account_id   INT NOT NULL REFERENCES linked_accounts(id) ON DELETE CASCADE,
    achievement_id      INT NOT NULL REFERENCES achievements(id) ON DELETE CASCADE,
    unlocked            BOOLEAN DEFAULT FALSE,
    unlocked_at         TIMESTAMPTZ,
    UNIQUE (linked_account_id, achievement_id)
);

CREATE TABLE IF NOT EXISTS user_games (
    id                  SERIAL PRIMARY KEY,
    linked_account_id   INT NOT NULL REFERENCES linked_accounts(id) ON DELETE CASCADE,
    platform_game_id    INT NOT NULL REFERENCES platform_games(id) ON DELETE CASCADE,
    playtime_minutes    INT,
    earned_achievements INT DEFAULT 0,
    total_achievements  INT DEFAULT 0,
    completion_pct      NUMERIC DEFAULT 0,
    last_played_at      TIMESTAMPTZ,
    UNIQUE (linked_account_id, platform_game_id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                  SERIAL PRIMARY KEY,
    platform            TEXT,
    linked_account_id   INT REFERENCES linked_accounts(id) ON DELETE SET NULL,
    started_at          TIMESTAMPTZ DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT,
    detail              TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_games_account ON user_games(linked_account_id);
CREATE INDEX IF NOT EXISTS idx_user_ach_account ON user_achievements(linked_account_id);
CREATE INDEX IF NOT EXISTS idx_platform_games_igdb ON platform_games(igdb_id);
