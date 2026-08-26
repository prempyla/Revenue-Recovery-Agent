# Incidents

A build journal of everything that broke during this build and how it got fixed. Sourced from `git log`, `DECISIONS.md`, and the tests that caught each one — nothing here is reconstructed from memory or dramatized. If a claim isn't backed by a commit or a DECISIONS.md entry, it isn't in this file.

Chronological order, oldest first.

---

## The double-charge invariant that could never fire

**Stage:** Building the eval harness's compliance invariants (`zero_double_charges`), right after the four baseline policies existed.

**What broke:** `run_policy()` had a `break` right after a payment's first successful attempt. So for `naive_fixed_retry` — a baseline that's supposed to fire 3 fixed retries regardless of outcome — the moment attempt 1 succeeded, attempts 2 and 3 never ran. The double-charge invariant was fully implemented and reported in every run output, but the condition it was built to catch (a second attempt firing after a payment already succeeded) could never physically arise, because the loop stopped generating it.

**Why it was dangerous:** This is worse than a missing test — it's a passing one that proves nothing. A metric that always reads "0 violations" looks identical whether the policy is actually safe or the harness simply can't observe a violation. Shipping this would mean showing a judge "zero double-charges, verified" when the check was structurally incapable of finding one.

**How I found it:** Reading the harness's own control flow while investigating why `naive_fixed_retry`'s `contact_count` looked lower than its spec (3 fixed attempts) implied — not from a failing test, since none existed to fail.

**The fix:** Removed the early-stop `break`. `naive_fixed_retry` now genuinely fires all 3 attempts independent of prior outcome, as its spec says. Revenue accounting still credits only the first chronological success per `payment_id` (`if success and not recovered`, no early exit), so `total_recovered` can't double-count even though every attempt is now logged. Re-run on the same batch: naive now shows 46 real double-charge payments (was always 0, vacuously). Added a deterministic proof test plus a real-batch regression test. Commit `ca604ec`.

**Lesson:** An invariant that has never fired is not evidence of safety until you've confirmed it's mechanically possible for it to fire at all.

**Video line:** Our double-charge counter said zero the whole time — turns out the test loop stopped early, so it never had a chance to see one.

---

## Outage duration too short to overlap the baseline's retry cadence

**Stage:** Building the four baseline policies and the eval harness, specifically the outage-detection ablation demo.

**What broke:** `TRUE_OUTAGE_DURATION_MINUTES` was set to 20. `naive_fixed_retry`'s first retry lands at `fail_time + 1hr` — by the time it fires, a 20-minute outage has already cleared. The baseline that was supposed to demonstrate the cost of retrying blindly into a live outage never actually retried into one.

**Why it was dangerous:** Not a crash, a silent demo failure — the entire point of simulating a systemic outage is to show a naive policy hammering a dead issuer while a smarter one holds off. With a 20-minute window and a 1-hour retry cadence, that comparison would show no difference at all, and the reason wouldn't be obvious from the output — it would just look like outages don't matter.

**How I found it:** Checking whether `naive_fixed_retry`'s attempt 1 actually landed inside the outage window before trusting the ablation's numbers — not from a failure, from verifying the setup made sense before relying on it.

**The fix:** Raised true outage duration to 90 minutes — long enough to overlap the first retry attempt (+1hr) while still clearing before the second (+2hr). Decoy cluster duration left untouched, since it only needs to look like a burst for the false-positive measurement, not overlap any retry schedule. Commit `9edf907`.

**Lesson:** A simulated failure condition is only as useful as its overlap with the thing that's supposed to react to it — check the timing arithmetic, not just that the event fires.

**Video line:** Our first outage was too short to catch a retry in the act, so the whole comparison had nothing to show.

---

## The outage detector's decoy false positives, and the density trap that caused them

**Stage:** Building the standalone systemic outage detector, right after the 20→90 minute duration fix above made this next problem visible.

**What broke:** A sliding-window count-threshold detector, swept across integer thresholds {2,3,5,6,7,8}, couldn't cleanly separate a real outage from a decoy cluster that was designed to merely *look* like one. Low thresholds (2/3/5) caught the real outage but also false-positived on the decoy; thresholds that looked clean (6/7) only won by a fragile one-event margin from random clustering variance, not a structural gap; 8 missed the real outage outright. Root cause: stretching the true outage to 90 minutes (the fix above) spread its cluster thin — density 0.167 events/min — while the decoy stayed packed into 15 minutes at 0.4 events/min. The decoy was denser per minute despite having fewer total events, so a plain count threshold couldn't tell them apart.

**Why it was dangerous:** A detector that trips on a random cluster of unrelated declines would make the system hold back retries on payments that didn't need holding — real recoverable revenue delayed for no reason, on a signal that looks authoritative in a log ("systemic event detected") but is actually noise. And a threshold that only worked by a one-event margin on one seed would have been a coin flip in production.

**How I found it:** A single-seed threshold sweep (`tests/test_outage_detector.py`) that explicitly measured the decoy's false-positive behavior, not just whether the real outage got caught — the study log itself surfaced the density mismatch when the "good" thresholds turned out to win by margins that didn't hold up on inspection.

