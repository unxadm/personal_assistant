"""Long-term memory: facts and preferences about the user (viewable, editable, deletable)."""

from __future__ import annotations

from sqlalchemy import select

from ..core.db import Database
from ..core.models import MemoryFact, utcnow
from .vectors import VectorStore

KIND = "fact"


class MemoryService:
    def __init__(self, db: Database, vectors: VectorStore):
        self._db = db
        self._vectors = vectors

    async def list_facts(self, limit: int | None = None) -> list[MemoryFact]:
        stmt = select(MemoryFact).order_by(MemoryFact.updated_at.desc(), MemoryFact.id.desc())
        if limit:
            stmt = stmt.limit(limit)
        async with self._db.session() as s:
            return list((await s.scalars(stmt)).all())

    async def add_fact(
        self, content: str, category: str = "general", source: str = "user"
    ) -> MemoryFact:
        content = content.strip()
        if not content:
            raise ValueError("fact content is empty")
        async with self._db.session() as s:
            existing = await s.scalar(select(MemoryFact).where(MemoryFact.content == content))
            if existing is not None:
                return existing
            fact = MemoryFact(content=content, category=category or "general", source=source)
            s.add(fact)
            await s.commit()
        await self._vectors.upsert(KIND, str(fact.id), content, {"category": fact.category})
        return fact

    async def update_fact(
        self, fact_id: int, content: str | None = None, category: str | None = None
    ) -> MemoryFact | None:
        async with self._db.session() as s:
            fact = await s.get(MemoryFact, fact_id)
            if fact is None:
                return None
            if content is not None:
                if not content.strip():
                    raise ValueError("fact content is empty")
                fact.content = content.strip()
            if category is not None:
                fact.category = category or "general"
            fact.updated_at = utcnow()
            await s.commit()
        await self._vectors.upsert(KIND, str(fact.id), fact.content, {"category": fact.category})
        return fact

    async def delete_fact(self, fact_id: int) -> bool:
        async with self._db.session() as s:
            fact = await s.get(MemoryFact, fact_id)
            if fact is None:
                return False
            await s.delete(fact)
            await s.commit()
        await self._vectors.delete(KIND, [str(fact_id)])
        return True
