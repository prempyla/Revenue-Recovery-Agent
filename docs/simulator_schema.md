# Simulator Schema — Implementation Spec

Companion to `docs/eval_protocol_and_simulator_spec.md`. That file says *what* to measure and why; this file says exactly *what fields exist* so the generator can be built without re-deciding anything mid-code. Language-agnostic — adapt types to whatever stack this repo uses.

Scale: ~300 customers, ~400–500 payments, 30 simulated days.

---

## 1. Customer persona

Hidden from the policy at decision time. Visible only to the simulator's ground-truth engine and the eval harness.

| Field | Type | Range / distribution | Notes |
|---|---|---|---|
| `customer_id` | string | `cust_0001` ... | |
| `recovery_propensity` | float | Beta(2,2), so mass around 0.3–0.7 | Base probability of paying if contacted at the *right* time with the *right* action |
| `funds_arrival_day` | int, days from failure | mixture: 60% clustered at day 1–3, 40% clustered at day 25–30 | Models salary-cycle reality — most people are either flush now or waiting for month-end. Only meaningful for `INSUFFICIENT_FUNDS` / `MANDATE_INSUFFICIENT_FUNDS`. |
| `contact_hours` | tuple (start, end), IST | mostly (9,21); ~10% of customers narrower, e.g. (10,18) | Used by the hard-constraint checker, not by ground truth |
| `annoyance_threshold` | int | Poisson(4), floor 2 | Number of customer-facing contacts before permanent opt-out |
| `channel_response_rate` | dict {upi_link, whatsapp, sms → float} | each 0.3–0.9, independent | Multiplies into ground-truth success for that channel |
| `preferred_language` | enum [en, hi, hinglish] | weighted, hinglish most common | Feeds the LLM drafting layer later — not used by ground truth |

---

## 2. Payment record

| Field | Type | Notes |
|---|---|---|
| `payment_id` | string | |
| `customer_id` | FK | |
| `amount_paise` | int | never float |
| `instrument_type` | enum [card, upi, mandate] | |
| `decline_reason` | enum, see taxonomy table in eval spec | drawn from a realistic category distribution — weight `INSUFFICIENT_FUNDS` and `ISSUER_DOWN` heaviest, terminal codes lightest. Sampled per `instrument_type`, not independently of it: `card` → {`INSUFFICIENT_FUNDS`, `ISSUER_DOWN`, `NETWORK_TIMEOUT`, `RISK_DECLINE`, `AFA_3DS_DROPOFF`, `LIMIT_EXCEEDED`, `CARD_EXPIRED`, `CARD_OR_ACCOUNT_BLOCKED`}; `upi` → {`INSUFFICIENT_FUNDS`, `ISSUER_DOWN`, `NETWORK_TIMEOUT`, `RISK_DECLINE`, `LIMIT_EXCEEDED`}; `mandate` → {`MANDATE_REVOKED`, `MANDATE_INSUFFICIENT_FUNDS`, `ISSUER_DOWN`, `NETWORK_TIMEOUT`} |
| `issuer_code` | string | e.g. `HDFC`, `ICICI`, `SBI` — needed for outage correlation |
| `failed_at` | timestamp | spread across the 30-day window |

---

## 3. Outage events

Two entries only.