**The fix, in two rounds:** Round 1 (commit `dd76637`) built the detector standalone and reported the problem honestly rather than picking a threshold to hide it — the DECISIONS.md entry from that round explicitly declines to wire any threshold into production code until it's addressed properly. Round 2 (commit `8844626`) fixed the actual root cause upstream, in payment generation, not in the detector: added `TRUE_OUTAGE_BURST_SIZE`/`TRUE_OUTAGE_BURST_WINDOW_MINUTES`, concentrating 20 extra organically-typed failures into the outage's first ~20 minutes, purely additive on top of the existing cluster. Re-swept the same six thresholds across 3 seeds (42, 7, 123) instead of one. Threshold 6 came out clean — zero decoy false positives on all 3 seeds — with detection lag down to 4–9 minutes (from 34–71 minutes pre-fix). Set as `config.DETECTOR_COUNT_THRESHOLD = 6`.

**Lesson:** A detector's threshold isn't a tuning knob to search over until the numbers look good on one run — if the underlying signal is genuinely ambiguous (same count, different density), fix the signal, and prove the fix across multiple seeds before trusting it.

**Video line:** Our outage detector kept confusing a random cluster of failures for a real outage, until we fixed how outages actually look, not the threshold.

---

## The invalid test card

**Stage:** Manual, real-world verification of the webhook receiver — the one step of the build that couldn't be scripted, requiring a real ngrok tunnel and a real browser payment against Razorpay's test mode.

**What broke:** The test card used in `docs/manual_webhook_verification.md` (`4111 1111 1111 1111`) — a number that reads as a standard, universally-recognized test card — was rejected outright by Razorpay's live test-mode checkout as "international cards are not supported." It isn't actually in Razorpay's current documented domestic test-card list.

**Why it was dangerous:** On its own, low stakes — it just blocked the verification step. But it's a concrete instance of a broader risk in this build: assuming a widely-known convention (a famous test card number) still holds against the actual current API, instead of checking. The same instinct, applied to something that mattered more (like the webhook payload shape below), is exactly what did go wrong in the same session.

**How I found it:** Running the actual manual procedure — the checkout page returned the rejection live, in the browser.

**The fix:** Switched to `4100 2800 0000 1007`, confirmed against Razorpay's current documentation, checked live rather than recalled from memory. Updated `docs/manual_webhook_verification.md` with the correct card and a note on why the old one fails. The rejected attempt incidentally produced a real `payment.failed` webhook, kept as a second test fixture. Commit `484be3c`.

**Lesson:** A "well-known" test value is still an assumption about the current state of someone else's API — verify it live before building anything on top of it.

**Video line:** Even the textbook Visa test card got rejected by Razorpay — the fix was checking the current docs instead of trusting memory.

---

## The webhook event id living in a header, not the body

**Stage:** Same manual verification session as above — the actual payload capture, via ngrok's inspector, of 6 real webhook deliveries from a real payment.

**What broke:** `webhook.py`'s deduplication logic assumed the event id would be in the JSON body (`payload["id"]` or `payload["event_id"]`). Every one of the 6 real deliveries carried its id only in the `X-Razorpay-Event-Id` HTTP header — the body fields the code checked for don't exist at all. Every single real webhook received during the verification run was rejected with our own 400 ("missing event id") until this was fixed.

**Why it was dangerous:** This is the exact mechanism the system relies on to guarantee at-least-once-but-not-duplicated processing of payment outcomes. If this had gone into a real deployment unverified, every webhook would 400 — Razorpay would retry (which it does, confirmed directly in the same capture: every one of the 6 deliveries was redelivered multiple times while the endpoint kept 400ing), so the failure would eventually be visible as a wall of retries in the logs. But the recovery path (the reconciliation poller) would have papered over it silently, and the specific reason — a wrong assumption about where an id lives — would have taken real debugging to find, on the exact system that's supposed to be the trustworthy source of "did the customer pay."

**How I found it:** The manual verification procedure itself — this was the entire reason it exists as a required step rather than an assumption. The payload envelope for `payment_link.entity.reference_id` was correctly assumed and needed no change; the event-id location was the one thing that was wrong, and it was only found because the real payload was actually inspected rather than assumed from documentation.

**The fix:** `webhook.py`'s endpoint now reads `X-Razorpay-Event-Id` from the request headers instead of the body. Verified twice: a new fixture-based unit test (`test_real_payment_link_paid_fixture_parses_and_recovers`) against the exact captured payload, and a live replay of that same payload against the running, fixed local server — the response went from 400 to 200. Real payloads (with contact info redacted) saved as test fixtures. Commit `484be3c`.

**Lesson:** An assumed request envelope is a hypothesis, not a fact — the only way to know a webhook contract is right is to receive a real one and read it.

**Video line:** Our webhook dedup checked the wrong field entirely — the real event id was in a header, not the body, and every real delivery 400'd until we looked.

---

## The 60-minute hold duration that produced a plausible null result on 2 of 3 seeds

**Stage:** Building `full_agent`, the cost-aware decision policy — specifically verifying its outage-detection ablation (`full_agent` vs. `full_agent_minus_outage_detection`) actually demonstrated something.

