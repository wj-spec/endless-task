-- M4A delegation: mark child-run conversations so product enumeration
-- (Conversation rail, knowledge lifecycle) excludes them by default.
--
-- 06 2.4: a child run is an internal execution object and must not appear
-- in the product Conversation / Lane navigation. The RuntimeV2AgentKernel
-- creates one isolated conversation per child run; this column records the
-- owning child run id so product queries can filter child conversations
-- without inventing a new ConversationKind (kind CHECK is normal/ephemeral).
--
-- No FOREIGN KEY on purpose: the conversation is created before its child
-- run row (the executor needs a conversation/lane/entry first), so an FK
-- would make the isolation marker un-writable. Integrity of the marker is
-- enforced by the delegation coordinator/adapter flow.
ALTER TABLE conversations
    ADD COLUMN delegation_parent_run_id TEXT;

CREATE INDEX idx_conversations_delegation_parent
    ON conversations(delegation_parent_run_id)
    WHERE delegation_parent_run_id IS NOT NULL;
