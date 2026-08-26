"""P5 知识源仓储：file/note 源的增删改、过期与索引同步。"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from endless_task.domain.models import (
    KnowledgeHit,
    KnowledgeScope,
    KnowledgeSource,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    KnowledgeSourceStatus,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


class SqliteKnowledgeRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    # ---------- 创建与读取 ----------

    def create_source(
        self,
        *,
        kind: KnowledgeSourceKind,
        origin: KnowledgeSourceOrigin,
        title: str,
        content: str,
        file_name: Optional[str] = None,
        source_conversation_id: Optional[str] = None,
        proposed_by_turn_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> KnowledgeSource:
        normalized_title = self._validate_title(title)
        normalized_content = self._validate_content(content)
        if kind is KnowledgeSourceKind.FILE and not (file_name or "").strip():
            raise ValidationError("File sources require a file name.")
        now = self._clock()
        source_id = self._id_factory("ks")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_sources (
                    id, kind, origin, title, content, file_name, status,
                    source_conversation_id, proposed_by_turn_id,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    kind.value,
                    origin.value,
                    normalized_title,
                    normalized_content,
                    file_name.strip() if file_name else None,
                    source_conversation_id,
                    proposed_by_turn_id,
                    expires_at,
                    now,
                    now,
                ),
            )
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> KnowledgeSource:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Knowledge source not found: {source_id}")
        return self._from_row(row)

    def list_sources(
        self, status: KnowledgeSourceStatus = KnowledgeSourceStatus.ACTIVE
    ) -> Sequence[KnowledgeSource]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_sources WHERE status = ? "
                "ORDER BY updated_at DESC, id DESC",
                (status.value,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    # ---------- 更新 ----------

    def update_source(
        self,
        source_id: str,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        file_name: Optional[str] = None,
        expires_at: Optional[str] = None,
        by_user: bool = True,
    ) -> KnowledgeSource:
        current = self.get_source(source_id)
        if current.status is KnowledgeSourceStatus.DELETED:
            raise InvalidStateError("Deleted knowledge sources cannot be updated.")
        new_title = self._validate_title(title) if title is not None else current.title
        new_content = (
            self._validate_content(content) if content is not None else current.content
        )
        new_file_name = file_name if file_name is not None else current.file_name
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE knowledge_sources
                SET title = ?, content = ?, file_name = ?, expires_at = ?,
                    user_edited_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_title,
                    new_content,
                    new_file_name,
                    expires_at,
                    now if by_user else current.user_edited_at,
                    now,
                    source_id,
                ),
            )
        return self.get_source(source_id)

    # ---------- 生命周期 ----------

    def expire_source(self, source_id: str) -> KnowledgeSource:
        current = self.get_source(source_id)
        if current.status is not KnowledgeSourceStatus.ACTIVE:
            raise InvalidStateError("Only active knowledge sources can expire.")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_sources SET status = 'expired', expired_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, source_id),
            )
        return self.get_source(source_id)

    def restore_source(self, source_id: str) -> KnowledgeSource:
        current = self.get_source(source_id)
        if current.status is not KnowledgeSourceStatus.EXPIRED:
            raise InvalidStateError("Only expired knowledge sources can be restored.")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_sources SET status = 'active', expired_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (now, source_id),
            )
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> KnowledgeSource:
        current = self.get_source(source_id)
        if current.status is KnowledgeSourceStatus.DELETED:
            raise InvalidStateError("Knowledge source is already deleted.")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_sources SET status = 'deleted', deleted_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, source_id),
            )
        return self.get_source(source_id)

    def expire_due(self, now: str) -> Sequence[KnowledgeSource]:
        """到期扫描：expires_at <= now 的活跃源置为过期。"""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM knowledge_sources "
                "WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchall()
        expired: list[KnowledgeSource] = []
        for row in rows:
            expired.append(self.expire_source(row["id"]))
        return expired

    # ---------- 内部 ----------

    _MAX_FRAGMENTS = 24

    _SCOPE_QUERIES = {
        "source": (
            "SELECT id AS ref_id, title, content AS body "
            "FROM knowledge_sources WHERE status = 'active' AND ({match})"
        ),
        "memory": (
            "SELECT id AS ref_id, '记忆' AS title, content AS body "
            "FROM memories WHERE status = 'active' AND ({match})"
        ),
        "artifact": (
            "SELECT a.id AS ref_id, a.title, v.content AS body "
            "FROM artifacts a "
            "JOIN artifact_versions v ON v.artifact_id = a.id "
            "AND v.ordinal = a.current_version_ordinal "
            "WHERE a.status = 'active' AND ({match})"
        ),
        "conversation": (
            "SELECT m.turn_id AS ref_id, c.title, m.content AS body "
            "FROM messages m "
            "JOIN turns t ON t.id = m.turn_id "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.kind = 'normal' AND c.status = 'active' "
            "AND t.status = 'completed' AND ({match})"
        ),
    }

    def search(
        self,
        query: str,
        scopes: Sequence[KnowledgeScope],
        limit: int = 8,
    ) -> Dict[KnowledgeScope, List[KnowledgeHit]]:
        """读时联合检索：三字符片段 OR 匹配，按命中片段数排序。

        中文没有词边界，整句 LIKE 召回率太低，拆成 3 字滑窗 OR 匹配
        （与 FTS trigram 同思路，但不需要写时索引）；不足 3 字的查询退化为整句 LIKE。
        结果在读时计算，永远新鲜。
        """
        cleaned = query.strip()
        if not cleaned or not scopes or limit <= 0:
            return {}
        fragments = self._fragments(cleaned)
        grouped: Dict[KnowledgeScope, List[KnowledgeHit]] = {}
        with self._database.connect() as connection:
            for scope in scopes:
                template = self._SCOPE_QUERIES.get(scope.value)
                if template is None:
                    continue
                hits = self._search_scope(connection, scope, template, fragments, limit)
                if hits:
                    grouped[scope] = hits
        return grouped

    def _search_scope(
        self,
        connection,
        scope: KnowledgeScope,
        template: str,
        fragments: Sequence[str],
        limit: int,
    ) -> List[KnowledgeHit]:
        clause = " OR ".join(["title LIKE ? OR body LIKE ?"] * len(fragments))
        params: List[str] = []
        for fragment in fragments:
            like = f"%{fragment}%"
            params.extend((like, like))
        rows = connection.execute(template.format(match=clause), params).fetchall()
        best: Dict[str, tuple[int, str, str, str]] = {}
        for row in rows:
            body = row["body"] or ""
            title = row["title"] or ""
            score = sum(
                body.count(fragment) + title.count(fragment)
                for fragment in fragments
            )
            current = best.get(row["ref_id"])
            if current is None or score > current[0]:
                best[row["ref_id"]] = (score, row["ref_id"], title, body)
        ordered = sorted(best.values(), key=lambda item: item[0], reverse=True)[:limit]
        return [
            KnowledgeHit(
                scope=scope,
                ref_id=ref_id,
                title=title,
                snippet=self._plain_snippet(body, fragments),
            )
            for _, ref_id, title, body in ordered
        ]

    @staticmethod
    def _fragments(query: str) -> List[str]:
        fragments: List[str] = []
        seen: set[str] = set()
        for token in query.split():
            if len(token) < 3:
                continue
            for start in range(len(token) - 2):
                gram = token[start : start + 3]
                if gram not in seen:
                    seen.add(gram)
                    fragments.append(gram)
        return fragments[:24] or [query]

    @staticmethod
    def _plain_snippet(content: str, fragments: Sequence[str]) -> str:
        index = -1
        hit = ""
        for fragment in fragments:
            index = content.find(fragment)
            if index >= 0:
                hit = fragment
                break
        if index < 0:
            return content[:120]
        start = max(0, index - 40)
        return content[start : index + len(hit) + 80]

    @staticmethod
    def _validate_title(title: str) -> str:
        normalized = (title or "").strip()
        if not normalized:
            raise ValidationError("Knowledge source title is required.")
        if len(normalized) > 200:
            raise ValidationError("Knowledge source title is too long.")
        return normalized

    @staticmethod
    def _validate_content(content: str) -> str:
        normalized = (content or "").strip()
        if not normalized:
            raise ValidationError("Knowledge source content is required.")
        return normalized

    @staticmethod
    def _from_row(row) -> KnowledgeSource:
        return KnowledgeSource(
            id=row["id"],
            kind=KnowledgeSourceKind(row["kind"]),
            origin=KnowledgeSourceOrigin(row["origin"]),
            title=row["title"],
            content=row["content"],
            status=KnowledgeSourceStatus(row["status"]),
            file_name=row["file_name"],
            source_conversation_id=row["source_conversation_id"],
            proposed_by_turn_id=row["proposed_by_turn_id"],
            user_edited_at=row["user_edited_at"],
            expires_at=row["expires_at"],
            expired_at=row["expired_at"],
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
