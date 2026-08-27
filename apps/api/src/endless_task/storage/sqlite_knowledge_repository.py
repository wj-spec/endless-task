"""P5 知识源仓储：file/note 源的增删改、过期与读时联合检索。"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Dict, List, Mapping, Optional, Sequence

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


#: 作用域分层：curated（用户/助手精心维护）优先于 derived（派生语料）。
CURATED_SCOPES: tuple[KnowledgeScope, ...] = (
    KnowledgeScope.SOURCE,
    KnowledgeScope.MEMORY,
)
DERIVED_SCOPES: tuple[KnowledgeScope, ...] = (
    KnowledgeScope.ARTIFACT,
    KnowledgeScope.CONVERSATION,
)
DEFAULT_SCOPE_WEIGHTS: Dict[KnowledgeScope, float] = {
    KnowledgeScope.SOURCE: 1.0,
    KnowledgeScope.MEMORY: 1.0,
    KnowledgeScope.ARTIFACT: 0.8,
    KnowledgeScope.CONVERSATION: 0.6,
}

_PUNCTUATION_RE = re.compile(
    "[\u0021-\u002f\u003a-\u0040\u005b-\u0060\u007b-\u007e"
    "\u3000-\u303f\uff00-\uffef\u2000-\u206f]+"
)


def normalize_query(query: str) -> str:
    """检索 query 归一化：NFKC 全半角统一、去标点、折叠空白、英文小写。"""
    normalized = unicodedata.normalize("NFKC", query or "")
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    normalized = " ".join(normalized.split())
    return normalized.casefold()


def scope_tier(scope: KnowledgeScope) -> int:
    """0 = curated（高优先），1 = derived。"""
    return 0 if scope in CURATED_SCOPES else 1


class SqliteKnowledgeRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
        scope_weights: Optional[Mapping[KnowledgeScope, float]] = None,
        synonym_map: Optional[Mapping[str, Sequence[str]]] = None,
        hybrid_literal_weight: float = 0.4,
        hybrid_semantic_weight: float = 0.6,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory
        weights = dict(DEFAULT_SCOPE_WEIGHTS)
        if scope_weights:
            for scope, weight in scope_weights.items():
                if weight < 0:
                    raise ValidationError("Scope weights cannot be negative.")
                weights[scope] = weight
        self._scope_weights = weights
        self._synonym_map = dict(synonym_map or {})
        if hybrid_literal_weight < 0 or hybrid_semantic_weight < 0:
            raise ValidationError("Hybrid weights cannot be negative.")
        self._hybrid_literal_weight = float(hybrid_literal_weight)
        self._hybrid_semantic_weight = float(hybrid_semantic_weight)
        self._semantic_searcher = None
        self._embedding_hook = None

    # ---------- R5.8 语义注入点 ----------

    def set_semantic_searcher(self, searcher) -> None:
        """注入语义评分器（embed_query/score_refs）；None 时走纯字面路径。"""
        self._semantic_searcher = searcher

    def set_embedding_hook(self, hook) -> None:
        """注入索引钩子（submit/remove），写侧增量建向量。"""
        self._embedding_hook = hook

    def _notify_hook(self, action: str, ref_id: str) -> None:
        hook = self._embedding_hook
        if hook is None:
            return
        try:
            if action == "submit":
                hook.submit(KnowledgeScope.SOURCE.value, ref_id)
            else:
                hook.remove(KnowledgeScope.SOURCE.value, ref_id)
        except Exception:  # noqa: BLE001 钩子失败不影响写操作
            pass

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
        file_size: Optional[int] = None,
        file_sha256: Optional[str] = None,
    ) -> KnowledgeSource:
        normalized_title = self._validate_title(title)
        normalized_content = self._validate_content(content)
        if kind is KnowledgeSourceKind.FILE and not (file_name or "").strip():
            raise ValidationError("File sources require a file name.")
        if file_size is not None and file_size < 0:
            raise ValidationError("File size cannot be negative.")
        now = self._clock()
        source_id = self._id_factory("ks")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_sources (
                    id, kind, origin, title, content, file_name, status,
                    source_conversation_id, proposed_by_turn_id,
                    expires_at, created_at, updated_at,
                    file_size, file_sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
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
                    file_size,
                    file_sha256,
                ),
            )
        self._notify_hook("submit", source_id)
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
        self._notify_hook("submit", source_id)
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
        self._notify_hook("remove", source_id)
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
        self._notify_hook("submit", source_id)
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
        self._notify_hook("remove", source_id)
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
            "AND t.status = 'completed' AND ({match}) ORDER BY m.rowid"
        ),
    }

    def search(
        self,
        query: str,
        scopes: Sequence[KnowledgeScope],
        limit: int = 8,
    ) -> Dict[KnowledgeScope, List[KnowledgeHit]]:
        """读时联合检索：query 归一化 + 三字符片段 OR 匹配，按加权命中数排序。

        中文没有词边界，整句 LIKE 召回率太低，拆成 3 字滑窗 OR 匹配
        （与 FTS trigram 同思路，但不需要写时索引）；不足 3 字的查询退化为整句 LIKE。
        结果在读时计算，永远新鲜。命中分 = 片段命中次数 × 作用域权重。
        """
        cleaned = normalize_query(query)
        if not cleaned or not scopes or limit <= 0:
            return {}
        cleaned = self._expand_synonyms(cleaned)
        fragments = self._fragments(cleaned)
        query_vector = self._embed_query(cleaned)
        grouped: Dict[KnowledgeScope, List[KnowledgeHit]] = {}
        with self._database.connect() as connection:
            for scope in scopes:
                template = self._SCOPE_QUERIES.get(scope.value)
                if template is None:
                    continue
                hits = self._search_scope(
                    connection, scope, template, fragments, limit, query_vector
                )
                if hits:
                    grouped[scope] = hits
        return grouped

    def _embed_query(self, query: str):
        if self._semantic_searcher is None:
            return None
        try:
            return self._semantic_searcher.embed_query(query)
        except Exception:  # noqa: BLE001 嵌入失败 → 纯字面路径
            return None

    def visible_rows(self, scope) -> List:
        """某作用域当前可见的全部检索行（ref_id/title/body），R5.8 语义索引范围。"""
        scope_value = (
            scope.value if isinstance(scope, KnowledgeScope) else str(scope)
        )
        template = self._SCOPE_QUERIES.get(scope_value)
        if template is None:
            return []
        with self._database.connect() as connection:
            return connection.execute(template.format(match="1=1")).fetchall()

    def _search_scope(
        self,
        connection,
        scope: KnowledgeScope,
        template: str,
        fragments: Sequence[str],
        limit: int,
        query_vector=None,
    ) -> List[KnowledgeHit]:
        clause = " OR ".join(["title LIKE ? OR body LIKE ?"] * len(fragments))
        params: List[str] = []
        for fragment in fragments:
            like = f"%{fragment}%"
            params.extend((like, like))
        rows = connection.execute(template.format(match=clause), params).fetchall()
        weight = self._scope_weights.get(scope, 1.0)
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
        if query_vector is not None and self._semantic_searcher is not None:
            semantic_scores, visible_map = self._semantic_scores(
                connection, scope, template, query_vector
            )
            if semantic_scores:
                return self._fuse_hits(
                    scope, weight, fragments, limit, best, semantic_scores,
                    visible_map,
                )
        ordered = sorted(best.values(), key=lambda item: item[0], reverse=True)[:limit]
        return [
            KnowledgeHit(
                scope=scope,
                ref_id=ref_id,
                title=title,
                snippet=self._plain_snippet(body, fragments),
                score=raw_score * weight,
            )
            for raw_score, ref_id, title, body in ordered
        ]

    def _semantic_scores(
        self, connection, scope: KnowledgeScope, template: str, query_vector
    ) -> tuple[Dict[str, float], Dict[str, tuple[str, str]]]:
        """向量召回：仅对当前可见行评分（临时/归档/过期永不进入检索）。

        返回（余弦分映射，可见行 title/body 映射）；评分失败返回空映射，
        调用方退回纯字面路径。
        """
        visible = connection.execute(template.format(match="1=1")).fetchall()
        ref_ids: List[str] = []
        visible_map: Dict[str, tuple[str, str]] = {}
        for row in visible:
            ref_id = row["ref_id"]
            if ref_id not in visible_map:
                visible_map[ref_id] = (row["title"] or "", row["body"] or "")
                ref_ids.append(ref_id)
        if not ref_ids:
            return {}, {}
        try:
            scores = dict(
                self._semantic_searcher.score_refs(scope, query_vector, ref_ids)
            )
        except Exception:  # noqa: BLE001 评分失败 → 纯字面路径
            return {}, visible_map
        return scores, visible_map

    def _fuse_hits(
        self,
        scope: KnowledgeScope,
        weight: float,
        fragments: Sequence[str],
        limit: int,
        best: Dict[str, tuple[int, str, str, str]],
        semantic_scores: Dict[str, float],
        visible_map: Dict[str, tuple[str, str]],
    ) -> List[KnowledgeHit]:
        """加权融合：字面分归一化后与余弦分线性组合（默认 0.4/0.6）。"""
        max_literal = max((item[0] for item in best.values()), default=0)
        scored: List[tuple[float, str, str, str]] = []
        for ref_id in set(best) | set(semantic_scores):
            literal_entry = best.get(ref_id)
            literal_norm = 0.0
            if literal_entry is not None and max_literal > 0:
                literal_norm = literal_entry[0] / max_literal
            semantic_score = max(0.0, semantic_scores.get(ref_id, 0.0))
            fused = (
                self._hybrid_literal_weight * literal_norm
                + self._hybrid_semantic_weight * semantic_score
            )
            if fused <= 0:
                continue
            if literal_entry is not None:
                _, _, title, body = literal_entry
            else:
                title, body = visible_map.get(ref_id, ("", ""))
            scored.append(
                (fused, ref_id, title, self._plain_snippet(body, fragments))
            )
        ordered = sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
        return [
            KnowledgeHit(
                scope=scope,
                ref_id=ref_id,
                title=title,
                snippet=snippet,
                score=fused * weight,
            )
            for fused, ref_id, title, snippet in ordered
        ]

    def _expand_synonyms(self, query: str) -> str:
        if not self._synonym_map:
            return query
        expansions: list[str] = []
        for term, synonyms in self._synonym_map.items():
            normalized_term = normalize_query(term)
            if normalized_term and normalized_term in query:
                expansions.extend(
                    synonym
                    for synonym in synonyms
                    if normalize_query(synonym)
                )
        if not expansions:
            return query
        return " ".join([query, *[normalize_query(item) for item in expansions]])

    @staticmethod
    def _fragments(query: str) -> List[str]:
        fragments: List[str] = []
        seen: set[str] = set()
        for token in query.split():
            if len(token) == 2:
                # 双字词（中文常见）整体参与 LIKE 匹配；单字过泛，丢弃。
                if token not in seen:
                    seen.add(token)
                    fragments.append(token)
                continue
            if len(token) < 2:
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
            file_size=row["file_size"],
            file_sha256=row["file_sha256"],
        )
