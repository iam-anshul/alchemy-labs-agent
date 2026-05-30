"""Build node trees for docs that have pages but no nodes yet.

Run from project root:
    PYTHONPATH=. python scripts/build_nodes.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from db import SessionLocal, utils  # noqa: E402
from db.models import Doc, Node  # noqa: E402
from shared import setup_logging  # noqa: E402
from tree import build_tree  # noqa: E402


async def main() -> None:
    setup_logging()
    with SessionLocal() as db:
        all_docs = list(db.scalars(select(Doc)))
        has_nodes = {
            row[0]
            for row in db.execute(select(Node.doc_id).distinct())
        }
        pending = [d for d in all_docs if d.doc_id not in has_nodes]

    if not pending:
        print("All docs already have nodes.")
        return

    print(f"{len(pending)} doc(s) to process\n")
    for doc in pending:
        print(f"→ {doc.doc_id}  {doc.title}")
        with SessionLocal() as db:
            root_id = await build_tree(doc.doc_id, doc.workspace_id, db)
        print(f"  root={root_id}\n")


if __name__ == "__main__":
    asyncio.run(main())
