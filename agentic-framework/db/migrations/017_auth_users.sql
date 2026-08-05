-- Migration 017: Auth users table + sessions.user_id FK
-- Run this in the Hasura SQL console (hospilot_app schema)

-- Users table
CREATE TABLE IF NOT EXISTS hospilot_app.users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username     text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    display_name text NOT NULL,
    role         text NOT NULL DEFAULT 'doctor' CHECK (role IN ('doctor', 'admin')),
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Add user_id FK to sessions (nullable so existing sessions are unaffected)
ALTER TABLE hospilot_app.sessions
    ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES hospilot_app.users(id) ON DELETE SET NULL;

-- Index for fast per-user session queries
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON hospilot_app.sessions(user_id);
