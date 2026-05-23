-- ============================================================
-- Trav Agent — Supabase Database Schema
-- Kör denna SQL i Supabase Dashboard → SQL Editor → New Query
-- ============================================================

-- 1. BANOR
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    country     TEXT DEFAULT 'SE',
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- 2. SPELOMGÅNGAR (V85, V75, V86 etc)
CREATE TABLE IF NOT EXISTS game_rounds (
    game_id     TEXT PRIMARY KEY,
    game_type   TEXT NOT NULL,
    round_date  DATE NOT NULL,
    track_id    INTEGER REFERENCES tracks(id),
    track_name  TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'upcoming',
    turnover    BIGINT,
    jackpot     BIGINT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_game_rounds_type_date ON game_rounds(game_type, round_date DESC);
CREATE INDEX IF NOT EXISTS idx_game_rounds_date ON game_rounds(round_date DESC);

-- 3. LOPP
CREATE TABLE IF NOT EXISTS races (
    id              TEXT PRIMARY KEY,
    game_id         TEXT REFERENCES game_rounds(game_id),
    race_number     INTEGER NOT NULL,
    track_id        INTEGER REFERENCES tracks(id),
    track_name      TEXT NOT NULL DEFAULT '',
    race_date       DATE NOT NULL,
    distance        INTEGER NOT NULL DEFAULT 0,
    start_method    TEXT NOT NULL DEFAULT 'auto',
    purse           INTEGER DEFAULT 0,
    race_type       TEXT DEFAULT '',
    race_name       TEXT DEFAULT '',
    conditions      TEXT DEFAULT '',
    breed           TEXT DEFAULT 'varmblod',
    is_stair_draw   BOOLEAN DEFAULT FALSE,
    status          TEXT DEFAULT 'upcoming',
    num_starters    INTEGER DEFAULT 0,
    result_order    INTEGER[] DEFAULT '{}',
    win_odds        REAL,
    upset_risk      REAL DEFAULT 0,
    upset_candidates INTEGER[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_races_game ON races(game_id);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(race_date DESC);
CREATE INDEX IF NOT EXISTS idx_races_track ON races(track_name, race_date DESC);

-- 4. HÄSTAR
CREATE TABLE IF NOT EXISTS horses (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    age                 INTEGER DEFAULT 0,
    sex                 TEXT DEFAULT 'valack',
    breed               TEXT DEFAULT 'varmblod',
    sire                TEXT DEFAULT '',
    dam                 TEXT DEFAULT '',
    dam_sire            TEXT DEFAULT '',
    trainer_name        TEXT DEFAULT '',
    trainer_id          INTEGER,
    owner_name          TEXT DEFAULT '',
    career_starts       INTEGER DEFAULT 0,
    career_wins         INTEGER DEFAULT 0,
    career_seconds      INTEGER DEFAULT 0,
    career_thirds       INTEGER DEFAULT 0,
    career_prize_money  BIGINT DEFAULT 0,
    career_win_rate     REAL DEFAULT 0,
    career_top3_rate    REAL DEFAULT 0,
    career_avg_km_time  REAL,
    career_prize_per_start INTEGER DEFAULT 0,
    career_avg_odds     REAL,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_horses_name ON horses(name);
CREATE INDEX IF NOT EXISTS idx_horses_trainer ON horses(trainer_id);

-- 5. STARTHISTORIK (alla tidigare starter per häst)
CREATE TABLE IF NOT EXISTS past_starts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    horse_id        INTEGER NOT NULL REFERENCES horses(id),
    start_date      DATE NOT NULL,
    track_name      TEXT DEFAULT '',
    track_id        INTEGER REFERENCES tracks(id),
    distance        INTEGER DEFAULT 0,
    start_method    TEXT DEFAULT 'auto',
    post_position   INTEGER DEFAULT 0,
    placement       INTEGER,
    disqualified    BOOLEAN DEFAULT FALSE,
    galloped        BOOLEAN DEFAULT FALSE,
    finish_time     REAL,
    km_time         REAL,
    prize_money     INTEGER DEFAULT 0,
    driver_name     TEXT DEFAULT '',
    trainer_name    TEXT DEFAULT '',
    num_starters    INTEGER DEFAULT 0,
    odds            REAL,
    race_purse      INTEGER DEFAULT 0,
    comment         TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(horse_id, start_date, track_name, distance, post_position)
);
CREATE INDEX IF NOT EXISTS idx_past_starts_horse ON past_starts(horse_id, start_date DESC);
CREATE INDEX IF NOT EXISTS idx_past_starts_date ON past_starts(start_date DESC);
CREATE INDEX IF NOT EXISTS idx_past_starts_track ON past_starts(track_name);

-- 6. LOPPSTARTER (häst-i-lopp med analysresultat)
CREATE TABLE IF NOT EXISTS race_entries (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    race_id         TEXT NOT NULL REFERENCES races(id),
    horse_id        INTEGER NOT NULL REFERENCES horses(id),
    post_position   INTEGER NOT NULL,
    distance        INTEGER DEFAULT 0,
    driver_name     TEXT DEFAULT '',
    driver_id       INTEGER,
    trainer_name    TEXT DEFAULT '',
    weight          REAL,
    shoes           TEXT DEFAULT '',
    scratched       BOOLEAN DEFAULT FALSE,
    odds            REAL,
    bet_percentage  REAL,
    trend           REAL,
    points          INTEGER,
    factor_scores   JSONB DEFAULT '{}',
    composite_score REAL DEFAULT 0,
    rank            INTEGER DEFAULT 0,
    recommendation  TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(race_id, horse_id),
    UNIQUE(race_id, post_position)
);
CREATE INDEX IF NOT EXISTS idx_race_entries_race ON race_entries(race_id);
CREATE INDEX IF NOT EXISTS idx_race_entries_horse ON race_entries(horse_id);
CREATE INDEX IF NOT EXISTS idx_race_entries_composite ON race_entries(composite_score DESC);

-- 7. BACKTEST-KÖRNINGAR
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    game_type           TEXT NOT NULL,
    start_date          DATE NOT NULL,
    end_date            DATE NOT NULL,
    num_rounds          INTEGER DEFAULT 0,
    num_races           INTEGER DEFAULT 0,
    spik_accuracy       REAL DEFAULT 0,
    top_pick_accuracy   REAL DEFAULT 0,
    avg_top3_overlap    REAL DEFAULT 0,
    roi_percent         REAL DEFAULT 0,
    factor_evaluations  JSONB DEFAULT '[]',
    suggested_weights   JSONB DEFAULT '{}',
    model_version       TEXT DEFAULT 'v4',
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- 8. LOPPREDIKTIONER (per-lopp prediktioner)
CREATE TABLE IF NOT EXISTS race_predictions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    backtest_run_id     BIGINT REFERENCES backtest_runs(id),
    race_id             TEXT,
    race_number         INTEGER NOT NULL,
    race_date           DATE NOT NULL,
    track               TEXT NOT NULL,
    game_type           TEXT NOT NULL,
    predicted_ranking   INTEGER[] DEFAULT '{}',
    actual_result       INTEGER[] DEFAULT '{}',
    horse_scores        JSONB DEFAULT '{}',
    horse_bet_pct       JSONB DEFAULT '{}',
    horse_factor_scores JSONB DEFAULT '{}',
    spik                INTEGER,
    top_picks           INTEGER[] DEFAULT '{}',
    spik_correct        BOOLEAN,
    created_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_race_predictions_run ON race_predictions(backtest_run_id);
CREATE INDEX IF NOT EXISTS idx_race_predictions_date ON race_predictions(race_date DESC);

-- 9. RÅ API-SVAR (backup/audit)
CREATE TABLE IF NOT EXISTS raw_api_responses (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url         TEXT NOT NULL,
    url_hash    TEXT NOT NULL UNIQUE,
    response    JSONB NOT NULL,
    fetched_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_raw_api_url_hash ON raw_api_responses(url_hash);

-- 10. ANALYSRESULTAT (walk-forward, sensitivity, game-type analyser)
CREATE TABLE IF NOT EXISTS analysis_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_type        TEXT NOT NULL,  -- 'walk_forward', 'walk_forward_extended', 'sensitivity', 'game_type_analysis'
    run_date        TIMESTAMPTZ DEFAULT now(),
    parameters      JSONB DEFAULT '{}',
    results         JSONB DEFAULT '{}',
    summary         TEXT DEFAULT '',
    num_rounds      INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_type ON analysis_runs(run_type, run_date DESC);

-- ============================================================
-- Klar! Verifiera med: SELECT tablename FROM pg_tables WHERE schemaname = 'public';
-- ============================================================
