# Revenue Recovery Agent

Built for the Razorpay AI Buildathon 2026 — Track 03, AI Revenue Recovery.

## Scope

This system recovers **failed one-time payments** and **failed UPI mandate debits** for a single synthetic merchant. Given a failed payment, it diagnoses why it failed, chooses one bounded recovery action from a fixed menu, executes it against Razorpay's test-mode APIs, and measures how much money it actually recovers compared to a naive fixed-retry baseline.

Explicitly out of scope: checkout abandonment, B2B receivables, voice channel, subscription lifecycle beyond mandate retry. These are natural extensions, not gaps — see `DECISIONS.md`.

## Why this shape

Recovery is treated as a sequential decision problem under a contact budget and a compliance constraint, not a classification problem. The system doesn't just flag payments at risk — it decides what to do about each one, and can choose to do nothing. See `docs/eval_protocol_and_simulator_spec.md` for how that claim is measured and what would falsify it.

## Status

Early build. See `DECISIONS.md` for a running log of what's been decided and why.

## Setup

```bash
cp .env.example .env
# fill in your Razorpay test-mode keys
```

(Full run instructions to be added as the pipeline comes together.)
