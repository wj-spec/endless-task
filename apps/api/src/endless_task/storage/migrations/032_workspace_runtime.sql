-- R5.12 工作区运行时：artifact 文件事实源列。
-- storage_path 为相对工作区根的应用托管路径（.endless-task/artifacts/...）；
-- NULL 表示仍为数据库全文（通用会话 artifact 或未迁移的存量工作区 artifact）。
ALTER TABLE artifacts ADD COLUMN storage_path TEXT;
ALTER TABLE artifacts ADD COLUMN content_sha256 TEXT;
ALTER TABLE artifact_versions ADD COLUMN storage_path TEXT;
ALTER TABLE artifact_versions ADD COLUMN content_sha256 TEXT;
