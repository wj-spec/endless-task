ALTER TABLE artifact_proposals
    ADD COLUMN source_labels TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_labels));