**What broke:** When the systemic detector flags an active outage, `full_agent` holds a retry rather than firing immediately — the hold offset was `detector.window_minutes + detector.cooldown_minutes + 30 = 60min`. On 2 of the 3 sweep seeds, `full_agent` and its outage-detection-disabled ablation produced byte-identical results. Not because detection didn't matter — because a payment failing near the onset of a 90-minute true outage still landed inside the still-live window whether it retried at +20min (ablated, no hold) or +60min (held). Ground truth forced both paths to 0% success either way, so the "improvement" from holding never had a chance to show up in the numbers.

**Why it was dangerous:** This is the most dangerous shape of bug in the whole build: a wrong number that produces a *plausible*, not obviously broken, result. "The ablation shows no difference" reads as a legitimate finding (maybe outage detection genuinely doesn't help much) rather than a bug — it would have been trivially easy to report this as an honest negative result and move on, when the actual cause was that the hold duration hadn't been made long enough to prove its own value.

**How I found it:** Running the comparison across 3 seeds instead of trusting one — the result differed by seed (identical on 2, different on 1), which is the tell that something in the setup, not the underlying effect, was seed-dependent.

**The fix:** Introduced `ASSUMED_MAX_OUTAGE_DURATION_MINUTES = 100` (the policy's own conservative belief about how long an outage can run, not a read of the simulator's internal ground-truth constant) and added it to the hold offset. Re-verified across all 3 seeds: `full_agent` now beats the ablation by +12–15% total recovered ₹, consistently, on every seed. Commit `6a92840`.

**Lesson:** A null result from a single run is not evidence of "no effect" until you've checked it holds across more than one seed — a null result that only appears sometimes is usually a setup bug wearing a negative finding as a disguise.

**Video line:** Our outage-detection upgrade looked like it did nothing — turns out our hold time was too short to prove it, not that it didn't work.

---

## The security control that crashed on the exact input it was meant to catch

**Stage:** Building the LLM layer's `classify_reply()` — specifically its parsing boundary, the whitelist check meant to guarantee a customer reply can never resolve to anything outside a closed 5-value intent enum, regardless of what a model (or a compromised one) returns.

**What broke:** `_parse_classification_response()`'s core check did `if intent_value in {enum members}` — a set membership test. Fed an adversarial "compromised model" response shaped like `{"intent": ["a", "list"]}`, the `intent` value is a list, which is unhashable — and Python's `in` against a set raises `TypeError` on an unhashable operand. The function built specifically to make bad input safe crashed on bad input instead of neutralizing it.

**Why it was dangerous:** This is the boundary that stands between "a model said something we didn't expect" and "the system does something safe by construction." A `TypeError` propagating up out of a security boundary is worse than the boundary doing nothing — it's an unhandled exception in a path that's supposed to be the last line of defense against exactly this kind of malformed input, and depending on what calls it, could take down request handling rather than degrade to a safe default.

**How I found it:** An adversarial test suite that ran both a well-behaved fake model response and a deliberately malformed/adversarial one (simulating a model that got fooled despite upstream defenses) through the same parsing function — parametrized across shapes specifically chosen to be hostile (wrong types, missing fields, unhashable values, non-dict payloads), not just realistic ones.

**The fix:** Added an `isinstance(intent_value, str)` guard before the set membership check, so any non-string value (list, dict, None, etc.) resolves straight to the safe `UNCLEAR` fallback instead of ever reaching the `in` check. Commit `cfae6d8`. The same test round also caught a bug in the test itself — a valid intent with an unrelated extra field was wrongly asserted to become `UNCLEAR`, when the correct, safe behavior is to resolve the valid intent and silently ignore the extra field — split into its own test once the wrong assumption was caught, rather than "fixed" by changing the code to match a wrong test.

**Lesson:** A boundary meant to make untrusted input safe has to be tested with the input that's actually hostile to *it*, specifically — type confusion, not just wrong values, because a crash inside the safety mechanism is a worse failure than the thing it was built to prevent.

**Video line:** Our own safety filter for model output crashed on the one input shape designed to break it, until we tested for exactly that.

---

## The SQLite connection pooling bug that would have broken webhook dedup invisibly

**Stage:** Building the real execution layer — the outbox pattern, the finite state machine, and the webhook receiver — specifically while writing tests that exercise the webhook endpoint's background-task handling.

**What broke:** `sqlite:///:memory:` without `poolclass=StaticPool` gives each new database connection its own separate, empty in-memory database. FastAPI's `BackgroundTasks` (used to process a webhook after acking it fast) can open its connection on a different thread than the one that received the request — meaning the webhook handler and the background processor could each be looking at their own private, disconnected copy of "the database," neither aware the other existed.

**Why it was dangerous:** This is the exact mechanism responsible for two things at once: idempotent webhook processing (dedup by event id) and correct state transitions after a payment outcome is confirmed. If the background task's write landed in a database the rest of the system could never see, dedup checks would always report "never seen this event before" (since the row recording it live in a different in-memory instance), and state updates from webhooks could silently vanish — no exception, no error, no log line indicating anything was wrong. The system would look like it processed a webhook and simply not have any lasting record of it.

**How I found it:** The webhook test suite — tests that write via one code path and read back via another started failing in ways that only made sense if the two paths weren't looking at the same data, which is what led to checking the connection/pooling configuration rather than the webhook logic itself.

**The fix:** `db.py` now forces `StaticPool` specifically for `:memory:` connection URLs, so every connection shares the same single in-memory database regardless of which thread opened it. File-backed SQLite (used by crash-recovery tests and real runs) doesn't need this — reconnecting to a file naturally sees the same data on disk. Commit `8e1407c`.

**Lesson:** An in-memory test database's biggest risk isn't being wrong about data, it's silently not being the same database across two code paths — always confirm pooling behavior explicitly for any concurrent or multi-connection access pattern, don't assume default settings match the mental model of "one shared database."

**Video line:** Our in-memory test database was quietly giving different threads different, disconnected copies of itself — webhook dedup would have silently done nothing.

---

## The test that passed for the wrong reason

**Stage:** Wiring `due_at`-based scheduling into the real execution path — specifically, updating existing execution-layer tests to account for the fact that a scheduled action could now genuinely be "not due yet."

**What broke:** An existing end-to-end test exercising a payment expiring and getting abandoned had been passing since before `due_at` scheduling existed — but it turned out to be passing for a reason that had nothing to do with what it claimed to test. The worker's `now` in that test was never advanced past the newly-real `due_at`, so the outbox worker never actually ran and never dispatched the payment. The test still passed only because `SCHEDULED → ABANDONED` happens to be a separately legal transition in the finite state machine — the payment reached `ABANDONED` by never being attempted at all, not by being attempted, sent, and then genuinely expiring the way the test's name and intent claimed.

**Why it was dangerous:** A green test suite is only meaningful if each test is actually exercising the code path it names. This one had silently stopped testing "expiry after a real dispatch attempt" and started testing nothing more than "an untouched payment can be abandoned" — a much weaker claim, hiding behind a test name that implied the stronger one. Any regression in the actual `EXECUTING → AWAITING_CONFIRMATION → ABANDONED (expiry)` path could have shipped with this test still green.

**How I found it:** Reading the test's assertions against the newly-introduced `due_at` semantics while updating it for the scheduling change, not from a failure — the test was passing right up until this was noticed. The tell was realizing the worker's `now` parameter had never been moved forward, which meant it genuinely couldn't have run.

**The fix:** Rewrote the test to poll the worker at `due_at`, so it actually drives `EXECUTING → AWAITING_CONFIRMATION` first before the expiry/abandon path is exercised — matching what the test's name and docstring always claimed to cover. Commit `0a803b1`.

**Lesson:** A green test can be passing by accident, via a different legal path than the one it names — when changing the preconditions a test relies on (like adding real scheduling), re-read what each affected test is actually exercising, not just whether it still passes.

**Video line:** One of our own tests had been passing for a year — sorry, a session — for a completely different reason than the one it claimed to check.

---

## The 8-item architecture audit

**Stage:** Not mid-build. The simulator, the real execution layer (state machine, outbox, webhook receiver, reconciliation poller), and the LLM layer were all independently built and independently tested, 215 tests green, and the next planned step was starting the pitch video. Paused before that, and audited each documented design decision in `DECISIONS.md` against the actual code, one by one, instead of moving on.

**What broke:** Nothing crashed, and nothing was reported failing — that's the substance of this incident, not an exception to it. The audit found that several components had been *designed and written up* in `DECISIONS.md` but never actually built:
- Jittered, ramped outage resume — documented 2026-08-24 ("resume ramps a handful of probe attempts first, confirms success, then opens the gate gradually — a circuit breaker in the closed/open/half-open sense") — has no implementation anywhere in the codebase. `outage_detector.py` only ever detects an outage (a boolean verdict); nothing ramps a resume or gates reopening.
- Scheduled retries in `execution/` — `decide()` could return "retry in 4 hours," but the execution layer had no concept of "later" at all. Every scheduled intent fired the instant a worker ran, regardless of what offset the policy had actually decided. The offset was computed and then discarded — the variable was literally named `_offset` to mark it unused.
- A worker lease — nothing in `outbox.py`'s `run_outbox_worker_once` prevents two concurrent worker processes from picking up and executing the same pending intent at once; there's no row lock or lease column, only a plain read-then-update.

On top of those three, a fourth, larger gap: `full_agent` — the actual cost-aware, outage-aware decision policy, built and evaluated extensively in the simulator — had never once driven the real execution path. Only the simpler `rules_only` policy had ever been wired into `execution/`. All of the real-money-adjacent plumbing had only ever been proven against the less capable policy.

**Why it was dangerous:** The system looked complete. 215 tests were green and nothing was failing, because a test suite can only cover what was built — a component that was designed, written up, and never implemented produces no red test, since there's no code path for a test to exercise in the first place. The gaps were invisible for the most ordinary possible reason: nothing was there to complain.

**How I found it:** Not a test failure, not a live incident — stopping before starting the pitch video and deliberately auditing each documented design decision against the actual code, on the assumption that a green test suite is evidence the tests pass, not evidence the design got built.

**The fix:** Two of the four gaps were closed in this round, as one piece of work since the second is meaningless without the first: `OutboxIntent` gained an indexed `due_at` column, `run_outbox_worker_once` now filters on `due_at <= now` instead of dispatching everything unconditionally, and `orchestrator.diagnose_and_schedule` gained a `policy_fn` parameter so `full_agent` — pre-bound with its batch-level dependencies via a factory — could be wired into the real path for the first time. Commit `0a803b1`. Ramped outage resume and the worker lease remain unbuilt as of this writing — reported here as still open, not fixed and not silently dropped (see the most recent `DECISIONS.md` entry: "Not touched, per instruction: the lease and circuit breaker"). The audit's full enumeration was never written down as its own artifact; what's verifiable from the repo is the two items it prompted a fix for, and what still doesn't exist in the code today.

**Lesson:** A passing test suite proves the tests that were written are passing — it says nothing about whether everything that was designed actually got built. Audit design documents against the code directly, on a schedule, especially before anything gets presented as finished.

**Video line:** Every test was green, but three things we'd designed on paper had simply never been built — the tests couldn't fail on code that didn't exist.

---

## A misplaced edit split a DECISIONS.md entry in half

Minor. While starting the P1 worker-lease task, inserting the timezone-fix entry into `DECISIONS.md` (commit `953278a`) matched the anchor text mid-entry instead of at the true end of the opt-out entry it was following — its closing "fourth instance of the family" paragraph (four numbered points plus "The rule, extended") got separated from its own entry and stranded after the timezone entry's "Not touched, per instruction: the lease and circuit breaker" line, instead of staying attached to the opt-out write-up it belonged to. Nothing was lost — both halves were still in the file, just in the wrong order, discovered by rereading the file before adding the lease entry rather than by any test (`DECISIONS.md` isn't executable). Fixed by moving the stranded paragraph back to sit immediately after the opt-out entry, before the timezone entry, restoring true chronological order. No commit hash of its own — folded into the P1 #2 (worker lease) commit alongside the intended DECISIONS.md addition.

