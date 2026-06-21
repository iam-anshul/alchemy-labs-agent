"""Ask the reasoning agent a question over the documents in the database.

Usage:
    python scripts/ask.py "your query here"
    python scripts/ask.py --workspace ws_default --user tejesh "your query"
    python scripts/ask.py --docs doc_14b0f97d69b5 "what is Berkshire's float?"
    python scripts/ask.py -v "..."   # verbose: show routing + findings
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.agents.agent import answer_query  # noqa: E402
from app.core.shared import setup_logging  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    result = await answer_query(
        workspace_id=args.workspace,
        user_id=args.user,
        query=args.query,
        doc_ids=args.docs or None,
    )

    bar = "=" * 80
    print(f"\n{bar}\nQUERY: {result.query}\n{bar}\n")

    print(f"## Answer  (confidence={result.confidence}, hops={result.n_hops}, latency={result.latency_ms} ms)\n")
    print(result.answer)
    print()

    if result.citations:
        print("## Citations")
        for c in result.citations:
            print(f"  - {c.doc_title}  pages {c.pages}")
        print()

    if args.verbose:
        if result.hops:
            print(f"## Hop trace  ({result.n_hops} hop{'s' if result.n_hops != 1 else ''})")
            for h in result.hops:
                print(f"\n  ── Hop {h.hop}  question: {h.question}")
                print(f"     confidence={h.confidence}  needs_more={h.needs_more}")
                for pt in h.page_targets:
                    print(f"     +page:  {pt.doc_id}  p{pt.start_page}-p{pt.end_page}  ({pt.reason})")
                for tt in h.table_targets:
                    print(f"     +table: {tt.table_id}  (doc {tt.doc_id})  ({tt.reason})")
                for f in h.table_findings:
                    preview = f.finding[:200] + ("…" if len(f.finding) > 200 else "")
                    print(f"     finding({f.table_id}): {preview}")
                for fq in h.follow_up_questions:
                    print(f"     follow_up: {fq}")
            print()

        print("## Cumulative page_targets")
        for t in result.page_targets:
            print(f"  - {t.doc_id}  p{t.start_page}-p{t.end_page}  reason: {t.reason}")
        if result.table_targets:
            print("\n## Cumulative table_targets")
            for t in result.table_targets:
                print(f"  - {t.table_id}  (doc {t.doc_id})  reason: {t.reason}")
        if result.table_findings:
            print("\n## Cumulative excel findings")
            for f in result.table_findings:
                print(f"  - Table {f.table_id} (doc {f.doc_id})")
                print(f"      {f.finding}")
        print()

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Ask the reasoning agent a question.")
    p.add_argument("query", help="The question to ask")
    p.add_argument("--workspace", default="ws_default", help="workspace_id (default: ws_default)")
    p.add_argument("--user", default="cli_user", help="user_id")
    p.add_argument("--docs", nargs="*", help="restrict to specific doc_ids")
    p.add_argument("-v", "--verbose", action="store_true", help="show routing + findings + DEBUG logs")
    args = p.parse_args()

    setup_logging(args.verbose)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
