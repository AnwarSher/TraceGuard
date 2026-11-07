# AI_USAGE.md – AI Tools Used in TraceGuard

**Author:** Anwar Sher

---

## Tools Used

- **Antigravity (Google DeepMind)** – Used as the primary AI coding assistant
  throughout development for code generation, test design, and architecture review.

---

## One Useful Suggestion

**Suggestion accepted:** The AI suggested using Python frozen dataclasses for
`RawEvent` to enforce immutability. This was excellent advice — it means that
once an event is parsed, no downstream code can accidentally mutate it,
eliminating an entire class of subtle bugs where reconciliation state could
be corrupted by shared mutable objects.

---

## One Suggestion Rejected or Corrected

**Suggestion rejected:** The AI initially suggested using the LLM to determine
whether a support note contained a prompt-injection attempt, feeding the note
to GPT-4 and asking it to classify the intent.

**Why it was rejected:** This creates an arms race — a sufficiently adversarial
note could fool the LLM classifier. More importantly, it's architecturally
wrong. The correct defence is not "detect and filter bad notes" but "notes
never touch financial logic regardless of their content." The implemented
solution uses a simple keyword list for logging/alerting purposes only, while
the financial engine ignores all notes by design.
