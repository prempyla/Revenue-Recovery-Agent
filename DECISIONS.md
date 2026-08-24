# Decisions

Short, dated entries. Each one: what was decided, why, what tradeoff was accepted. This file doubles as the architecture writeup and panel prep — write here as decisions happen, not in retrospect.

---

**2026-08-24 — Track: AI Revenue Recovery, narrowed to payments + mandates**
Chose Track 03 over Track 01 (Agentic Commerce) because Razorpay has already shipped agentic checkout in production three times over (NPCI/Claude, OpenAI, Sarvam) — a student submission competes at the noise floor there. Track 03's crowd is more likely to build a detector-only dashboard, which the track brief explicitly warns against. Narrowed further to failed payments and failed UPI mandates, dropping checkout abandonment and B2B receivables, because covering all four surfaces in nine days produces four shallow things instead of one deep one.

**2026-08-24 — Money stored as integer paise, never float**
Razorpay's own API already works in paise. Float arithmetic on money is the fastest way to lose credibility with a payments reviewer. Tradeoff: slightly more verbose display formatting, accepted.

**2026-08-24 — Decision policy is a pure function**
`decide(payment_state, customer_history, config, now) -> action` has no DB calls, no API calls, no internal clock reads. All IO lives outside it. This is what makes the policy unit-testable without mocks, replayable against historical events, and explainable after the fact — call the same function with the same stored inputs and get the same answer. Tradeoff: more plumbing to pass state in explicitly, accepted.

**2026-08-24 — Append-only event log, state is derived, not mutated**
No `UPDATE` on outcomes. Current state is computed by replaying events. Chosen because it's the only way to answer "what actually happened to payment X" with proof rather than trust, and it's what a real audit trail requires. Tradeoff: more storage, more replay logic, accepted.

**2026-08-24 — Transactional outbox instead of direct external calls**
A decide-then-call sequence (`db.save(); api.call();`) loses or duplicates work if the process dies between the two lines. Outbox pattern: write the intent in the same transaction as the state change, a separate worker executes and marks it done. Gives at-least-once delivery, which combined with idempotency keys gives effectively-once. Tradeoff: one more moving part (the outbox worker), accepted because the alternative is silent money loss or double-charging.

**2026-08-24 — Idempotency keys derived deterministically from (payment_id, attempt_number)**
Not a random UUID per attempt — a random key gives zero protection against a crash-and-replay. Deriving the key means a replay produces the same key and the API returns the original result instead of firing twice.

**2026-08-24 — Reconciliation poller as a backstop to webhooks**
Webhooks arrive out of order, duplicate, or don't arrive at all. HMAC-verified, deduped by event id, but backed by a periodic poller that catches whatever the webhook layer missed. A system that trusts webhooks alone is a system that silently loses money on a bad network day.

**2026-08-24 — Jittered, ramped resume after a detected outage, not a synchronized retry**
If 200 held payments all retry the instant an issuer outage clears, the system DDoSes a bank that just came back up. Resume ramps a handful of probe attempts first, confirms success, then opens the gate gradually — a circuit breaker in the closed/open/half-open sense, not a novel idea, just correctly applied here.

**2026-08-24 — LLM boundary: classification and drafting only, never the money decision**
The LLM writes the audit-trail diagnosis in plain language, drafts the customer message, and classifies free-text replies into a fixed intent enum. The decision of what action to take and whether to execute it is made entirely by the deterministic policy layer downstream. This is a hard boundary, not a soft guideline — the LLM's output type makes it structurally incapable of triggering a payment action on its own. Also means the pipeline degrades gracefully (templates, rule-based parsing) if the model API is unavailable.

**2026-08-24 — Eval protocol and simulator spec written before implementation**
See `docs/eval_protocol_and_simulator_spec.md`. Written first specifically so the measurement wasn't designed after seeing what the system does well — that ordering makes cherry-picking impossible by construction, not by discipline.

**2026-08-24 — Test mode only, no live keys generated**
No KYC or live activation needed for the buildathon scope, and it removes any chance of accidentally touching real money during development.

**2026-08-25 — Action channel added to the log schema, missing from initial spec**
Claude Code caught that the eval spec's §4 formulas need a channel to compute success probability, but §6's persisted log entry didn't include one. Fixed: channel is now part of the audit record itself, not just an internal calculation input — an audit trail that doesn't say *how* a customer was contacted is incomplete. Also fixed: decline_reason is now explicitly mapped per instrument_type rather than left to inference.

**2026-08-25 — True outage duration raised from 20 to 90 minutes**
At 20 minutes, naive_fixed_retry's first attempt (fail_time + 1hr) always lands after the outage has already cleared — the baseline never actually gets caught by it, so the outage-ablation demo has nothing to show. 90 minutes overlaps naive's first retry attempt while still clearing before its second (+2hr). Decoy cluster duration is unaffected — it only needs to look like a burst for the false-positive-rate measurement, not overlap any baseline's retry schedule.

**2026-08-25 — naive_fixed_retry's retries are customer-facing, not silent**
Its 3 fixed attempts each send a customer-facing notification, unlike a taxonomy-aware silent backend retry. Tradeoff: this is what makes contact_count (and therefore ₹/contact) actually differentiate naive from a real policy — a silent naive baseline would win on ₹/contact by construction, which would defeat the point of the primary metric.
