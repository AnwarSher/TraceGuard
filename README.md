# TraceGuard

Deterministic investigation of payment incidents. TraceGuard rebuilds the financial state of an e-commerce order from a stream of payment events, surfaces contradictions, and returns an explanation tied to the records that support it.

Financial status, amounts, and evidence are decided by explicit rules. An optional language model can phrase the explanation for an operator. It cannot change the outcome.

## Capabilities

- Reconciles captures, failures, refunds, and chargebacks for a single tenant and order
- Ignores exact duplicate events and rejects records that reuse an identifier with different facts
- Keeps tenants isolated and treats support notes as untrusted text
- Returns `UNKNOWN` when the evidence is incomplete or contradictory
- Emits a stable JSON result with status, amounts, anomalies, evidence IDs, and confidence

## Requirements

- Python 3.11 or newer
- No third-party packages for the reconciliation engine
- [pytest](https://docs.pytest.org/) to run the test suite
- An OpenAI API key only if you want narrative explanations

## Setup

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

macOS and Linux:

```bash
source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

For narrative explanations:

```bash
pip install -e ".[llm]"
```

## Usage

```bash
python run.py --events data/events.jsonl --tenant T1 --order O-201 --question "Was this order paid?"
```

| Flag | Required | Purpose |
| --- | --- | --- |
| `--events` | Yes | Path to a JSONL event file |
| `--tenant` | Yes | Tenant to investigate |
| `--order` | Yes | Order to investigate |
| `--question` | No | Question the explanation should answer |
| `--llm` | No | Ask the language model to phrase the explanation |
| `--verbose` | No | Write debug logs to stderr |

Set `OPENAI_API_KEY` before using `--llm`. If the API call fails, TraceGuard retries once and then returns the deterministic explanation.

### Example

```json
{
  "status": "PAID",
  "captured_cents": 12900,
  "refunded_cents": 0,
  "net_paid_cents": 12900,
  "anomalies": [],
  "evidence_event_ids": ["order-1", "pay-fail-1", "pay-1"],
  "explanation": "Order expected 12900 USD. 1 payment attempt(s) failed. 1 capture(s) totalling 12900 cents. Final status determined: PAID.",
  "confidence": "HIGH"
}
```

## How a status is chosen

Rules are applied in this order. The first match wins.

| Order | Condition | Status |
| --- | --- | --- |
| 1 | Integrity failure, or the order itself cannot be established | `UNKNOWN` |
| 2 | A chargeback is open | `DISPUTED` |
| 3 | Nothing was captured, and a payment failure is on record | `UNPAID` |
| 4 | Refunded amount equals captured amount, and both are positive | `REFUNDED` |
| 5 | A partial refund is on record | `PARTIALLY_REFUNDED` |
| 6 | Captured amount matches the order amount | `PAID` |
| 7 | A positive capture is still short of the order amount | `PARTIALLY_PAID` |
| 8 | None of the above | `UNKNOWN` |

Confidence is `HIGH` when the record is complete and consistent, `MEDIUM` when the result holds with minor anomalies, and `LOW` when integrity problems, an `UNKNOWN` status, or an attempted instruction in a support note remain.

## Design boundaries

- Only payment-source events establish money movement. A failed attempt does not cancel a capture on a different attempt.
- Events are interpreted by type and timestamp, so late arrivals do not rewrite a valid capture.
- Support notes are stored as opaque text. They cannot change status, totals, or the evidence list. Instruction-like notes are reported as anomalies.
- The same investigation on the same events produces the same result.

The reasoning behind these choices is written up in [DECISIONS.md](DECISIONS.md).

## Sample orders

`data/events.jsonl` includes worked examples:

| Order | Outcome |
| --- | --- |
| O-201 | Paid after a failed attempt and a duplicate capture |
| O-202 | Fully refunded |
| O-203 | Partially refunded |
| O-204 | Unpaid after failed attempts |
| O-205 | Disputed by chargeback |
| O-206 | Partially paid, with an instruction-like support note |
| O-207 | Unknown, because the order definition conflicts with itself |
| O-209 | Unknown, because a refund exceeds the capture |
| O-210 | Unknown, because the currency does not match |

## Tests

```bash
python -m pytest
```

The suite covers consistent orders, malformed input, contradictory records, out-of-order delivery, duplicate events, tenant boundaries, and adversarial notes.

## Layout

```
traceguard/
├── run.py                 # Command-line entry point
├── traceguard/
│   ├── models.py          # Events, statuses, and the investigation result
│   ├── parser.py          # JSONL parsing and validation
│   ├── engine.py          # Reconciliation rules
│   └── llm.py             # Optional explanation layer
├── data/events.jsonl      # Sample event stream
├── tests/                 # Automated checks
├── DECISIONS.md           # Engineering notes
└── AI_USAGE.md            # Notes on assistant use during development
```

## License

[MIT](LICENSE) © Anwar Sher
