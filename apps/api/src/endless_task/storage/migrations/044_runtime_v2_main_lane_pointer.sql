-- Conversation pointers identify only the selected main lane.
-- Repair databases where run lifecycle writes or the old promotion path left the
-- pointer, lane kinds, and active run columns describing different facts.
UPDATE v2_conversation_pointers
SET active_lane_id = COALESCE(
        (
            SELECT event.lane_id
            FROM v2_lane_events AS event
            JOIN v2_lanes AS promoted_lane ON promoted_lane.id = event.lane_id
            WHERE event.conversation_id = v2_conversation_pointers.conversation_id
              AND event.event_type = 'branch.promoted'
              AND promoted_lane.kind IN ('main', 'persistent_branch')
            ORDER BY event.event_seq DESC
            LIMIT 1
        ),
        (
            SELECT lane.id
            FROM v2_lanes AS lane
            WHERE lane.conversation_id = v2_conversation_pointers.conversation_id
              AND lane.kind = 'main'
              AND lane.status = 'active'
            ORDER BY lane.created_at, lane.id
            LIMIT 1
        ),
        active_lane_id
    ),
    active_run_id = NULL,
    active_run_variant_id = NULL;

-- A normal Conversation has exactly one role-bearing main lane. Historical
-- promotion changed only the pointer, so first demote stale main rows and then
-- promote the pointer target. Temporary Conversation roots retain kind=temporary.
UPDATE v2_lanes
SET kind = 'persistent_branch'
WHERE kind = 'main'
  AND EXISTS (
      SELECT 1
      FROM v2_conversation_pointers AS pointer
      JOIN v2_lanes AS selected ON selected.id = pointer.active_lane_id
      JOIN conversations AS conversation ON conversation.id = pointer.conversation_id
      WHERE pointer.conversation_id = v2_lanes.conversation_id
        AND pointer.active_lane_id <> v2_lanes.id
        AND selected.kind IN ('main', 'persistent_branch')
        AND conversation.kind = 'normal'
  );

UPDATE v2_lanes
SET kind = 'main', status = 'active', archived_at = NULL
WHERE kind IN ('main', 'persistent_branch')
  AND EXISTS (
      SELECT 1
      FROM v2_conversation_pointers AS pointer
      JOIN conversations AS conversation ON conversation.id = pointer.conversation_id
      WHERE pointer.active_lane_id = v2_lanes.id
        AND conversation.kind = 'normal'
  );