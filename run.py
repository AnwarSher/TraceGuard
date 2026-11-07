"""
Command-line interface for TraceGuard.

Usage:
    python run.py --events data/events.jsonl --tenant T1 --order O-201 \
                  --question "Was this order paid?"

Flags:
    --events   Path to JSONL events file (required)
    --tenant   Tenant ID to investigate (required)
    --order    Order ID to investigate (required)
    --question Free-text question for the explanation (optional)
    --llm      Enable LLM explanation if OPENAI_API_KEY is set (flag)
    --verbose  Enable debug logging (flag)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running as `python run.py` from the project root
sys.path.insert(0, str(Path(__file__).parent))

from traceguard.engine import investigate
from traceguard.parser import load_events


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="traceguard",
        description="TraceGuard – Payment Incident Investigation System",
    )
    parser.add_argument("--events", required=True, help="Path to JSONL events file")
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--order", required=True, help="Order ID")
    parser.add_argument(
        "--question",
        default="What is the financial status of this order?",
        help="Question for explanation",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Enable LLM explanation (requires OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Logging setup
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    # Load events
    try:
        events = list(load_events(args.events))
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    # Optional LLM
    llm_fn = None
    if args.llm:
        try:
            from traceguard.llm import llm_explain
            llm_fn = llm_explain
        except Exception:
            pass  # falls back to deterministic explanation automatically

    # Run investigation
    result = investigate(
        events=events,
        tenant_id=args.tenant,
        order_id=args.order,
        question=args.question,
        llm_explain_fn=llm_fn,
    )

    # Output
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
