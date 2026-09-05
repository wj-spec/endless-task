-- 14 B2: v1→v2 migration machinery removed (v2 sole runtime). The mapping
-- and migration-state tables are no longer read anywhere.
DROP TABLE IF EXISTS v2_migration_state;
DROP TABLE IF EXISTS v2_migration_conversation_mappings;