**Why it's worth a line and not more:** low stakes (a prose doc, not code — nothing tested it and nothing depended on its order at runtime), but it's a concrete reminder that `old_string`/`new_string` edits anchored on a substring need the substring to be unambiguous about *where the insertion point actually is*, not just present in the file — a match that's technically correct can still land in the middle of a logical unit if that unit doesn't end where the anchor text does.

---

## The worker lease's own real limitation, found by stress-testing it after it shipped

**Stage:** Right after the P1 #2 worker-lease commit landed, all tests green — asked directly "is it done right?" instead of taking the existing test suite's word for it.

**What broke:** Nothing, under any of the tests already written — every one of them simulates concurrency sequentially, calling the claim step from two "workers" one after another in the same thread. That's a real test of the claim mechanism's atomicity, but it can't surface a different class of problem: what happens when a worker's actual *processing* (not just its claim) takes longer than its own lease. Constructing that scenario deliberately — one worker claims an intent, a second worker's clock check happens after the lease would have expired and correctly (from its own perspective) reclaims and finishes the row first, then the first worker, still alive and simply slow, finally gets around to processing what it claimed originally — surfaced that its belated `append_event(EXECUTING)` call raised an *uncaught* `IllegalStateTransition`, since the payment had already moved past `EXECUTING` under the worker that got there first.

