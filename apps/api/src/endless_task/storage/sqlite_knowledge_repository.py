"""P5 知识源仓储：file/note 源的增删改、过期与读时联合检索。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
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
from .knowledge_chunking import DEFAULT_CHUNK_MAX_CHARS, chunk_text

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
        self._feedback_provider = None

    # ---------- R5.8 语义注入点 ----------

    def set_semantic_searcher(self, searcher) -> None:
        """注入语义评分器（embed_query/score_refs）；None 时走纯字面路径。"""
        self._semantic_searcher = searcher

    def set_embedding_hook(self, hook) -> None:
        """注入索引钩子（submit/remove），写侧增量建向量。"""
        self._embedding_hook = hook

    def set_feedback_provider(self, provider) -> None:
        """R5.10：注入引用反馈权重提供器；None 时排序不受反馈影响。"""
        self._feedback_provider = provider

    def _notify_hook(
        self, action: str, source_id: str, kind, stale_ref_ids: Sequence[str] = ()
    ) -> None:
        """R5.9：note 源按整体索引；file 源按分块索引（ref_id=chunk id）。"""
        hook = self._embedding_hook
        if hook is None:
            return
        scope_value = KnowledgeScope.SOURCE.value
        try:
            for stale_id in stale_ref_ids:
                hook.remove(scope_value, stale_id)
            if action == "submit":
                if kind is KnowledgeSourceKind.FILE:
                    for chunk_id in self.list_chunk_ids(source_id):
                        hook.submit(scope_value, chunk_id)
                else:
                    hook.submit(scope_value, source_id)
            else:
                hook.remove(scope_value, source_id)
                for chunk_id in self.list_chunk_ids(source_id):
                    hook.remove(scope_value, chunk_id)
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
        workspace_id: Optional[str] = None,
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
                    file_size, file_sha256, workspace_id
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
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
                    workspace_id,
                ),
            )
            if kind is KnowledgeSourceKind.FILE:
                self._replace_chunks(connection, source_id, normalized_content)
        self._notify_hook("submit", source_id, kind)
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
        self,
        status: KnowledgeSourceStatus = KnowledgeSourceStatus.ACTIVE,
        workspace_id: Optional[str] = None,
    ) -> Sequence[KnowledgeSource]:
        """workspace_id：None 不过滤；"general" 仅全局；区 id = 该区 ∪ 全局。"""
        sql = "SELECT * FROM knowledge_sources WHERE status = ?"
        params: list = [status.value]
        if workspace_id == self.WORKSPACE_GENERAL:
            sql += " AND workspace_id IS NULL"
        elif workspace_id is not None:
            sql += " AND (workspace_id IS NULL OR workspace_id = ?)"
            params.append(workspace_id)
        sql += " ORDER BY updated_at DESC, id DESC"
        with self._database.connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [self._from_row(row) for row in rows]

    def list_sources_in_partition(
        self, workspace_id: Optional[str]
    ) -> Sequence[KnowledgeSource]:
        """R5.11 同分区活跃源（精确匹配）：去重检测只在本分区内比较。"""
        sql = "SELECT * FROM knowledge_sources WHERE status = 'active'"
        params: tuple
        if workspace_id is None:
            sql += " AND workspace_id IS NULL"
            params = ()
        else:
            sql += " AND workspace_id = ?"
            params = (workspace_id,)
        sql += " ORDER BY updated_at DESC, id DESC"
        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
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
        stale_chunk_ids = (
            self.list_chunk_ids(source_id)
            if current.kind is KnowledgeSourceKind.FILE
            else ()
        )
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
            if current.kind is KnowledgeSourceKind.FILE:
                self._replace_chunks(connection, source_id, new_content)
        self._notify_hook(
            "submit", source_id, current.kind, stale_ref_ids=stale_chunk_ids
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
        self._notify_hook("remove", source_id, current.kind)
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
        self._notify_hook("submit", source_id, current.kind)
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> KnowledgeSource:
        current = self.get_source(source_id)
        if current.status is KnowledgeSourceStatus.DELETED:
            raise InvalidStateError("Knowledge source is already deleted.")
        now = self._clock()
        with self._database.transaction() as connection:
            # expired_at 同步置空：表 CHECK 要求 expired 状态与 expired_at 同现。
            connection.execute(
                "UPDATE knowledge_sources SET status = 'deleted', deleted_at = ?, "
                "expired_at = NULL, updated_at = ? WHERE id = ?",
                (now, now, source_id),
            )
        self._notify_hook("remove", source_id, current.kind)
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

    # ---------- R5.9 分块 ----------

    def _replace_chunks(self, connection, source_id: str, content: str) -> None:
        """事务内重切分块：删旧建新（file 源检索单元 = 分块）。"""
        connection.execute(
            "DELETE FROM knowledge_chunks WHERE source_id = ?", (source_id,)
        )
        now = self._clock()
        for seq, chunk in enumerate(chunk_text(content, DEFAULT_CHUNK_MAX_CHARS)):
            connection.execute(
                "INSERT INTO knowledge_chunks (id, source_id, seq, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._id_factory("kchk"), source_id, seq, chunk, now),
            )

    def list_chunk_ids(self, source_id: str) -> List[str]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM knowledge_chunks WHERE source_id = ? ORDER BY seq",
                (source_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def get_chunk(self, chunk_id: str):
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Knowledge chunk not found: {chunk_id}")
        return row

    # ---------- 内部 ----------

    _MAX_FRAGMENTS = 24

    _SCOPE_QUERIES = {
        "source": (
            "SELECT ks.id AS ref_id, ks.title, ks.content AS body, "
            "NULL AS parent_id, NULL AS chunk_seq "
            "FROM knowledge_sources ks "
            "WHERE ks.status = 'active' AND ks.kind = 'note' AND ({match}){ws} "
            "UNION ALL "
            "SELECT kc.id AS ref_id, ks.title, kc.content AS body, "
            "ks.id AS parent_id, kc.seq AS chunk_seq "
            "FROM knowledge_chunks kc "
            "JOIN knowledge_sources ks ON ks.id = kc.source_id "
            "WHERE ks.status = 'active' AND ks.kind = 'file' AND ({match}){ws} "
            "UNION ALL "
            "SELECT ks.id AS ref_id, ks.title, ks.content AS body, "
            "NULL AS parent_id, NULL AS chunk_seq "
            "FROM knowledge_sources ks "
            "WHERE ks.status = 'active' AND ks.kind = 'file' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM knowledge_chunks kc WHERE kc.source_id = ks.id"
            ") AND ({match}){ws}"
        ),
        "memory": (
            "SELECT id AS ref_id, '记忆' AS title, content AS body, "
            "NULL AS parent_id, NULL AS chunk_seq "
            "FROM memories WHERE status = 'active' AND ({match})"
        ),
        "artifact": (
            "SELECT a.id AS ref_id, a.title, v.content AS body, "
            "NULL AS parent_id, NULL AS chunk_seq "
            "FROM artifacts a "
            "JOIN artifact_versions v ON v.artifact_id = a.id "
            "AND v.ordinal = a.current_version_ordinal "
            "WHERE a.status = 'active' AND ({match})"
        ),
        "conversation": (
            # 会话正文事实源 = v2 transcript entries（14 B1-i：v1 messages 不再
            # 镜像/索引；runtime 唯一 v2，历史 v1 无归档需求）。
            "SELECT e.id AS ref_id, c.title, "
            "json_extract(e.payload_json, '$.content') AS body, "
            "NULL AS parent_id, NULL AS chunk_seq "
            "FROM v2_transcript_entries e "
            "JOIN conversations c ON c.id = e.conversation_id "
            "WHERE c.kind = 'normal' AND c.status = 'active' "
            "AND e.type IN ('user_message', 'assistant_message') "
            "AND ({match})"
        ),
    }

    #: 分区哨兵：指代「通用/全局」（workspace_id IS NULL）。
    WORKSPACE_GENERAL = "general"

    _WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

    def search(
        self,
        query: str,
        scopes: Sequence[KnowledgeScope],
        limit: int = 8,
        workspace_id: Optional[str] = None,
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
        ws_clause = self._workspace_clause(workspace_id)
        grouped: Dict[KnowledgeScope, List[KnowledgeHit]] = {}
        with self._database.connect() as connection:
            for scope in scopes:
                template = self._SCOPE_QUERIES.get(scope.value)
                if template is None:
                    continue
                hits = self._search_scope(
                    connection, scope, template, fragments, limit,
                    query_vector, ws_clause,
                )
                if hits:
                    grouped[scope] = hits
        return grouped

    @classmethod
    def _workspace_clause(cls, workspace_id: Optional[str]) -> str:
        """R5.11 source 作用域可见性：工作区 ∪ 全局；None 不过滤。

        workspace_id 校验后以字面量拼入（模板参数已被 {match} 占满），
        只允许仓储生成的 [A-Za-z0-9_] id。
        """
        if workspace_id is None:
            return ""
        if workspace_id == cls.WORKSPACE_GENERAL:
            return " AND ks.workspace_id IS NULL"
        if not cls._WORKSPACE_ID_RE.match(workspace_id):
            raise ValidationError(f"Invalid workspace id: {workspace_id}")
        return (
            " AND (ks.workspace_id IS NULL OR "
            f"ks.workspace_id = '{workspace_id}')"
        )

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
            return connection.execute(
                template.format(match="1=1", ws="")
            ).fetchall()

    def _search_scope(
        self,
        connection,
        scope: KnowledgeScope,
        template: str,
        fragments: Sequence[str],
        limit: int,
        query_vector=None,
        ws_clause: str = "",
    ) -> List[KnowledgeHit]:
        clause = " OR ".join(["title LIKE ? OR body LIKE ?"] * len(fragments))
        single_params: List[str] = []
        for fragment in fragments:
            like = f"%{fragment}%"
            single_params.extend((like, like))
        # UNION 分支各含一个 {match}：参数按占位段数重复。
        params = single_params * template.count("{match}")
        rows = connection.execute(
            template.format(match=clause, ws=ws_clause), params
        ).fetchall()
        weight = self._scope_weights.get(scope, 1.0)
        best: Dict[str, tuple[int, str, str, str, Optional[str], Optional[int]]] = {}
        for row in rows:
            body = row["body"] or ""
            title = row["title"] or ""
            score = sum(
                body.count(fragment) + title.count(fragment)
                for fragment in fragments
            )
            current = best.get(row["ref_id"])
            if current is None or score > current[0]:
                best[row["ref_id"]] = (
                    score,
                    row["ref_id"],
                    title,
                    body,
                    row["parent_id"],
                    row["chunk_seq"],
                )
        if query_vector is not None and self._semantic_searcher is not None:
            semantic_scores, visible_map = self._semantic_scores(
                connection, scope, template, query_vector, ws_clause
            )
            if semantic_scores:
                return self._apply_feedback(
                    scope,
                    self._fuse_hits(
                        scope, weight, fragments, limit, best, semantic_scores,
                        visible_map,
                    ),
                )
        ordered = sorted(best.values(), key=lambda item: item[0], reverse=True)[:limit]
        hits = [
            KnowledgeHit(
                scope=scope,
                ref_id=ref_id,
                title=title,
                snippet=self._plain_snippet(body, fragments),
                score=raw_score * weight,
                source_id=parent_id,
                chunk_seq=chunk_seq,
            )
            for raw_score, ref_id, title, body, parent_id, chunk_seq in ordered
        ]
        return self._apply_feedback(scope, hits)

    def _apply_feedback(self, scope: KnowledgeScope, hits: List[KnowledgeHit]):
        """R5.10：source 作用域命中乘以引用反馈因子并重排（仅局部序）。"""
        provider = self._feedback_provider
        if provider is None or scope is not KnowledgeScope.SOURCE or not hits:
            return hits
        adjusted: List[KnowledgeHit] = []
        changed = False
        for hit in hits:
            try:
                factor = provider.factor(hit.ref_id, hit.source_id)
            except Exception:  # noqa: BLE001 反馈失败退回原序
                factor = 1.0
            if factor == 1.0:
                adjusted.append(hit)
                continue
            changed = True
            adjusted.append(replace(hit, score=hit.score * factor))
        if changed:
            adjusted.sort(key=lambda item: item.score, reverse=True)
        return adjusted

    def _semantic_scores(
        self, connection, scope: KnowledgeScope, template: str, query_vector,
        ws_clause: str = "",
    ) -> tuple[Dict[str, float], Dict[str, tuple[str, str, Optional[str], Optional[int]]]]:
        """向量召回：仅对当前可见行评分（临时/归档/过期永不进入检索）。

        返回（余弦分映射，可见行 title/body 映射）；评分失败返回空映射，
        调用方退回纯字面路径。
        """
        visible = connection.execute(
            template.format(match="1=1", ws=ws_clause)
        ).fetchall()
        ref_ids: List[str] = []
        visible_map: Dict[str, tuple[str, str, Optional[str], Optional[int]]] = {}
        for row in visible:
            ref_id = row["ref_id"]
            if ref_id not in visible_map:
                visible_map[ref_id] = (
                    row["title"] or "",
                    row["body"] or "",
                    row["parent_id"],
                    row["chunk_seq"],
                )
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
        best: Dict[str, tuple[int, str, str, str, Optional[str], Optional[int]]],
        semantic_scores: Dict[str, float],
        visible_map: Dict[str, tuple[str, str, Optional[str], Optional[int]]],
    ) -> List[KnowledgeHit]:
        """加权融合：字面分归一化后与余弦分线性组合（默认 0.4/0.6）。"""
        max_literal = max((item[0] for item in best.values()), default=0)
        scored: List[tuple[float, str, str, str, Optional[str], Optional[int]]] = []
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
                _, _, title, body, parent_id, chunk_seq = literal_entry
            else:
                title, body, parent_id, chunk_seq = visible_map.get(
                    ref_id, ("", "", None, None)
                )
            scored.append(
                (
                    fused,
                    ref_id,
                    title,
                    self._plain_snippet(body, fragments),
                    parent_id,
                    chunk_seq,
                )
            )
        ordered = sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
        return [
            KnowledgeHit(
                scope=scope,
                ref_id=ref_id,
                title=title,
                snippet=snippet,
                score=fused * weight,
                source_id=parent_id,
                chunk_seq=chunk_seq,
            )
            for fused, ref_id, title, snippet, parent_id, chunk_seq in ordered
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
            workspace_id=row["workspace_id"],
        )
