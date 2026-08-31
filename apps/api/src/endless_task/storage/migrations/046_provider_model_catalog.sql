-- Model service catalog and connection status.
ALTER TABLE provider_profiles ADD COLUMN connection_state TEXT NOT NULL DEFAULT 'untested'
    CHECK (connection_state IN ('untested', 'ready', 'failed'));
ALTER TABLE provider_profiles ADD COLUMN last_checked_at TEXT;
ALTER TABLE provider_profiles ADD COLUMN last_error TEXT;

CREATE TABLE IF NOT EXISTS provider_models (
    provider_profile_id TEXT NOT NULL
        REFERENCES provider_profiles(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('discovered', 'manual')),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT,
    PRIMARY KEY (provider_profile_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_models_enabled
    ON provider_models(provider_profile_id, enabled, display_name COLLATE NOCASE);

INSERT INTO provider_models (
    provider_profile_id,
    model_id,
    display_name,
    source,
    enabled,
    created_at,
    updated_at,
    last_seen_at
)
SELECT
    id,
    default_model,
    default_model,
    'manual',
    1,
    created_at,
    updated_at,
    NULL
FROM provider_profiles
WHERE trim(default_model) != ''
ON CONFLICT(provider_profile_id, model_id) DO NOTHING;

-- Existing non-default conversation overrides remain valid after catalog validation.
INSERT INTO provider_models (
    provider_profile_id,
    model_id,
    display_name,
    source,
    enabled,
    created_at,
    updated_at,
    last_seen_at
)
SELECT
    COALESCE(c.provider_profile_id, p.default_provider_profile_id),
    trim(c.model_override),
    trim(c.model_override),
    'manual',
    1,
    MIN(c.created_at),
    MAX(c.updated_at),
    NULL
FROM conversations AS c
CROSS JOIN preferences AS p
JOIN provider_profiles AS profile
    ON profile.id = COALESCE(c.provider_profile_id, p.default_provider_profile_id)
WHERE c.model_override IS NOT NULL
  AND trim(c.model_override) != ''
GROUP BY
    COALESCE(c.provider_profile_id, p.default_provider_profile_id),
    trim(c.model_override)
ON CONFLICT(provider_profile_id, model_id) DO NOTHING;