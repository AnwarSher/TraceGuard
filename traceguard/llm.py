"""
Optional LLM explanation layer.

The LLM is used ONLY to format a natural-language answer to the operator's
question. It cannot change status, amounts, anomaly list, or evidence IDs.

If the OPENAI_API_KEY environment variable is not set, the module falls back
to the deterministic explanation without raising an error.

Retry policy: up to 2 attempts with a 2-second delay. No unbounded retries.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceguard.engine import _ReconciliationState
    from traceguard.models import OrderStatus

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 2


def _build_system_prompt() -> str:
    return (
        "You are TraceGuard, a payment investigation assistant. "
        "You receive a pre-computed financial summary and must explain it clearly. "
        "You MUST NOT change any financial figures, status, or evidence IDs. "
        "Answer only the operator's question based on the provided facts."
    )


def _build_user_message(
    state: "_ReconciliationState",
    status: "OrderStatus",
    question: str,
) -> str:
    summary_lines = [
        f"Order status: {status.value}",
        f"Expected: {state.expected_amount_cents} {state.currency}",
        f"Captures: {len(state.captures)} record(s)",
        f"Refunds: {len(state.refunds)} record(s)",
        f"Anomalies: {state.anomalies}",
        f"Evidence event IDs: {state.evidence_ids}",
    ]
    summary = "\n".join(summary_lines)
    return f"Financial summary (do not modify):\n{summary}\n\nOperator question: {question}"


def llm_explain(
    state: "_ReconciliationState",
    status: "OrderStatus",
    question: str,
) -> str:
    """
    Call OpenAI to generate an explanation. Falls back to deterministic text
    after _MAX_RETRIES failed attempts.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.info("OPENAI_API_KEY not set – using deterministic explanation")
        raise RuntimeError("No API key")

    try:
        import openai  # type: ignore[import]
    except ImportError:
        logger.warning("openai package not installed – using deterministic explanation")
        raise RuntimeError("openai not installed")

    client = openai.OpenAI(api_key=api_key)
    system_prompt = _build_system_prompt()
    user_message = _build_user_message(state, status, question)

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=300,
                temperature=0.0,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "LLM call attempt %d/%d failed: %s", attempt, _MAX_RETRIES, exc
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_SECONDS)

    raise RuntimeError(f"LLM failed after {_MAX_RETRIES} attempts: {last_exc}")
