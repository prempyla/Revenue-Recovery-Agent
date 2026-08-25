"""Isolation tests for the webhook receiver: signature verification, event-id
dedup, and process_webhook_event's RECOVERED / payment_failed transitions."""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from execution.db import make_engine, make_session_factory
from execution.eventlog import append_event, derive_state, history
from execution.idempotency import make_idempotency_key
from execution.states import AbandonReason, PaymentState
from execution.webhook import create_app, is_duplicate_event, process_webhook_event

WEBHOOK_SECRET = "test_webhook_secret"


def _session_factory():
    engine = make_engine("sqlite:///:memory:")
    return make_session_factory(engine)


def _to_awaiting_confirmation(session, payment_id, now):
    append_event(session, payment_id, PaymentState.AT_RISK, now)
    append_event(session, payment_id, PaymentState.DIAGNOSED, now)
    append_event(session, payment_id, PaymentState.SCHEDULED, now)
    append_event(session, payment_id, PaymentState.EXECUTING, now)
    append_event(session, payment_id, PaymentState.AWAITING_CONFIRMATION, now)
    session.commit()


def _sign(body_bytes: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def test_bad_signature_is_rejected():
    session_factory = _session_factory()
    app = create_app(session_factory, WEBHOOK_SECRET)
    client = TestClient(app)

    body = json.dumps({"event": "payment_link.paid", "payload": {}}).encode()
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": "wrong", "X-Razorpay-Event-Id": "evt_1"},
    )
    assert response.status_code == 400


def test_missing_event_id_header_is_rejected():
    """Verified against 6 real webhook deliveries (see webhook.py's module
    docstring): the event id is ONLY in the header, never the body. A
    request with a valid signature but no X-Razorpay-Event-Id header must
    still be rejected, not silently accepted with no dedup key."""
    session_factory = _session_factory()
    app = create_app(session_factory, WEBHOOK_SECRET)
    client = TestClient(app)

    body = json.dumps({"event": "payment_link.paid", "payload": {}}).encode()
    response = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": _sign(body)}
    )
    assert response.status_code == 400


def test_valid_signature_is_accepted_and_processed():
    session_factory = _session_factory()
    session = session_factory()
    idem_key = make_idempotency_key("pay_x", 1)
    _to_awaiting_confirmation(session, "pay_x", datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.close()

    app = create_app(session_factory, WEBHOOK_SECRET)
    client = TestClient(app)

    body_dict = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"reference_id": idem_key}}},
    }
    body = json.dumps(body_dict).encode()
    response = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": _sign(body), "X-Razorpay-Event-Id": "evt_1"},
    )
    assert response.status_code == 200

    verify_session = session_factory()
    assert derive_state(verify_session, "pay_x") == PaymentState.RECOVERED


def test_duplicate_event_id_is_deduped_and_processed_only_once():
    session_factory = _session_factory()
    session = session_factory()
    idem_key = make_idempotency_key("pay_x", 1)
    _to_awaiting_confirmation(session, "pay_x", datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.close()

    app = create_app(session_factory, WEBHOOK_SECRET)
    client = TestClient(app)

    body_dict = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"reference_id": idem_key}}},
    }
    body = json.dumps(body_dict).encode()
    headers = {"X-Razorpay-Signature": _sign(body), "X-Razorpay-Event-Id": "evt_dup"}

    first = client.post("/webhooks/razorpay", content=body, headers=headers)
    second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == 200 and first.json().get("duplicate") is not True
    assert second.status_code == 200 and second.json()["duplicate"] is True

    verify_session = session_factory()
    events = history(verify_session, "pay_x")
    recovered_events = [e for e in events if e.to_state == "recovered"]
    assert len(recovered_events) == 1  # processed exactly once, not twice


def test_process_webhook_event_payment_expired_abandons_with_payment_failed_reason():
    session_factory = _session_factory()
    session = session_factory()
    idem_key = make_idempotency_key("pay_x", 1)
    _to_awaiting_confirmation(session, "pay_x", datetime(2026, 1, 1, tzinfo=timezone.utc))

    payload = {
        "event": "payment_link.expired",
        "payload": {"payment_link": {"entity": {"reference_id": idem_key}}},
    }
    process_webhook_event(session, payload, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert derive_state(session, "pay_x") == PaymentState.ABANDONED
    last_event = history(session, "pay_x")[-1]
    assert last_event.abandon_reason == AbandonReason.PAYMENT_FAILED.value


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_real_payment_link_paid_fixture_parses_and_recovers():
    """Real webhook payload captured 2026-08-24 via ngrok's inspector during
    manual verification (docs/manual_webhook_verification.md) -- not a
    hand-written approximation. Confirms process_webhook_event handles the
    actual structure Razorpay sends, not just our assumption of it."""
    fixture = json.loads((FIXTURES_DIR / "real_webhook_payment_link_paid.json").read_text())
    assert fixture["event"] == "payment_link.paid"
    reference_id = fixture["payload"]["payment_link"]["entity"]["reference_id"]

    session_factory = _session_factory()
    session = session_factory()
    payment_id = reference_id.split(":attempt:")[0]
    _to_awaiting_confirmation(session, payment_id, datetime(2026, 1, 1, tzinfo=timezone.utc))

    acted_on = process_webhook_event(session, fixture, datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert acted_on == payment_id
    assert derive_state(session, payment_id) == PaymentState.RECOVERED


def test_real_payment_failed_fixture_is_ignored_not_acted_on():
    """Real payment.failed payload -- not a payment_link event, so
    process_webhook_event correctly leaves state untouched rather than
    raising or guessing at an unrelated event type."""
    fixture = json.loads((FIXTURES_DIR / "real_webhook_payment_failed.json").read_text())
    assert fixture["event"] == "payment.failed"

    session_factory = _session_factory()
    session = session_factory()
    acted_on = process_webhook_event(session, fixture, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert acted_on is None


def test_is_duplicate_event_false_before_seen_true_after():
    session_factory = _session_factory()
    session = session_factory()
    assert is_duplicate_event(session, "evt_new") is False
    from execution.webhook import mark_event_seen

    mark_event_seen(session, "evt_new", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert is_duplicate_event(session, "evt_new") is True
