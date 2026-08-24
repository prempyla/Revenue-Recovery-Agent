# Eval Protocol & Simulator Spec — Revenue Recovery Agent

Written before implementation. This is the contract the system will be judged against — if the code and this spec disagree later, the spec wins and the code is wrong, not the other way round.

Scope (from DECISIONS.md): failed one-time payments + failed UPI mandate debits, for a single synthetic merchant, over roughly one month of activity.

---

## 1. Failure taxonomy

Every decline gets mapped to exactly one category. This mapping is a lookup table, not a model — it's deterministic and testable on its own.

| Category | Retryable? | Systemic-detectable? | Correct response family | Typical delay before retry |
|---|---|---|---|---|
| `INSUFFICIENT_FUNDS` | Yes, delayed | No (idiosyncratic) | Wait for funds signal, or send payment link customer can use later | Until estimated funds-arrival, or a link with no fixed retry |
| `ISSUER_DOWN` | Yes, delayed | **Yes** | Hold, detect spike, resume after recovery | 15–30 min, jittered |
| `NETWORK_TIMEOUT` | Yes, immediate | Weak | Short retry | 1–5 min |
| `RISK_DECLINE` | No blind retry | No | Alternate instrument, or escalate — never brute-force retry | N/A |
| `CARD_EXPIRED` | No | No | Prompt instrument update, don't retry old card | N/A |
| `CARD_OR_ACCOUNT_BLOCKED` | No (terminal) | No | Mark unrecoverable via this instrument, offer alternate | N/A |
| `MANDATE_REVOKED` | No (terminal) | No | Requires fresh mandate consent — outside auto-retry scope | N/A |
| `MANDATE_INSUFFICIENT_FUNDS` | Yes, delayed | No | Same as insufficient funds, but bounded by mandate's retry rules | Until funds signal |
| `AFA_3DS_DROPOFF` | No blind retry | No | Customer abandoned mid-auth — nudge/reminder, not a system retry | Short nudge delay |
| `LIMIT_EXCEEDED` | Yes, delayed | Weak | Cooldown, or alternate instrument | Hours |

Two categories are structurally different from the rest and deserve emphasis: `ISSUER_DOWN` is the only one where **many customers failing together is itself the signal** — a single-payment view can never diagnose it. `RISK_DECLINE` is the one where retrying is actively wrong — repeated attempts against a risk-declined instrument can get the merchant's traffic flagged. Both of these are places a rules-only baseline (see §3) can go wrong in opposite directions, which is why they're both in the taxonomy explicitly rather than folded into "retry with backoff."

---

## 2. Metrics

**Primary metric — ₹ recovered per customer contact.**
`sum(recovered_amount) / count(contacts_made)`. This is the one number the whole pitch hangs on, because it's the only metric a naive "retry more, message more" policy can't win by brute force.

