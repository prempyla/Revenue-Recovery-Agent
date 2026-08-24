# Revenue Recovery Agent

Built for the Razorpay AI Buildathon 2026 — Track 03, AI Revenue Recovery.

## Scope

This system recovers **failed one-time payments** and **failed UPI mandate debits** for a single synthetic merchant. Given a failed payment, it diagnoses why it failed, chooses one bounded recovery action from a fixed menu, executes it against Razorpay's test-mode APIs, and measures how much money it actually recovers compared to a naive fixed-retry baseline.

Explicitly out of scope: checkout abandonment, B2B receivables, voice channel, subscription lifecycle beyond mandate retry. These are natural extensions, not gaps — see `DECISIONS.md`.

## Why this shape

Recovery is treated as a sequential decision problem under a contact budget and a compliance constraint, not a classification problem. The system doesn't just flag payments at risk — it decides what to do about each one, and can choose to do nothing. See `docs/eval_protocol_and_simulator_spec.md` for how that claim is measured and what would falsify it.

## The LLM boundary

The LLM (`src/llm/`, Claude Sonnet 4.6) has exactly three jobs: draft outbound customer copy, classify a customer's reply into a closed five-value intent enum, and write a plain-language audit-trail note after a decision has already been made. It never decides what action to take or whether to execute it — that's `decide()` (`src/simulator/full_agent.py`), a deterministic policy that never calls the LLM.

The proof isn't a claim in this paragraph — it's `tests/test_llm_fallback.py`, which runs the entire 3-seed policy comparison with an LLM client that raises on every single call and asserts it produces the exact same ₹/contact numbers and the exact same policy ranking as with a working client. And in the comparison itself, `full_agent` and `full_agent_minus_llm` come out **identical on every decision metric — by construction, not by omission.** That's not a limitation of the ablation; it's the demonstration. Nothing in the ground-truth simulation has a parameter for message quality, and `decide()`'s code path never imports anything from `src/llm/`, so there is no mechanism by which the LLM *could* move ₹/contact even if it tried. What differs between the two is exactly what should differ: LLM cost (real vs. zero) and the actual drafted message/diagnosis text (LLM-authored vs. template) — reported honestly in the harness output, not smoothed over.

## Status

Early build. See `DECISIONS.md` for a running log of what's been decided and why.

## Setup

```bash
cp .env.example .env
# fill in your Razorpay test-mode keys
```

(Full run instructions to be added as the pipeline comes together.)
