"""Draft a report from documents in the database.

Usage:
    python scripts/draft.py "report brief here"
    python scripts/draft.py --workspace ws_default --length deep "brief"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from report import draft_report  # noqa: E402
from shared import setup_logging  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    result = await draft_report(
        workspace_id=args.workspace,
        user_id=args.user,
        brief=args.brief,
        doc_ids=args.docs or None,
        target_length=args.length,
    )

    bar = "=" * 80
    print(f"\n{bar}\nBRIEF: {result.brief}\n{bar}\n")
    print(
        f"## Report  (sections={result.n_sections}, words={result.n_words}, "
        f"hops={result.n_hops}, latency={result.latency_ms} ms)\n"
    )
    print(f"Title: {result.outline.title}\n")
    if result.output_path:
        print(f"Saved to: {result.output_path}\n")
    print(result.draft_md[:4000])
    if len(result.draft_md) > 4000:
        print("\n… (truncated)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Draft a report from workspace documents.")
    p.add_argument("brief", help="Report brief / topic")
    p.add_argument("--workspace", default="ws_default", help="workspace_id (default: ws_default)")
    p.add_argument("--user", default="cli_user", help="user_id")
    p.add_argument("--docs", nargs="*", help="restrict to specific doc_ids")
    p.add_argument(
        "--length",
        choices=("brief", "standard", "deep"),
        default="standard",
        help="target report length (default: standard)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG logs")
    args = p.parse_args()

    setup_logging(args.verbose)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