| Field | Type | Notes |
|---|---|---|
| `issuer_code` | string | pick one issuer with reasonable payment volume |
| `start_time` | timestamp | mid-window, not day 1 or day 30 |
| `duration_minutes` | int | 90 for the true outage (raised from an initial 20 — see DECISIONS.md 2026-08-25: at 20 min, naive_fixed_retry's first attempt at +1hr always lands after the outage clears, so the outage-ablation comparison has nothing to demonstrate) |
| `kind` | enum [true_outage, decoy_cluster] | one of each, different issuers, different times |

During a `true_outage` window: any payment attempt against that issuer is forced to `ISSUER_DOWN`, near-100%. Recovery probability returns to baseline shortly after the window closes (see §4).

The `decoy_cluster` is a smaller, coincidental burst of same-issuer `ISSUER_DOWN` failures that is **not** a real outage — it exists solely to measure the systemic-detector's false-positive rate. Don't give it special-case handling in ground truth beyond normal idiosyncratic decay.

---

## 4. Ground-truth success function

One function, called by the eval harness whenever the policy takes an action:

```
success_probability(payment, customer, action, action_time) -> float [0,1]
```

This is never visible to the policy under test — only to the harness, which samples a Bernoulli draw from it to decide whether the action succeeded.

**By category:**

- **`INSUFFICIENT_FUNDS` / `MANDATE_INSUFFICIENT_FUNDS`**
  `0` if `action_time < funds_arrival_day`.
  Else `recovery_propensity * exp(-k1 * days_past_arrival)` — decays because intent fades the longer it sits unresolved after funds actually arrive. `k1` ≈ 0.15/day is a reasonable starting constant.

- **`ISSUER_DOWN`**
  `0` if `action_time` falls inside the outage window for that issuer.
  Else `recovery_propensity * exp(-k2 * hours_since_recovery)`, `k2` small — this one should barely decay, since once the bank is back the payment should just work.

- **`NETWORK_TIMEOUT`**
  `recovery_propensity * exp(-k3 * minutes_since_failure)`, `k3` large — decays fast, this category rewards quick retry specifically.

- **`RISK_DECLINE`**
  `0` for any `retry_*` action, regardless of timing.
  Nonzero, `recovery_propensity * 0.4`, only for an `escalate_alternate_instrument` action — reflects that blind retry is actively wrong here.

- **`CARD_EXPIRED`, `CARD_OR_ACCOUNT_BLOCKED`, `MANDATE_REVOKED`** (terminal)
  `0` for any retry action, ever.
  Nonzero, `recovery_propensity * channel_response_rate[channel]`, only for a `send_instrument_update_link` action.

- **`AFA_3DS_DROPOFF`**
  `0` for retry actions.
  `recovery_propensity * channel_response_rate[channel] * exp(-k4 * hours_since_failure)`, `k4` large — these customers forget within hours, so timing matters more than for any other category.

- **`LIMIT_EXCEEDED`**
  `0` before a cooldown period (few hours) has passed.
  After cooldown: `recovery_propensity * exp(-k5 * hours_since_cooldown_start)`, moderate decay.

All `k` constants belong in one config block, not scattered through the code — you'll want to tune them once you see whether the baselines separate sensibly.

---

## 5. Contact cost and annoyance

This is the mechanism that makes ₹-recovered-per-contact a real metric instead of a made-up ratio — it's what penalizes the naive baseline in the simulation itself.

- Define which `action_type`s are **customer-facing** (increment contact count) vs **silent** (a backend retry the customer never sees, e.g. `retry_now` on a card with no message sent). Only customer-facing actions cost money and count toward annoyance.
- `cost_per_contact_paise`: flat constant to start (e.g. 200 = ₹2), can differentiate by channel later if time allows.
- Track `contact_count` per customer, incrementing on each customer-facing action.
- The instant `contact_count > annoyance_threshold`: set `customer.opted_out = true`. From that point on, `success_probability` returns `0` for every subsequent action on that customer, no matter the category or action. This must be enforced inside the ground-truth function itself, not as a special case in the eval harness — the naive fixed-retry baseline should walk straight into this and lose those customers, exactly as a real overly-aggressive collections policy would.

---

## 6. Action log entry

What every policy — baseline or real — writes when it acts. This is also your audit trail.

| Field | Type | Notes |
|---|---|---|
| `log_id` | string | |
| `payment_id` | FK | |
| `action_type` | enum [retry_now, retry_scheduled, send_payment_link, send_nudge, send_instrument_update_link, escalate_alternate_instrument, stop] | |
| `channel` | enum [sms, whatsapp, upi_link, none] | required for customer-facing actions; `none` for silent/retry/stop actions. Also the channel `success_probability` multiplies `channel_response_rate` by (§4) — the ground-truth formula's input and the audit record are the same value, not two separate things. |
| `action_time` | timestamp | |
| `is_customer_facing` | bool | drives contact-cost accounting |
| `policy_name` | string | which of the four policies (or ablations) produced this |
| `reason` | string | short human-readable — required even for baselines, this is what "explainable" means in practice |
| `outcome` | enum [success, fail, pending] | sampled from ground truth at write time |
| `cost_paise` | int | 0 for silent actions |

---

## 7. Build order for D2

1. Persona generator → sanity-check the distributions by histogram before moving on.
2. Payment generator, drawing from the category distribution.
3. Ground-truth function, one category at a time — write a quick script that plots probability vs. time for each category and eyeball that the curves look like the story in §4.
4. Outage + decoy injection.
5. Contact-cost/annoyance mechanism, tested in isolation: simulate a customer getting spammed and confirm they opt out at the right count.
6. Only then wire in the four baseline policies (can be near-trivial stubs) and confirm the harness produces every metric from the eval spec end to end.

Don't start on the real decision policy until step 6 runs cleanly on stubs. If the harness has a bug, you want to find it against a dumb policy, not your real one.