**Why it was dangerous:** The exception wasn't caught anywhere in the processing loop, so it would propagate out of `_process_claimed_intents` and abort the rest of that worker's entire claimed batch — not just the one stale intent, every other legitimate intent that same worker had also claimed in that pass. In a real deployment's poll loop, this is the shape of a rare-but-real crash: it requires actual processing to outrun the lease duration, which won't happen on every pass, but will happen eventually under real load (a slow network call, GC pause, a busy host) — and when it does, it doesn't just skip the one affected payment, it silently drops every other payment queued behind it in that worker's batch until the next poll.

**How I found it:** Not a test failure — three rounds of deliberate adversarial verification after the feature already had 6 passing tests and had been reported as done: real OS threads (8, then 10) hammering a shared file-backed SQLite database with a deliberately too-short lease (down to 1 millisecond) to try to force the race under genuine concurrency first (it held — zero errors, zero double-executions), then a hand-constructed worst case specifically targeting the lease's known theoretical weak point (a live-but-slow worker, not a dead one) once the stress tests alone didn't reproduce anything.

**The fix:** `_process_claimed_intents` now catches `IllegalStateTransition` specifically around the `EXECUTING` transition and skips that one intent — never reaches the dispatch handler, so no second real API call — while continuing to process the rest of its batch normally. The FSM's `validate_transition` was already doing the important work (genuinely preventing a second Razorpay call); the bug was in how the resulting exception propagated, not in the compliance check itself. Commit `9f9ba9f`'s follow-up (same PR/round). Documented as an explicit, permanent limitation in `outbox.py`'s docstring and `DECISIONS.md` — a time-based lease is not a perfect equivalent to Postgres's transaction-scoped `FOR UPDATE SKIP LOCKED`, and the honest mitigation is sizing the lease comfortably above realistic processing time, not a claim that this can never happen again.

