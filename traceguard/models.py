"""
Data models for TraceGuard events and results.
All dataclasses are immutable (frozen) to prevent accidental mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    ORDER_CREATED = "ORDER_CREATED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
    REFUND_ISSUED = "REFUND_ISSUED"
    CHARGEBACK_OPENED = "CHARGEBACK_OPENED"
    NOTE = "NOTE"


class OrderStatus(str, Enum):
    PAID = "PAID"
    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    DISPUTED = "DISPUTED"
    UNKNOWN = "UNKNOWN"


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Source(str, Enum):
    CHECKOUT = "checkout"
    PAYMENTS = "payments"
    SUPPORT = "support"


# ---------------------------------------------------------------------------
# Raw event (parsed from JSONL)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RawEvent:
    """Represents one parsed event record from the JSONL input."""

    event_id: str
    tenant_id: str
    order_id: str
    source: str
    type: str
    occurred_at: Optional[datetime]
    ingested_at: Optional[datetime]

    # Optional fields
    attempt_id: Optional[str] = None
    capture_id: Optional[str] = None
    refund_id: Optional[str] = None
    amount_cents: Optional[int] = None
    currency: Optional[str] = None
    text: Optional[str] = None

    # Raw original dict for reference
    raw: dict = field(default_factory=dict, compare=False, hash=False)


# ---------------------------------------------------------------------------
# Investigation result
# ---------------------------------------------------------------------------

@dataclass
class InvestigationResult:
    """Final output of a TraceGuard investigation."""

    status: OrderStatus
    captured_cents: Optional[int]
    refunded_cents: Optional[int]
    net_paid_cents: Optional[int]
    anomalies: list[str]
    evidence_event_ids: list[str]
    explanation: str
    confidence: Confidence

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "captured_cents": self.captured_cents,
            "refunded_cents": self.refunded_cents,
            "net_paid_cents": self.net_paid_cents,
            "anomalies": self.anomalies,
            "evidence_event_ids": self.evidence_event_ids,
            "explanation": self.explanation,
            "confidence": self.confidence.value,
        }
