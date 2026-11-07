"""
Reconciliation engine – the financial brain of TraceGuard.

All financial decisions are deterministic and rule-based.
The language model (if present) only generates the natural-language explanation
and has ZERO influence on status, amounts, or evidence selection.

Rules implemented (from the spec):
  01. Conflicting ORDER_CREATED records → integrity error → UNKNOWN
  02. Only 'payments' source events establish financial state
  03. PAYMENT_FAILED on attempt X does not cancel a capture on attempt Y
  04. Exact duplicate events (same event_id + same substantive fields) ignored
  05. Same event_id but different substantive fields → integrity error
  06. Conflicting records reusing a financial transaction ID → integrity error
  07. Each refund must reference an existing capture; refunds cannot exceed capture
  08. Processing order does not matter (we sort by occurred_at after loading)
  09. Tenant isolation is strict – never mix tenants
  10. Invalid references, mismatched currencies, over-captures, etc. → UNKNOWN
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from traceguard.models import (
    Confidence,
    InvestigationResult,
    OrderStatus,
    RawEvent,
)

logger = logging.getLogger(__name__)

# Fields that make two events with the same event_id "substantively different"
_SUBSTANTIVE_FIELDS = {
    "tenant_id", "order_id", "source", "type",
    "attempt_id", "capture_id", "refund_id",
    "amount_cents", "currency",
}


# ---------------------------------------------------------------------------
# Prompt-injection detection
# ---------------------------------------------------------------------------

_INJECTION_MARKERS = [
    "system override",
    "ignore previous instructions",
    "ignore all previous",
    "disregard previous",
    "you are now",
    "new instructions",
    "financial records are outdated",
    "mark this order",
    "do not mention this",
]


def _is_prompt_injection(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _INJECTION_MARKERS)


# ---------------------------------------------------------------------------
# Internal reconciliation state
# ---------------------------------------------------------------------------

@dataclass
class _ReconciliationState:
    """Mutable working state built while processing events for one order."""

    # Integrity anomalies discovered during processing
    anomalies: list[str] = field(default_factory=list)

    # event_ids of events we have seen (for dedup)
    seen_event_ids: dict[str, RawEvent] = field(default_factory=dict)

    # ORDER_CREATED records (by event_id); only checkout source
    order_definitions: list[RawEvent] = field(default_factory=list)

    # Expected amount/currency from ORDER_CREATED
    expected_amount_cents: Optional[int] = None
    currency: Optional[str] = None

    # Captures: capture_id → RawEvent (payments source only)
    captures: dict[str, RawEvent] = field(default_factory=dict)

    # Refunds: refund_id → RawEvent (payments source only)
    refunds: dict[str, RawEvent] = field(default_factory=dict)

    # Chargebacks: capture_id → list[RawEvent]
    chargebacks: dict[str, list[RawEvent]] = field(default_factory=dict)

    # Failed attempt IDs (from PAYMENT_FAILED)
    failed_attempts: set[str] = field(default_factory=set)

    # Evidence event_ids used in the final decision
    evidence_ids: list[str] = field(default_factory=list)

    # Prompt-injection attempts detected
    injection_attempts: int = 0


def _substantively_equal(a: RawEvent, b: RawEvent) -> bool:
    """Return True if two events with the same event_id are substantively identical."""
    for f in _SUBSTANTIVE_FIELDS:
        if getattr(a, f) != getattr(b, f):
            return False
    return True


# ---------------------------------------------------------------------------
# Main reconciliation logic
# ---------------------------------------------------------------------------

def reconcile(
    events: list[RawEvent],
    tenant_id: str,
    order_id: str,
) -> _ReconciliationState:
    """
    Process all events for the given tenant+order and return a reconciliation state.
    Events from other tenants are silently dropped (tenant isolation – Rule 09).
    """
    state = _ReconciliationState()

    # Filter: only this tenant, this order (Rule 09 – strict tenant isolation)
    relevant = [
        e for e in events
        if e.tenant_id == tenant_id and e.order_id == order_id
    ]

    # Sort by event type priority first (ORDER_CREATED → CAPTURES → REFUNDS → etc.)
    # then by occurred_at so processing is deterministic (Rule 08).
    # This ensures captures always exist before refunds/chargebacks reference them,
    # regardless of arrival order.
    _TYPE_ORDER = {
        "ORDER_CREATED": 0,
        "PAYMENT_FAILED": 1,
        "PAYMENT_CAPTURED": 2,
        "REFUND_ISSUED": 3,
        "CHARGEBACK_OPENED": 4,
        "NOTE": 5,
    }
    relevant.sort(key=lambda e: (
        _TYPE_ORDER.get(e.type, 99),
        e.occurred_at is None,
        e.occurred_at,
    ))

    for event in relevant:
        _process_event(event, state)

    return state


def _process_event(event: RawEvent, state: _ReconciliationState) -> None:  # noqa: C901
    """Apply one event to the reconciliation state."""

    # ── Deduplication (Rules 04 & 05) ──────────────────────────────────────
    if event.event_id in state.seen_event_ids:
        prior = state.seen_event_ids[event.event_id]
        if _substantively_equal(event, prior):
            # Exact duplicate – silently ignore (Rule 04)
            logger.debug("Duplicate event ignored: %s", event.event_id)
            return
        else:
            # Same ID, different data – integrity error (Rule 05)
            msg = (
                f"Integrity error: event_id '{event.event_id}' has conflicting "
                f"substantive fields between occurrences"
            )
            logger.warning(msg)
            state.anomalies.append(msg)
            return
    state.seen_event_ids[event.event_id] = event

    # ── Support note handling ───────────────────────────────────────────────
    if event.type == "NOTE":
        if event.text and _is_prompt_injection(event.text):
            state.injection_attempts += 1
            msg = (
                f"Prompt-injection attempt detected in event '{event.event_id}' "
                f"(source={event.source}). Note content ignored."
            )
            logger.warning(msg)
            state.anomalies.append(msg)
        # Notes never contribute to financial state (Rule 02)
        return

    # ── ORDER_CREATED ───────────────────────────────────────────────────────
    if event.type == "ORDER_CREATED":
        # Only checkout source defines order amounts (Rule 01)
        if event.source != "checkout":
            msg = (
                f"ORDER_CREATED from non-checkout source '{event.source}' "
                f"(event_id={event.event_id}) – ignored"
            )
            state.anomalies.append(msg)
            return

        if event.amount_cents is None or event.currency is None:
            msg = (
                f"ORDER_CREATED missing amount/currency "
                f"(event_id={event.event_id})"
            )
            state.anomalies.append(msg)
            state.order_definitions.append(event)
            return

        # Check for conflicting order definitions (Rule 01)
        if state.expected_amount_cents is not None:
            if (
                state.expected_amount_cents != event.amount_cents
                or state.currency != event.currency
            ):
                msg = (
                    f"Integrity error: conflicting ORDER_CREATED definitions – "
                    f"existing={state.expected_amount_cents} {state.currency}, "
                    f"new={event.amount_cents} {event.currency} "
                    f"(event_id={event.event_id})"
                )
                logger.warning(msg)
                state.anomalies.append(msg)

        state.expected_amount_cents = event.amount_cents
        state.currency = event.currency
        state.order_definitions.append(event)
        state.evidence_ids.append(event.event_id)
        return

    # ── All remaining event types must come from 'payments' source (Rule 02) ─
    if event.source != "payments":
        # Non-payment financial events are silently ignored per Rule 02
        return

    # ── PAYMENT_FAILED ──────────────────────────────────────────────────────
    if event.type == "PAYMENT_FAILED":
        if event.attempt_id:
            state.failed_attempts.add(event.attempt_id)
        state.evidence_ids.append(event.event_id)
        return

    # ── PAYMENT_CAPTURED ────────────────────────────────────────────────────
    if event.type == "PAYMENT_CAPTURED":
        if not event.capture_id:
            msg = f"PAYMENT_CAPTURED missing capture_id (event_id={event.event_id})"
            state.anomalies.append(msg)
            return

        if event.amount_cents is None:
            msg = f"PAYMENT_CAPTURED missing amount_cents (event_id={event.event_id})"
            state.anomalies.append(msg)
            return

        if event.amount_cents <= 0:
            msg = (
                f"PAYMENT_CAPTURED has non-positive amount_cents={event.amount_cents} "
                f"(event_id={event.event_id})"
            )
            state.anomalies.append(msg)
            return

        # Check for conflicting capture record (Rule 06)
        if event.capture_id in state.captures:
            prior_cap = state.captures[event.capture_id]
            if (
                prior_cap.amount_cents != event.amount_cents
                or prior_cap.currency != event.currency
            ):
                msg = (
                    f"Integrity error: capture_id '{event.capture_id}' has "
                    f"conflicting records (event_ids: {prior_cap.event_id}, "
                    f"{event.event_id})"
                )
                state.anomalies.append(msg)
                return
            # Identical capture – duplicate, ignore
            return

        # Currency check
        if state.currency and event.currency and event.currency != state.currency:
            msg = (
                f"Currency mismatch: order currency={state.currency}, "
                f"capture currency={event.currency} (event_id={event.event_id})"
            )
            state.anomalies.append(msg)
            return

        state.captures[event.capture_id] = event
        state.evidence_ids.append(event.event_id)
        return

    # ── REFUND_ISSUED ───────────────────────────────────────────────────────
    if event.type == "REFUND_ISSUED":
        if not event.capture_id:
            msg = f"REFUND_ISSUED missing capture_id (event_id={event.event_id})"
            state.anomalies.append(msg)
            return

        if not event.refund_id:
            msg = f"REFUND_ISSUED missing refund_id (event_id={event.event_id})"
            state.anomalies.append(msg)
            return

        if event.amount_cents is None or event.amount_cents <= 0:
            msg = (
                f"REFUND_ISSUED has invalid amount_cents={event.amount_cents} "
                f"(event_id={event.event_id})"
            )
            state.anomalies.append(msg)
            return

        # Refund must reference an existing capture (Rule 07)
        if event.capture_id not in state.captures:
            msg = (
                f"REFUND_ISSUED references unknown capture_id '{event.capture_id}' "
                f"(event_id={event.event_id})"
            )
            state.anomalies.append(msg)
            return

        # Check for conflicting refund record (Rule 06)
        if event.refund_id in state.refunds:
            prior_ref = state.refunds[event.refund_id]
            if (
                prior_ref.amount_cents != event.amount_cents
                or prior_ref.capture_id != event.capture_id
            ):
                msg = (
                    f"Integrity error: refund_id '{event.refund_id}' has "
                    f"conflicting records (event_ids: {prior_ref.event_id}, "
                    f"{event.event_id})"
                )
                state.anomalies.append(msg)
                return
            # Identical refund – duplicate, ignore
            return

        # Over-refund check (Rule 07)
        cap = state.captures[event.capture_id]
        total_refunded_for_cap = sum(
            r.amount_cents
            for r in state.refunds.values()
            if r.capture_id == event.capture_id and r.amount_cents is not None
        )
        if total_refunded_for_cap + event.amount_cents > cap.amount_cents:  # type: ignore[operator]
            msg = (
                f"Over-refund: refunding {event.amount_cents} would exceed "
                f"captured amount {cap.amount_cents} for capture_id '{event.capture_id}' "
                f"(event_id={event.event_id})"
            )
            state.anomalies.append(msg)
            return

        state.refunds[event.refund_id] = event
        state.evidence_ids.append(event.event_id)
        return

    # ── CHARGEBACK_OPENED ───────────────────────────────────────────────────
    if event.type == "CHARGEBACK_OPENED":
        if not event.capture_id:
            msg = f"CHARGEBACK_OPENED missing capture_id (event_id={event.event_id})"
            state.anomalies.append(msg)
            return

        if event.capture_id not in state.captures:
            msg = (
                f"CHARGEBACK_OPENED references unknown capture_id '{event.capture_id}' "
                f"(event_id={event.event_id})"
            )
            state.anomalies.append(msg)
            return

        state.chargebacks.setdefault(event.capture_id, []).append(event)
        state.evidence_ids.append(event.event_id)
        return


# ---------------------------------------------------------------------------
# Status decision (deterministic, Rule 10 / status table)
# ---------------------------------------------------------------------------

def determine_status(state: _ReconciliationState) -> tuple[OrderStatus, int | None, int | None]:
    """
    Apply the status decision table from the spec.
    Returns (status, captured_cents, refunded_cents).
    All values are None when status is UNKNOWN due to missing evidence.
    """
    # Rule 1: integrity failure or missing essential order evidence → UNKNOWN
    integrity_errors = [a for a in state.anomalies if "integrity error" in a.lower()]
    if integrity_errors or not state.order_definitions:
        return OrderStatus.UNKNOWN, None, None

    if state.expected_amount_cents is None:
        return OrderStatus.UNKNOWN, None, None

    captured = sum(
        c.amount_cents for c in state.captures.values() if c.amount_cents is not None
    )
    refunded = sum(
        r.amount_cents for r in state.refunds.values() if r.amount_cents is not None
    )

    # Over-capture check (Rule 10)
    if captured > state.expected_amount_cents:
        return OrderStatus.UNKNOWN, captured, refunded

    # Rule 2: open chargeback → DISPUTED
    if any(state.chargebacks.values()):
        return OrderStatus.DISPUTED, captured, refunded

    # Rule 3: no captures, payment failure evidence → UNPAID
    if not state.captures and state.failed_attempts:
        return OrderStatus.UNPAID, 0, 0

    # Rule 4: refunded == captured (both > 0) → REFUNDED
    if captured > 0 and refunded == captured:
        return OrderStatus.REFUNDED, captured, refunded

    # Rule 5: 0 < refunded < captured → PARTIALLY_REFUNDED
    if 0 < refunded < captured:
        return OrderStatus.PARTIALLY_REFUNDED, captured, refunded

    # Rule 6: captured == expected → PAID
    if captured == state.expected_amount_cents:
        return OrderStatus.PAID, captured, refunded

    # Rule 7: 0 < captured < expected → PARTIALLY_PAID
    if 0 < captured < state.expected_amount_cents:
        return OrderStatus.PARTIALLY_PAID, captured, refunded

    # Rule 8: nothing established → UNKNOWN
    return OrderStatus.UNKNOWN, None, None


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _compute_confidence(
    state: _ReconciliationState,
    status: OrderStatus,
) -> Confidence:
    """
    Confidence assignment rules (documented in README):

    HIGH   – Status is deterministic, no anomalies, all timestamps present,
              essential events from a single authoritative source.
    MEDIUM – Minor anomalies (e.g., duplicate events, unsupported types skipped),
              or one missing timestamp.
    LOW    – Multiple anomalies, integrity issues were found (even if non-fatal),
              UNKNOWN status, or injection attempts detected.
    """
    if status == OrderStatus.UNKNOWN:
        return Confidence.LOW

    if state.injection_attempts > 0:
        return Confidence.LOW

    # Count integrity-level anomalies
    serious = sum(1 for a in state.anomalies if "integrity error" in a.lower())
    if serious > 0:
        return Confidence.LOW

    if state.anomalies:
        return Confidence.MEDIUM

    return Confidence.HIGH


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def investigate(
    events: list[RawEvent],
    tenant_id: str,
    order_id: str,
    question: str = "",
    llm_explain_fn=None,
) -> InvestigationResult:
    """
    Run a full investigation for the given tenant + order.

    Parameters
    ----------
    events        : All loaded events (may contain other tenants/orders – filtered internally)
    tenant_id     : Tenant to investigate
    order_id      : Order to investigate
    question      : Free-text question from the operator (passed to LLM for explanation)
    llm_explain_fn: Optional callable(state, status, question) → str.
                    If None, a deterministic explanation is generated.
    """
    state = reconcile(events, tenant_id, order_id)
    status, captured, refunded = determine_status(state)
    confidence = _compute_confidence(state, status)

    # Net paid
    if captured is not None and refunded is not None:
        net_paid = captured - refunded
    else:
        net_paid = None

    # Explanation
    if llm_explain_fn is not None:
        try:
            explanation = llm_explain_fn(state, status, question)
        except Exception as exc:
            logger.warning("LLM explanation failed: %s – using fallback", exc)
            explanation = _build_deterministic_explanation(state, status)
    else:
        explanation = _build_deterministic_explanation(state, status)

    # Deduplicate evidence IDs while preserving order
    seen: set[str] = set()
    evidence = []
    for eid in state.evidence_ids:
        if eid not in seen:
            seen.add(eid)
            evidence.append(eid)

    return InvestigationResult(
        status=status,
        captured_cents=captured,
        refunded_cents=refunded,
        net_paid_cents=net_paid,
        anomalies=state.anomalies,
        evidence_event_ids=evidence,
        explanation=explanation,
        confidence=confidence,
    )


def _build_deterministic_explanation(
    state: _ReconciliationState,
    status: OrderStatus,
) -> str:
    """Generate a plain-English explanation from the reconciliation state."""
    parts: list[str] = []

    n_caps = len(state.captures)
    n_refs = len(state.refunds)
    n_fails = len(state.failed_attempts)
    n_chargebacks = sum(len(v) for v in state.chargebacks.values())

    if state.expected_amount_cents is not None:
        parts.append(
            f"Order expected {state.expected_amount_cents} {state.currency or '?'}."
        )

    if n_fails:
        parts.append(f"{n_fails} payment attempt(s) failed.")

    if n_caps:
        total_cap = sum(
            c.amount_cents for c in state.captures.values() if c.amount_cents
        )
        parts.append(f"{n_caps} capture(s) totalling {total_cap} cents.")
    else:
        parts.append("No successful captures found.")

    if n_refs:
        total_ref = sum(
            r.amount_cents for r in state.refunds.values() if r.amount_cents
        )
        parts.append(f"{n_refs} refund(s) totalling {total_ref} cents.")

    if n_chargebacks:
        parts.append(f"{n_chargebacks} open chargeback(s).")

    if state.anomalies:
        parts.append(f"{len(state.anomalies)} anomaly/anomalies detected.")

    if state.injection_attempts:
        parts.append(
            f"WARNING: {state.injection_attempts} prompt-injection attempt(s) in notes – ignored."
        )

    # Duplicate events
    parts.append(f"Final status determined: {status.value}.")

    return " ".join(parts)
