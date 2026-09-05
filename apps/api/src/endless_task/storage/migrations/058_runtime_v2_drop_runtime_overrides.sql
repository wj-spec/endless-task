-- 14-runtime-single-mode A2: per-conversation v1 override is removed (v2 is
-- the only runtime). Drop the override table created by 041.
DROP TABLE IF EXISTS v2_conversation_runtime_overrides;
