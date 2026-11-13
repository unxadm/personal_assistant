"""Embedding store + semantic search.

Vectors are float32 blobs in the ``embeddings`` table. Search uses sqlite-vec's
``vec_distance_cosine`` when the extension is loaded and an in-process scan otherwise.
Rows are tagged with the embedding model, and only rows from the currently configured
model are searched (vectors from different models are not comparable).
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, text

from ..core.db import Database
from ..core.models import Embedding, utcnow
from ..llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

MAX_CHARS = 6000  # keep chunks comfortably inside embedding-model context limits


@dataclass
class Hit:
    kind: str
    ref_id: str
    text: str
    distance: float
    meta: dict[str, Any]


def pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


class VectorStore:
    def __init__(self, db: Database, llm: LLMClient):
        self._db = db
        self._llm = llm

    @property
    def enabled(self) -> bool:
        return self._llm.embeddings_model is not None

    async def upsert_many(self, entries: list[tuple[str, str, str, dict[str, Any]]]) -> bool:
        """Embed and store ``(kind, ref_id, text, meta)`` entries. Returns False on failure.

        Embedding failures are logged, never raised: search is an enhancement and must not
        break the action that triggered indexing.
        """
        entries = [e for e in entries if e[2].strip()]
        if not self.enabled or not entries:
            return False
        model = self._llm.embeddings_model
        try:
            vectors = await self._llm.embed([e[2][:MAX_CHARS] for e in entries])
        except LLMError as e:
            log.warning("embedding failed, skipping index update: %s", e)
            return False
        async with self._db.session() as s:
            for (kind, ref_id, body, meta), vector in zip(entries, vectors, strict=True):
                row = await s.scalar(
                    select(Embedding).where(Embedding.kind == kind, Embedding.ref_id == ref_id)
                )
                if row is None:
                    row = Embedding(kind=kind, ref_id=ref_id)
                    s.add(row)
                row.text, row.model, row.dim = body[:MAX_CHARS], model, len(vector)
                row.vector, row.meta, row.created_at = pack(vector), meta, utcnow()
            await s.commit()
        return True

    async def upsert(self, kind: str, ref_id: str, body: str, meta: dict[str, Any] | None = None) -> bool:
        return await self.upsert_many([(kind, ref_id, body, meta or {})])

    async def delete(self, kind: str, ref_ids: list[str]) -> None:
        if not ref_ids:
            return
        async with self._db.session() as s:
            await s.execute(
                delete(Embedding).where(Embedding.kind == kind, Embedding.ref_id.in_(ref_ids))
            )
            await s.commit()

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        kinds: list[str] | None = None,
        max_distance: float | None = None,
    ) -> list[Hit]:
        if not self.enabled or not query.strip():
            return []
        model = self._llm.embeddings_model
        try:
            [qvec] = await self._llm.embed([query[:MAX_CHARS]])
        except LLMError as e:
            log.warning("embedding failed, semantic search skipped: %s", e)
            return []

        async with self._db.session() as s:
            # Touch the connection first so we know whether sqlite-vec loaded on it.
            await s.connection()
            if self._db.vec_available:
                sql = (
                    "SELECT kind, ref_id, text, meta, vec_distance_cosine(vector, :q) AS distance "
                    "FROM embeddings WHERE model = :model AND dim = :dim"
                )
                params: dict[str, Any] = {"q": pack(qvec), "model": model, "dim": len(qvec)}
                if kinds:
                    names = [f"k{i}" for i in range(len(kinds))]
                    sql += f" AND kind IN ({', '.join(':' + n for n in names)})"
                    params.update(dict(zip(names, kinds, strict=True)))
                sql += " ORDER BY distance LIMIT :k"
                params["k"] = k
                rows = (await s.execute(text(sql), params)).all()
                hits = [
                    Hit(r.kind, r.ref_id, r.text, float(r.distance), _json(r.meta)) for r in rows
                ]
            else:
                stmt = select(Embedding).where(Embedding.model == model, Embedding.dim == len(qvec))
                if kinds:
                    stmt = stmt.where(Embedding.kind.in_(kinds))
                rows = (await s.scalars(stmt)).all()
                hits = sorted(
                    (
                        Hit(r.kind, r.ref_id, r.text, cosine_distance(qvec, unpack(r.vector)), r.meta or {})
                        for r in rows
                    ),
                    key=lambda h: h.distance,
                )[:k]
        if max_distance is not None:
            hits = [h for h in hits if h.distance <= max_distance]
        return hits


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes):
        import json

        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
