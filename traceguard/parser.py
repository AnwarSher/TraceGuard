"""
Event parser: reads JSONL input, validates schema, normalises timestamps,
and flags malformed or unsupported records.

Security note: text fields (support notes) are stored as opaque strings and
are NEVER evaluated or used to influence financial state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from traceguard.models import RawEvent

logger = logging.getLogger(__name__)

# Fields required on every record
_REQUIRED_FIELDS = {"event_id", "tenant_id", "order_id", "source", "type"}

# Supported event types
_SUPPORTED_TYPES = {
    "ORDER_CREATED",
    "PAYMENT_FAILED",
    "PAYMENT_CAPTURED",
    "REFUND_ISSUED",
    "CHARGEBACK_OPENED",
    "NOTE",
}


def _parse_timestamp(value: object, field_name: str) -> datetime | None:
    """Parse an ISO-8601 timestamp. Returns None on failure (logged as warning)."""
    if value is None:
        return None
    if not isinstance(value, str):
        logger.warning("Field %s is not a string: %r", field_name, value)
        return None
    try:
        # Python 3.11+ supports Z suffix natively; handle older formats too
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Normalise to UTC
        return ts.astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid timestamp in %s=%r: %s", field_name, value, exc)
        return None


def _sanitise_note(text: object) -> str | None:
    """
    Return the text field as a plain string with no interpretation.
    Any attempt at prompt injection is preserved literally as evidence of the
    attack; it cannot influence financial state.
    """
    if text is None:
        return None
    return str(text)  # always a dumb string – never evaluated


def parse_line(line: str, line_number: int) -> RawEvent | None:
    """
    Parse one JSONL line into a RawEvent.
    Returns None for blank lines or lines that cannot be parsed.
    Raises nothing – all errors are logged and skipped.
    """
    line = line.strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        logger.warning("Line %d: JSON parse error: %s", line_number, exc)
        return None

    if not isinstance(obj, dict):
        logger.warning("Line %d: expected JSON object, got %s", line_number, type(obj).__name__)
        return None

    # Check required fields
    missing = _REQUIRED_FIELDS - obj.keys()
    if missing:
        logger.warning("Line %d: missing required fields %s", line_number, missing)
        return None

    event_type = obj.get("type", "")
    if event_type not in _SUPPORTED_TYPES:
        logger.warning(
            "Line %d: unsupported event type %r (event_id=%s) – skipped",
            line_number,
            event_type,
            obj.get("event_id"),
        )
        return None

    # Validate amount_cents is a non-negative integer when present
    amount_cents = obj.get("amount_cents")
    if amount_cents is not None:
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
            logger.warning(
                "Line %d: amount_cents must be integer, got %r – treating as None",
                line_number,
                amount_cents,
            )
            amount_cents = None
        elif amount_cents < 0:
            logger.warning(
                "Line %d: negative amount_cents=%d – treating as None",
                line_number,
                amount_cents,
            )
            amount_cents = None

    return RawEvent(
        event_id=str(obj["event_id"]),
        tenant_id=str(obj["tenant_id"]),
        order_id=str(obj["order_id"]),
        source=str(obj["source"]),
        type=event_type,
        occurred_at=_parse_timestamp(obj.get("occurred_at"), "occurred_at"),
        ingested_at=_parse_timestamp(obj.get("ingested_at"), "ingested_at"),
        attempt_id=str(obj["attempt_id"]) if obj.get("attempt_id") is not None else None,
        capture_id=str(obj["capture_id"]) if obj.get("capture_id") is not None else None,
        refund_id=str(obj["refund_id"]) if obj.get("refund_id") is not None else None,
        amount_cents=amount_cents,
        currency=str(obj["currency"]).upper() if obj.get("currency") else None,
        text=_sanitise_note(obj.get("text")),
        raw=obj,
    )


def load_events(path: str | Path) -> Iterator[RawEvent]:
    """
    Stream RawEvent objects from a JSONL file.
    Skips blank lines and malformed records; logs warnings for each.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Events file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            event = parse_line(line, line_number)
            if event is not None:
                yield event
