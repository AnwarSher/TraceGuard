# Engineering decisions

**Author:** Anwar Sher  
**Project:** TraceGuard

---

## 01. What is the most dangerous assumption in this problem?

The most dangerous assumption is that **event timestamps are trustworthy and
monotonically correct**. The spec explicitly says events may arrive late or
out of order, but a naive implementation might assume that "the latest event
wins" or that `occurred_at` reliably reflects the true sequence of financial
activity.

This matters most when a `PAYMENT_FAILED` event arrives *after* a
`PAYMENT_CAPTURED` for the same attempt — an ordering-naive engine might
wrongly mark a successful payment as failed.

TraceGuard avoids this by sorting on `occurred_at` before processing and
applying **declarative rules that are order-independent**: a failure does not
cancel a capture on a different attempt ID, regardless of which event arrived
first.

---

## 02. Which part must not depend on an LLM, and why?

The entire **financial decision layer** must never depend on an LLM:

- Order status determination (`PAID`, `UNPAID`, `DISPUTED`, etc.)
- Captured, refunded, and net amounts
- Anomaly detection and integrity checks
- Duplicate detection and tenant isolation
- Prompt-injection resistance

**Why:** Language models are non-deterministic, hallucinate, and can be
manipulated through adversarial text in support notes. Financial decisions
require auditability — every conclusion must be traceable to specific event
records. An LLM cannot provide this guarantee.

TraceGuard uses the LLM **only** to generate the natural-language explanation
for a human operator, and even that explanation is bounded by the pre-computed
facts (status, amounts, evidence IDs) that the LLM receives as read-only input.

---

## 03. When is UNKNOWN the right answer?

`UNKNOWN` is returned when the system cannot reliably establish financial
state. Specifically:

1. **Missing essential evidence**: no `ORDER_CREATED` from checkout source, or
   the order definition is missing amount/currency.
2. **Integrity failures**: conflicting `ORDER_CREATED` definitions, same
   `event_id` with different substantive fields, same `capture_id` or
   `refund_id` reused with different amounts.
3. **Invalid financial references**: over-capture (captured > expected),
   currency mismatches between order and capture.
4. **No rule applies**: the event set doesn't satisfy any of the 7 status
   conditions (e.g., an order with no captures and no payment failure evidence).

The principle is *conservative by default*: when in doubt, say so. A false
`PAID` on a disputed order is worse than a correct `UNKNOWN`.

---

## 04. What was the hardest bug you found, and how did you verify the fix?

**Bug:** The deduplication logic initially compared `RawEvent` objects using
Python's default equality (which compares all fields including `ingested_at`).
This meant that an event arriving twice with only `ingested_at` changed would
**not** be detected as a duplicate — it would be counted as a new event,
doubling the captured amount.

**Root cause:** `RawEvent` is a frozen dataclass. The spec says "a change in
`ingested_at` alone does not make an event new." The equality check needed to
ignore `ingested_at` and only compare *substantive* fields.

**Fix:** Introduced the `_SUBSTANTIVE_FIELDS` set and the
`_substantively_equal()` helper function that compares only those fields.
Added `compare=False` to the `raw` field to exclude it from dataclass equality.

**Verification:** `TestDeduplicationAndOrdering::test_duplicate_event_ignored`
creates two captures with identical fields except `ingested_at`, then verifies
that `captured_cents` equals the single capture amount (not doubled). The test
was failing before the fix and passes after.

**Second bug found:** `test_out_of_order_events_same_result` was failing
because the engine processed events in `occurred_at` order only. When a
REFUND_ISSUED event had an `occurred_at` earlier than its referenced
PAYMENT_CAPTURED, the refund was rejected as "referencing unknown capture_id"
even though both events existed.

**Fix:** Changed the sorting strategy to first order by *event type priority*
(ORDER_CREATED → PAYMENT_FAILED → PAYMENT_CAPTURED → REFUND_ISSUED →
CHARGEBACK_OPENED → NOTE) and then by `occurred_at` within each type. This
ensures captures always exist in state before refunds and chargebacks
reference them, regardless of their original timestamps.

---

## 05. Which approach or AI-generated suggestion did you reject, and why?

**Rejected:** Using the LLM to classify whether a support note is a prompt-
injection attempt, and then deciding whether to process it.

This was rejected because:
1. **It's circular**: using an LLM to defend against LLM manipulation is
   inherently fragile — a sufficiently adversarial prompt could fool the
   classifier.
2. **It's unnecessary**: support notes should **never** influence financial
   state, period. The correct architecture is to treat all note text as opaque
   strings that go into anomaly logging but never touch the reconciliation
   engine.
3. **It introduces an external dependency** that could fail or be unavailable.

The implemented approach uses a simple keyword list (`_INJECTION_MARKERS`) for
detection and logging, while the core defence is architectural: notes are parsed
but discarded before financial processing.

---

## 06. What would break first at one million events per day?

**The in-memory event list.** Currently, `run.py` loads all events into a
Python list before filtering by tenant+order. At 1M events/day that's
potentially tens of GBs of JSON in memory.

The second failure would be **linear scan time** — filtering and reconciling
across all events for one order touches every event in the file.

**Fixes that would be needed first:**
1. Stream events from storage with server-side filtering (database query or
   object-storage prefix by tenant+order).
2. Persist the reconciled state in a database keyed by `(tenant_id, order_id)`,
   updating incrementally on each new event rather than re-reading the full
   history.
3. Use an event index (`event_id → offset`) to detect duplicates without
   loading full payloads.

---

## 07. If you had one more day, what would you improve first?

**Incremental state persistence with idempotent event application.**

Currently the engine re-processes all events from scratch on every invocation.
Adding a persistent `ReconciliationState` store (even a SQLite database) would:

1. Enable O(1) updates — apply only new events to the existing state.
2. Support real-time alerting when anomalies are detected.
3. Allow the system to handle the 1M events/day scale issue (Q6).

The state store would use the same deterministic rules, just applied
incrementally. Idempotency (duplicate events are still ignored) would be
guaranteed by checking `event_id` against the persisted `seen_event_ids` set
before applying.
