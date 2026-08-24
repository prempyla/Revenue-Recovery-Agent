# Manual webhook verification

`src/execution/webhook.py` parses incoming webhooks against an **assumed**
JSON envelope, built from Razorpay's documented shape but never checked
against a real webhook delivery:

```json
{
  "event": "payment_link.paid",
  "payload": {
    "payment_link": {
      "entity": { "reference_id": "..." }
    }
  }
}
```

Nothing in this repo can verify that shape by itself — Razorpay's test mode
doesn't auto-pay a link; a human has to open it and pay with test
credentials, and Razorpay needs a real reachable URL to deliver the webhook
to, which `localhost` isn't. This is that one manual step. Do it once,
record what actually arrived, and if it differs from the assumption above,
fix `process_webhook_event()` in `webhook.py` to match — the recorded
payload wins, not this doc.

## Prerequisites

- `.env` has real `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` (test mode) already.
- Install [ngrok](https://ngrok.com/download) (or any tunnel tool) — needed
  because Razorpay's dashboard can't send a webhook to `localhost`.
- `.venv/bin/pip install -r requirements.txt` (pulls in `fastapi`, `uvicorn`,
  `razorpay`, `python-dotenv`).

## Steps

1. **Pick a webhook secret and add it to `.env`:**
   ```bash
   echo "RAZORPAY_WEBHOOK_SECRET=whatever_you_choose_here" >> .env
   ```
   (You'll enter this exact same value in the Razorpay dashboard in step 4 —
   it's not issued by Razorpay, you set it yourself.)

2. **Start the webhook receiver locally:**
   ```bash
   .venv/bin/python -m execution.run_webhook_server
   ```
   This runs on `http://localhost:8000/webhooks/razorpay`, backed by a real
   `execution.db` SQLite file (not `:memory:`, so events persist across
   this whole procedure).

3. **In a second terminal, tunnel it publicly:**
   ```bash
   ngrok http 8000
   ```
   Note the `https://....ngrok-free.app` URL it prints. ngrok also runs a
   local inspector UI at **http://127.0.0.1:4040** — open that in a browser
   now. It will show you the raw request Razorpay sends, exactly as
   received, which is what you'll use in step 6.

4. **Configure the webhook in the Razorpay dashboard** (test mode, Settings →
   Webhooks → Add New Webhook):
   - URL: `https://<your-ngrok-subdomain>.ngrok-free.app/webhooks/razorpay`
   - Secret: the same value you put in `.env` in step 1
   - Active events: enable at least `payment_link.paid` (and
     `payment_link.expired` if you want to check that path too)

5. **Create a real test-mode payment link and pay it:**
   ```bash
   .venv/bin/python -m execution.create_demo_link
   ```
   This prints a `short_url` (e.g. `https://rzp.io/rzp/...`). Open it in a
   browser and pay with a documented **domestic** test card — `4111 1111
   1111 1111` looked like a standard test number but is NOT actually in
   Razorpay's current domestic list and gets rejected as "international
   cards are not supported" (confirmed 2026-08-24). Use one that's
   confirmed to work instead:
   - Card: `4100 2800 0000 1007`, any future expiry, any CVV
   - Then any 4–6 digit OTP to succeed (under 4 digits deliberately fails)

6. **Read the actual payload from the ngrok inspector** (http://127.0.0.1:4040):
   click the `POST /webhooks/razorpay` request, view the raw request body.
   Compare its structure to the assumed envelope at the top of this file:
   - Does the top-level event name match `payment_link.paid`?
   - Is the reference_id really at `payload.payment_link.entity.reference_id`?
   - What's the top-level event-id field actually called — `id`? something else?
   - Check the terminal running `run_webhook_server.py` too: a 200 response
     there confirms signature verification and dedup both worked against a
     real signed request, not just the test suite's synthetic ones.

7. **Record what you found.** Paste the actual captured JSON body below this
   line (redact nothing structural — amounts/ids from a Rs 1 test link
   aren't sensitive), and if any field name differs from the assumption,
   update `process_webhook_event()` in `src/execution/webhook.py` to match
   the recorded reality, not the other way around.

---

## Actual captured payload

**Recorded 2026-08-24.** Ran the full procedure above. Full payloads (email/phone
redacted before committing — this repo is public) are saved as
`tests/fixtures/real_webhook_payment_link_paid.json` and
`real_webhook_payment_failed.json`, and exercised directly by
`tests/test_execution_webhook.py::test_real_payment_link_paid_fixture_parses_and_recovers`.

What matched the assumption:
- `payload.payment_link.entity.reference_id` — correct structure, no change needed.
- Top-level event name `payment_link.paid` — correct.

What didn't match (fixed in `webhook.py`, see DECISIONS.md 2026-08-25):
- **Event id location.** The body has no `id` or `event_id` field at all —
  every one of 6 real deliveries carried it only in the
  `X-Razorpay-Event-Id` header. The endpoint now reads the header instead
  of the body.
- Observed as a side effect: Razorpay actually retries a failing webhook
  (every event was redelivered multiple times while our endpoint 400'd on
  the event-id bug above) — confirms at-least-once delivery is real
  behavior, not just documentation.

Fix verified twice: the fixture-based unit test above, and a live replay
of the exact captured payload against the running (fixed) local server —
which went from `400` to `200`.