**Lesson:** A green test suite for a concurrency mechanism proves the scenarios that were written are handled — it doesn't mean the mechanism has no other failure modes, especially ones that only show up under genuine timing pressure a sequential test can't construct. When a fix specifically claims to approximate a known, different mechanism (here: Postgres's `FOR UPDATE SKIP LOCKED`), the differences between the approximation and the original are exactly where to go looking for what wasn't covered yet.

**Video line:** After we shipped the lease, we didn't just trust our own tests — we threw ten real threads and a millisecond-long lease at it and found one more edge before anyone else could.

---

## The ramped outage resume made the numbers worse, and the reason was a real architectural mismatch

**Stage:** Building P1 #3 (the last of three P1 fixes) — jittered, ramped release for payments held during a detected issuer outage, replacing a fixed hold offset that made every held payment retry at the same instant once the outage cleared.

**What broke:** Nothing in the mechanism itself — a named circuit breaker (`closed`/`open`/`half_open`, derived fresh from the failure log, never a stored flag) and deterministic jitter, exactly as specified, verified correct by 9 passing tests including proof-of-load-bearing (reverting the jitter or the ramp-bucket fractions and confirming the relevant tests fail). But re-running the 3-seed harness comparison — done because the task explicitly asked whether the numbers would move — showed `full_agent`'s ₹/contact and total recovered dropping by a consistent ~4% on every seed, and its margin over the outage-detection ablation shrinking from roughly +11–15% to +7–10%. The mechanism worked exactly as built; the *net effect* on this simulator's numbers was a real decrease, not the improvement a "smarter release schedule" would suggest.

**Why it was dangerous:** Not a runtime failure — a silent, plausible-looking regression in the exact metric the whole project's pitch depends on (₹/contact). Reporting the new numbers without diagnosing *why* they moved would have meant handing over a table that looked like a step backward, with no explanation, on the eve of recording the pitch video. Worse, without digging in, the natural (wrong) conclusion would have been "the circuit breaker doesn't help" — when what actually happened is a specific, fixable mismatch between the mechanism's assumptions and this harness's architecture, not a flaw in the mechanism's own logic.

**How I found it:** Instrumented one seed's held-payment cohort directly rather than guessing from the aggregate numbers: of 25 payments held for a detected outage, 11 got an early release opportunity (probe or ramp checkpoint confirmed the breaker `CLOSED`) instead of the conservative fallback — but checking those 11 against the simulator's own ground-truth `outage_events` (not the detector's belief) showed only 2 actually landed after the true outage had ended; the other 9 landed while it was still genuinely live, guaranteeing a wasted, zero-probability attempt.

**The root cause:** The circuit breaker's `CLOSED` verdict is derived from the failure log — i.e., from *other* customers' observed failures thinning out — and this simulator's true-outage generation deliberately front-loads its failure signal into the first ~20 minutes (see the front-loaded-burst fix, an earlier incident in this log). So the detector often reads "clear" well before the true outage's full duration has actually elapsed. A real system would treat a failed early probe as informative and simply try again on the next cycle. This simulator's harness calls `decide()` exactly once per payment, with no retry-on-failure loop — so a probe landing during a still-live outage isn't "informative," it's a permanent loss. The circuit breaker pattern assumes a system that can retry a failed probe; this harness's one-shot-per-payment architecture can't, and that mismatch, not a bug in the breaker's own code, is what moved the numbers.

**The fix:** None applied, deliberately — a follow-up round confirmed keeping the honest 4% cost and explanation is the stronger result than tuning the probe/ramp checkpoints to hide it. Asked instead to substantiate the "harness artifact, not breaker flaw" claim rather than just assert it, two things confirm it precisely:
- `tests/test_full_agent_circuit_breaker.py::test_if_probes_fail_the_breaker_reopens_rather_than_widening` is the mechanism-level evidence: a burst that keeps failing past both checkpoints proves every held payment falls through to the fully conservative fallback — the breaker's own logic never releases early against a genuinely still-failing outage.
- A new permanent regression test instruments the SAME 3 real batches used for the harness comparison and counts, of the payments whose early checkpoint landed while the true outage was still live, how many would have been past it by the time the breaker's own fallback checkpoint (reached after correctly reopening) arrived: **9/9 on seed 42, 8/8 on seed 7, 9/9 on seed 123 — every single wasted probe, on every seed, would have been recoverable with a retry-on-failure loop.** The entire 4% traces to the harness's one-shot `decide()`-per-payment architecture, not to the breaker.

`DECISIONS.md` carries the full before/after table and the final 3-seed numbers going in the video.

**Lesson:** A textbook pattern (circuit breaker, probe-then-widen) carries assumptions about the system it's dropped into — specifically here, that a failed probe gets a next attempt — and those assumptions don't announce themselves; they only surface by actually measuring the pattern's effect against the target metric, not by trusting that "this is a solved, well-known mechanism" is enough on its own. And once a plausible explanation for a regression is found, the honest next step is measuring it, not just asserting it and moving on — "I believe this is a harness artifact" and "100% of it, measured across all 3 seeds, is" are different claims, and only the second one belongs in a pitch.

**Video line:** Our smarter outage-recovery logic made the numbers 4% worse — and we proved, seed by seed, that every point of that was our simulator's inability to retry a failed probe, not our breaker.

---

## The demo's outage payment silently used the wrong detector threshold

Minor. While building `scripts/demo_run.py` (`make demo`), the outage-hold payment was seeded with a 4-failure burst against the same issuer — enough to trip `detect_systemic_event` in the circuit-breaker unit tests, which deliberately use a lower `count_threshold=3` for small hand-built scenarios. Against the actual *production* config `full_agent` uses by default (`count_threshold=6`, strictly greater-than), 4 failures never breached it — the payment silently fell through to a normal ~20-minute retry instead of the jittered/ramped outage-hold path the demo exists to show off. Caught by reading the printed `due_at` (`+0:20:00`, the untouched-path value) rather than the intended multi-hour held offset, not by any test failure — nothing asserts what this demo script's own output should look like. Fixed by sizing the burst to 7 failures, correctly clearing the real threshold. Folded into the same round: a raw `timedelta` repr in the same script's `due_at` printout showed microsecond noise (`+2:15:47.733629`) before anyone but me saw it — rounded to whole seconds before committing.

**Why it's worth a line and not more:** the demo script isn't part of the system under test, so this couldn't have caused a real-world bug — but it's a concrete instance of the same lesson as the circuit-breaker regression above, at much lower stakes: two numbers that are both called "the detection threshold" can silently be different values in different parts of the same codebase (one tuned for fast, deterministic unit tests; one tuned for the actual policy), and code written against the wrong one fails silently rather than loudly, showing a *plausible*, not obviously-broken, path instead of the intended one.

---

## The recurring pattern

Four separate incidents across this build share one exact shape, found roughly three weeks apart in build time but structurally identical each time: **the system's behavior was correct, but the reason recorded for that behavior was false, or nothing was recorded at all.** This matters specifically because the audit trail here is append-only by design (see DECISIONS.md's original architecture decision) — the entire point of that choice is that "what actually happened" should be provable from the log rather than trusted on faith. Each of these four incidents is a way that guarantee can quietly fail without any single line in the log being individually wrong.

**Instance 1 — `ABANDONED` collapsing three meanings into one state.**
Caught during design of the execution layer's finite state machine, before it was ever built the wrong way: the original plan for a terminal `ABANDONED` state had no way to distinguish "the policy chose not to act," "the Razorpay API call itself failed," and "the webhook confirmed the customer simply didn't pay." All three are legitimate, correct outcomes — but collapsed into one unstructured state, the log could show a payment was given up on and never say which of three very different reasons applied, which matters enormously for debugging and for the next policy iteration. Fixed before commit: `AbandonReason` is now a required enum (`policy_stop` / `execution_error` / `payment_failed`) enforced by `validate_transition()` — no transition to `ABANDONED` is legal without one, and no other transition may carry one. Landed in commit `8e1407c`.

**Instance 2 — `OPT_OUT` implemented as `contact_count` inflation.**
The original plan for handling an explicit customer opt-out (before implementation) was to inflate the tracked `contact_count` past the weekly contact cap. This would have worked — further contact really would have been blocked — but the audit log would have recorded "hit the weekly rate limit," when the true reason was "the customer asked to stop." Same defect shape as Instance 1: a correct-looking number that answers the wrong question the moment someone reads it back. Caught and corrected before implementation; `full_agent.decide()` instead gained `explicit_opt_out` as its own separately-named hard veto. Landed in commit `cfae6d8`.

**Instance 3 — `SIMULATED_SENT` missing, so a working simulated action would be recorded as a failure.**
A simulated action (a logged nudge or retry with no real Razorpay primitive behind it, added when `full_agent`'s full action set was wired into execution) transitioned to `AWAITING_CONFIRMATION` exactly like a real send, for FSM consistency. But nothing could ever confirm it — no webhook arrives for a send that never happened — so the reconciliation poller would eventually query Razorpay, correctly find no record, and write `ABANDONED(execution_error)`: a literal false statement in an append-only ledger, saying the system failed when it had in fact worked exactly as designed. Fixed by adding a genuinely terminal `SIMULATED_SENT` state, reached directly from `EXECUTING`, which the reconciliation poller's stale-state check excludes entirely — not "checks and skips," never even considers it a candidate. Landed in commit `9f02973`.

**Instance 4 — opt-out never persisted at all.**
The most recent, and different in kind from the first three: this time the recorded reason wasn't false, because nothing was recorded at all. An explicit customer opt-out lived only in an in-memory tracker inside `apply_reply_intent` — correct for the remainder of that process's life, but invisible to any future process. Customer says stop, the process restarts, the system contacts them again, with no audit entry anywhere to explain why the veto that should have applied didn't. Fixed with a dedicated, durable, append-only `CustomerOptOutEvent` table that `apply_reply_intent` writes to directly and `contact_tracker_for()` reads back on every call, so the veto survives a restart rather than living only in one process's memory. Landed in commit `03f169c`.

**The rule that came out of this, stated in full:** every time a new state, transition, or guardrail is added to an append-only audit log, it isn't enough to ask "does this produce the right behavior." Two further questions have to be asked every time: "will the recorded reason still be TRUE at every point someone might read it back" (instances 1–3), and "is this fact recorded at all" (instance 4). A false entry in an append-only log can't be edited later — it sits there permanently, indistinguishable from a true one to anyone reading the trail after the fact. A missing entry is arguably worse: a false entry is at least visible and falsifiable on inspection; a guardrail that only ever existed in memory leaves nothing in the log to show it was there, or that it's now gone. This isn't a one-off bug pattern to fix case by case — it's a standing hazard of the append-only design itself, the same design chosen specifically for "proof rather than trust," and it needed to be checked for explicitly on every new addition, not caught by accident four times running.

**A second family: invisible because nothing was complaining.**
The four incidents above all involve a mistaken *reason*, or a missing one. A separate, equally recurring family in this build involves no reason at all — no error, no failing assertion, nothing to read back and question, because nothing was watching that specific thing in the first place:
- The double-charge invariant that was implemented, reported, and green in every run — because an early-stop bug in `run_policy()` made the condition it was built to catch physically impossible to produce. A metric that has never fired is not evidence of safety until it's confirmed the metric could fire at all.
- The 60-minute hold offset whose outage-detection ablation showed *zero measurable difference* on 2 of 3 seeds — a result that reads as a legitimate negative finding, not a bug, unless it's specifically checked against more than one seed.
- The timezone bug (contact-hours veto evaluating the wrong clock, `git log` commit `953278a`) — 215 tests were green the entire time it existed, because no test constructed a scenario where a UTC-hosted deployment's real clock would diverge from a customer's IST-declared window; every test either ran the simulator's own IST-naive convention or never separated the two zones enough to notice.
- This round's architecture audit itself — three designed components with no implementation at all, found not because anything broke but because of a deliberate pause to check design against code before treating the build as finished.

**Both families share one root, and it's the closing thought of this log:** green results are not evidence of correctness. A test suite proves the tests that were written are passing; it says nothing about whether the condition worth testing can even occur (family 1, above) or whether everything that was designed actually got built (family 2, here). Neither family announces itself — the first hides behind a plausible, correct-looking log entry; the second hides behind the simple absence of anything to look at. Both were only found by going looking on purpose: adversarial input for the parsing boundary, multi-seed sweeps instead of single runs, a manual verification step that couldn't be skipped, and — for the last two entries here — stopping deliberately before calling the build finished and checking documented intent against actual code, rather than trusting a green run as proof there was nothing left to check.

---

## Top 3 for the form

**1. The recurring pattern (all four instances, but lead with `SIMULATED_SENT` and the opt-out persistence gap).**
This is the strongest thing in the build because it isn't one bug — it's the same specific defect shape recurring across four unrelated parts of the system, which is evidence of a general design blind spot being found and named, not a one-off mistake being patched. It shows judgment: the response wasn't "fix this instance" three times, it was "state the rule" after the third, and then correctly recognize the fourth as a variant of the same rule rather than a new problem. The failure mode is genuinely non-obvious — an append-only audit log is specifically the design chosen to make the system trustworthy, and this is a way that exact choice can quietly betray itself.

**2. The 60-minute hold duration producing a plausible null result on 2 of 3 seeds.**
This is the best evidence of *not* getting fooled by a comfortable answer. "The ablation shows no measurable difference" is a result that's easy to accept and move on from — it doesn't look like a bug, it looks like an honest negative finding. Catching it required not trusting a single run and specifically comparing across seeds. This is the incident most likely to be genuinely missed by someone less careful, because nothing about it announces itself as wrong.

**3. The SQLite `StaticPool` cross-thread bug.**
This is the strongest build-quality evidence: a real, subtle bug in infrastructure code (connection pooling under a background-task/multi-thread access pattern) that would have caused *silent* data loss — no exception, no log line, just a webhook that appeared to process successfully and then vanished. It was found by a test suite actually exercising the concurrent access pattern realistically, not by a spec check, and the fix was scoped precisely (only `:memory:` URLs, not file-backed SQLite) rather than applied as a blanket workaround.

---

## Mapped to their rubric

**Problem taste**
- The recurring pattern (naming a family of bugs and stating the general rule, rather than fixing four instances independently)
- The self-audit and what it found unbuilt (going looking for integration gaps deliberately, instead of waiting for one to surface)
- Outage duration vs. naive retry cadence (recognizing the demo had nothing to show *before* trusting its output)

**Build quality**
- The SQLite `StaticPool` cross-thread bug (correct, narrowly-scoped fix to a genuine infrastructure defect)
- The vacuously-true double-charge invariant (fixing the actual control-flow bug, not just re-describing the metric)
- `ABANDONED` collapsing three meanings / `SIMULATED_SENT` (structured state machine design, enforced at the FSM level, not by convention)

**AI judgment**
- The TypeError in the classification parsing boundary (adversarial testing of the exact component meant to make untrusted model output safe, and treating a crash in a security boundary as more serious than a wrong value)
- The webhook event-id bug (verifying an assumed contract against real captured data rather than trusting documentation or convention)
- The invalid test card (checking a "well-known" value against the live current API instead of memory)

**Failure recovery**
- The 60-minute hold duration (catching a plausible-but-wrong negative result by re-checking across seeds, not accepting the first answer)
- The test that passed for the wrong reason (noticing a green test was proving a weaker claim than it named, and fixing the test's actual coverage, not just its assertions)
- The outage detector's decoy false positives (two full rounds — first reporting the limitation honestly rather than papering over it with a cherry-picked threshold, then fixing the actual root cause and re-verifying across three seeds before trusting it)
