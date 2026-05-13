"""Postgres smoke test — exercise schema, models, and FTS end-to-end.

Run after ``alembic upgrade head`` against a Postgres database. Verifies:

  - Models insert/select cleanly with JSONB columns
  - ``ensure_fts_index()`` creates the GIN expression index
  - ``search_messages()`` returns hits via Postgres ``plainto_tsquery``

Usage:

    KORPHA_DB_URL="postgresql+psycopg://postgres:test@localhost:5433/korpha" \\
        uv run python scripts/smoke_postgres.py

Exit code 0 = pass, non-zero = fail. Print findings inline.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlmodel import Session

from korpha.business.model import Business
from korpha.cofounder.fts import (
    PG_FTS_INDEX,
    ensure_fts_index,
    search_messages,
)
from korpha.cofounder.model import (
    AgentRole,
    Message,
    MessageSenderType,
    RoleType,
    Thread,
    ThreadPlatform,
    ThreadStatus,
)
from korpha.identity.model import Founder


def main() -> int:
    db_url = os.environ.get("KORPHA_DB_URL")
    if not db_url or "postgres" not in db_url:
        print("FAIL: KORPHA_DB_URL must be set to a postgres URL")
        return 2

    print(f"Connecting to {db_url}")
    engine = create_engine(db_url, echo=False)

    with Session(engine) as session:
        # ---- Phase 1: insert hierarchy with JSONB ----
        print("\n=== Phase 1: insert with JSONB columns ===")
        founder = Founder(
            id=uuid4(),
            email="smoke@korpha.com",
            display_name="Smoke Test",
            preferences={"timezone": "UTC", "tone": "direct"},
        )
        session.add(founder)
        session.commit()

        business = Business(
            id=uuid4(),
            founder_id=founder.id,
            name="Smoke Co",
            founder_brief={"problem": "test the schema", "audience": "us"},
        )
        session.add(business)
        session.commit()

        agent_role = AgentRole(
            id=uuid4(),
            business_id=business.id,
            role_type=RoleType.CEO,
            title="CEO",
            specialty=None,
        )
        session.add(agent_role)
        session.commit()

        thread = Thread(
            id=uuid4(),
            business_id=business.id,
            founder_id=founder.id,
            agent_role_id=agent_role.id,
            platform=ThreadPlatform.WEB,
            topic="pricing",
            status=ThreadStatus.ACTIVE,
        )
        session.add(thread)
        session.commit()

        messages = [
            Message(
                id=uuid4(),
                thread_id=thread.id,
                sender_type=MessageSenderType.FOUNDER,
                content="The pricing model needs to be decided by Friday.",
                created_at=datetime.now(UTC),
                attachments={"kind": "url", "value": "https://example.com"},
            ),
            Message(
                id=uuid4(),
                thread_id=thread.id,
                sender_type=MessageSenderType.AGENT,
                sender_role_id=agent_role.id,
                content="Recommend $29/month for the basic tier.",
                created_at=datetime.now(UTC),
            ),
            Message(
                id=uuid4(),
                thread_id=thread.id,
                sender_type=MessageSenderType.FOUNDER,
                content="What about the support team capacity?",
                created_at=datetime.now(UTC),
            ),
        ]
        for m in messages:
            session.add(m)
        session.commit()
        print(f"  inserted founder + business + agent_role + thread + {len(messages)} messages")

        # Verify JSONB queries work (Postgres-specific operator)
        rows = session.execute(
            text(
                "SELECT name, founder_brief->>'audience' AS audience "
                "FROM business WHERE id = :id"
            ),
            {"id": str(business.id)},
        ).all()
        assert rows[0].audience == "us", f"JSONB ->> path failed: {rows}"
        print(f"  JSONB path query works: {rows[0].name} → audience='{rows[0].audience}'")

        # ---- Phase 2: ensure FTS index ----
        print("\n=== Phase 2: FTS index creation ===")
        ok = ensure_fts_index(session)
        session.commit()
        if not ok:
            print("FAIL: ensure_fts_index returned False on Postgres")
            return 1
        idx = session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'message' AND indexname = :name"
            ),
            {"name": PG_FTS_INDEX},
        ).first()
        if idx is None:
            print(f"FAIL: index {PG_FTS_INDEX} not found after ensure_fts_index")
            return 1
        print(f"  index {PG_FTS_INDEX} created (GIN expression on to_tsvector)")

        # ---- Phase 3: full-text search ----
        print("\n=== Phase 3: search_messages ===")
        hits = search_messages(
            session,
            query="pricing model",
            business_id=business.id,
            founder_id=founder.id,
            limit=5,
        )
        if not hits:
            print("FAIL: search_messages returned no hits for 'pricing model'")
            return 1
        print(f"  query 'pricing model' → {len(hits)} hits")
        for h in hits:
            print(f"    rank={h.rank:.4f}  {h.sender_type.value}: {h.content[:60]!r}")

        # Filter check: another founder must see no hits
        other_founder = Founder(
            id=uuid4(),
            email="other@korpha.com",
            display_name="Other",
            preferences={},
        )
        session.add(other_founder)
        session.commit()
        no_hits = search_messages(
            session,
            query="pricing model",
            business_id=business.id,
            founder_id=other_founder.id,
            limit=5,
        )
        assert no_hits == [], (
            f"FAIL: cross-founder leak — got {len(no_hits)} hits"
        )
        print("  cross-founder isolation verified (no leak)")

        # Empty query
        empty = search_messages(
            session,
            query="",
            business_id=business.id,
            founder_id=founder.id,
        )
        assert empty == [], f"FAIL: empty query returned hits: {empty}"
        print("  empty query → [] (correct)")

    print("\nALL POSTGRES SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