**Secondary metrics, reported alongside it — not optimized in isolation:**
- Recovery rate: % of at-risk ₹ recovered within a fixed horizon (e.g. 7 days from first failure)
- Median time-to-recovery
- Outage-window recovery rate specifically (isolates the diagnose layer's contribution)
- Systemic-detector false-positive rate (fires on noise, not a real outage)
- Systemic-detector detection lag (real outage to detection time)

**Hard invariants — pass/fail, not scored on a curve:**
- Zero double-charges across the entire run
- Zero contacts outside allowed hours
- Zero contacts to a customer past their weekly cap
- Zero contacts after an opt-out event

A policy that improves the primary metric by violating an invariant is a failed run, full stop. Report invariant checks as a separate pass/fail block before the metrics table — this is what "bounded and gated" looks like as a number instead of a claim.

---

## 3. Policies compared, same batch, same ground truth

1. **Do nothing** — floor. Whatever recovers with zero intervention (funds arrive, customer retries on their own).
2. **Naive fixed retry** — industry default. 3 attempts, 1 hour apart, then give up. No taxonomy awareness.
3. **Rules-only** — taxonomy lookup drives the action, no systemic detection, no LLM. Deterministic routing only.
4. **Full agent** — taxonomy + systemic outage detection + cost-aware decision policy + LLM messaging layer.

**Ablations, run alongside the main four:**
- Full agent **minus** outage detection — isolates exactly how much the systemic-detection idea is worth. If this ablation performs the same as the full agent, the outage feature is decorative and the video claim is wrong.
- Full agent **minus** LLM (template messages, rule-based reply parsing) — isolates the LLM's actual contribution. It is fine, and expected, if this number is small. Reporting it honestly is the point — it's what makes the "LLM never decides when to move money" line credible instead of a slogan.

## 4. What would falsify this approach

State these before running anything, so the result can't be quietly reinterpreted afterward:

- If **rules-only ≥ full agent** on ₹/contact, the decision-policy layer adds nothing over static taxonomy routing.
- If the **outage ablation ≈ full agent**, the systemic-detection idea is not earning its place in the architecture.
- If the **systemic-detector false-positive rate** is high on the decoy cluster (§5), the "diagnose, don't just detect" claim is undermined — a detector that cries wolf is worse than none.
- If **naive retry ≥ full agent** on raw ₹ recovered (not ₹/contact), that's fine and expected — it should recover more in absolute terms, at a far worse cost-per-contact. If it doesn't even do that, the ground-truth model in the simulator is broken, not the policy.

---

## 5. Simulator spec

**Scale:** ~400–500 at-risk payments across ~300 customers, over a simulated 30-day window.

**Customer persona (hidden from the policy, known to the simulator — this is the ground truth):**
- `recovery_propensity` — base probability of paying if contacted appropriately
- `funds_arrival_day` — for insufficient-funds cases, when they'll actually have money
- `contact_hours` — their real preferred window
- `annoyance_threshold` — number of contacts before they opt out and become permanently unrecoverable
- `channel_response_rate` — per channel (UPI link, WhatsApp, SMS)

**Payment record:** amount, instrument type (card / UPI / mandate), decline reason drawn from a realistic category distribution, timestamp.

**Ground-truth success function** — this is the part that makes the eval real rather than circular:
- `INSUFFICIENT_FUNDS`: near-zero probability before `funds_arrival_day`, rises after, decays again the longer the payment sits unresolved past that point (opportunity decay — customers who intended to pay lose intent over time too)
- `ISSUER_DOWN`: near-zero during the outage window, high shortly after recovery, decays with excess delay past recovery
- Terminal categories: zero probability from any retry; only a non-retry action (instrument update, alternate channel) has nonzero probability
- `AFA_3DS_DROPOFF`: moderate probability from a nudge, decaying fast — these customers forget quickly

**Contact cost and annoyance — the mechanism that makes ₹/contact meaningful, not just a ratio:** each contact has a fixed ₹ cost. If a customer's contact count exceeds their `annoyance_threshold`, they opt out and become permanently unrecoverable regardless of what happens afterward. This is what actually penalizes the naive fixed-retry baseline in the simulation — not a made-up penalty term, but the same mechanism a real collections team lives with.

**Outage injection — both a real one and a decoy:**
- One **true outage**: pick one issuer code, a start time, ~20-minute duration. During that window, force `ISSUER_DOWN` at near-100% for any payment attempt against that issuer. Recovery probability returns to normal shortly after the window closes.
- One **decoy cluster**: a smaller, coincidental burst of same-issuer failures that is *not* a real outage — just random clustering. This exists specifically to measure the detector's false-positive rate (§2, §4). Without a decoy, you can only ever report a true-positive story, which a reviewer will notice.

**What "ground truth" gives you that real data can't:** because you generate `recovery_propensity`, `funds_arrival_day`, and the outage timing yourself, you know — for every payment — whether a given action at a given time *would have worked*. That's what makes precision/recall-style claims on the diagnosis and policy layers possible at all. State this plainly in the README: the absolute ₹ figures are synthetic; the ranking between policies, run on identical ground truth, is the claim.

---

## 6. Order of build (maps to D1–D2)

1. Freeze the taxonomy table above — don't revisit it after today.
2. Build the persona and payment generators with hidden ground truth.
3. Build the ground-truth success function per category, including decay.
4. Add the contact-cost/annoyance mechanism.
5. Inject the true outage and the decoy cluster.
6. Write the four baseline policies as stubs that return a fixed or trivial action — this lets you build the eval harness against something runnable before the real policy exists.
7. Confirm the harness produces all primary + secondary metrics + invariant checks on stub policies before writing a single line of the real decision function.
