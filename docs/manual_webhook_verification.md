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
   This prints a `short_url` (e.g. `https://rzp.io/i/...`). Open it in a
   browser and pay using [Razorpay's published test
   credentials](https://razorpay.com/docs/payments/payments/test-card-upi-details/)
   (a test card number, any future expiry, any CVV; or a test UPI VPA).

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

_(not yet recorded — fill in after running the procedure above)_
