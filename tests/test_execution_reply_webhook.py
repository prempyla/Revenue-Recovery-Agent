"""execution/reply_webhook.py: the live path closing external cold review
Finding 2 (2026-08-27, see INCIDENTS.md) -- llm.classify_reply/apply_reply_
intent were built and adversarially tested but called from nowhere outside
tests, so explicit_opt_out could never fire in a real run. These tests drive
the actual POST /replies route (fastapi.testclient.TestClient -- in-process
ASGI, no real network) and confirm the full chain: classify -> apply ->
durable CustomerOptOutEvent -> a FRESH query_layer.contact_tracker_for()
reconstruction (a brand-new engine/session, never the one that wrote it) ->
full_agent.decide()'s explicit_opt_out veto on the customer's next payment.

Deliberately does NOT hand-construct a ContactTracker anywhere below -- that
would just be another test-shaped caller, the exact gap this closes."""

import tempfile
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from execution.db import make_engine, make_session_factory
from execution.orchestrator import diagnose_and_schedule
from execution.query_layer import contact_tracker_for, recent_failures
from execution.reply_webhook import create_reply_app, handle_reply
from llm.client import FakeLLMClient
from simulator.full_agent import make_full_agent_policy
from simulator.types import Customer, DeclineReason, InstrumentType, Language, Payment

WINDOW_START = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

PERSONA = Customer(
    customer_id="cust_reply_webhook",
    recovery_propensity=0.6,
    funds_arrival_day=2,
    contact_hours=(0, 24),
    annoyance_threshold=99,
    channel_response_rate={"upi_link": 0.7, "whatsapp": 0.6, "sms": 0.5},
    preferred_language=Language.EN,
)


def _diagnosed_payment(session, payment_id, failed_at, decline_reason=DeclineReason.AFA_3DS_DROPOFF):
    payment = Payment(
        payment_id=payment_id,
        customer_id=PERSONA.customer_id,
        amount_paise=250_000,
        instrument_type=InstrumentType.CARD,
        decline_reason=decline_reason,
        issuer_code="HDFC",
        failed_at=failed_at,
    )
    tracker = contact_tracker_for(session, PERSONA.customer_id)
    policy = make_full_agent_policy(failure_log=recent_failures(session, failed_at), outage_events=[])(tracker)
    return payment, diagnose_and_schedule(session, payment, PERSONA, failed_at, policy_fn=policy)


def test_opt_out_reply_through_the_route_persists_and_survives_a_fresh_engine():
    """:memory: wouldn't prove this -- a truly fresh engine doesn't share
    it. File-backed, same pattern as
    test_execution_query_layer.py::test_opt_out_survives_a_restart..."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_url = f"sqlite:///{tmp_dir}/execution.db"
        engine = make_engine(db_url)
        session_factory = make_session_factory(engine)
        session = session_factory()

        payment, intent = _diagnosed_payment(session, "pay_webhook_1", WINDOW_START)
        assert intent is not None  # scheduled, no opt-out yet

        llm_client = FakeLLMClient()
        llm_client.classify_response = {"intent": "OPT_OUT", "promised_date": None}
        app = create_reply_app(session_factory, llm_client=llm_client)
        client = TestClient(app)

        response = client.post(
            "/replies",
            json={
                "customer_id": PERSONA.customer_id,
                "payment_id": payment.payment_id,
                "reply_text": "please stop texting me",
                "contact_hours": [0, 24],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "OPT_OUT"
        assert body["payment_found"] is True

        # A genuinely NEW engine reconnecting to the same file -- nothing
        # in memory carried over.
        fresh_engine = make_engine(db_url)
        fresh_session = make_session_factory(fresh_engine)()
        tracker = contact_tracker_for(fresh_session, PERSONA.customer_id)
        assert tracker.is_opted_out(PERSONA.customer_id) is True


def test_opt_out_via_the_live_route_vetoes_the_customers_next_decide_through_query_layer():
    """THE required test: an end-to-end run where a customer replies STOP,
    and a SUBSEQUENT diagnose_and_schedule() for that customer is vetoed by
    explicit_opt_out -- with the tracker sourced from
    query_layer.contact_tracker_for() against a fresh engine/session, never
    hand-built. Uses handle_reply() (what create_reply_app's route calls)
    with an injected `now`, the same route/logic split webhook.py already
    uses for process_webhook_event -- so the opt-out timestamp and the
    second payment's failed_at sit on the same deterministic timeline,
    rather than mixing a synthetic WINDOW_START against the route's real
    utc_now() (which would make is_opted_out's as_of comparison meaningless:
    see test_reply_route_via_http_also_persists_and_vetoes below for the
    real-clock-anchored version of the same claim through actual HTTP)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_url = f"sqlite:///{tmp_dir}/execution.db"
        engine = make_engine(db_url)
        session_factory = make_session_factory(engine)
        session = session_factory()

        first_payment, first_intent = _diagnosed_payment(session, "pay_webhook_before", WINDOW_START)
        assert first_intent is not None  # unvetoed: no opt-out yet

        llm_client = FakeLLMClient()
        llm_client.classify_response = {"intent": "OPT_OUT", "promised_date": None}
        opt_out_at = WINDOW_START + timedelta(hours=1)
        result = handle_reply(
            session, PERSONA.customer_id, first_payment.payment_id, "STOP", opt_out_at, llm_client=llm_client
        )
        assert result["intent"] == "OPT_OUT"
        assert result["payment_found"] is True
        session.commit()

        # Fresh engine/session -- the veto must survive this, not just be
        # readable from the same in-process objects that wrote it.
        fresh_engine = make_engine(db_url)
        fresh_session = make_session_factory(fresh_engine)()

        second_failed_at = opt_out_at + timedelta(days=1)  # strictly after the opt-out
        second_payment, second_intent = _diagnosed_payment(
            fresh_session, "pay_webhook_after", second_failed_at, decline_reason=DeclineReason.INSUFFICIENT_FUNDS
        )
        assert second_intent is None  # vetoed by explicit_opt_out, via the live path

        from execution.eventlog import history

        events = history(fresh_session, second_payment.payment_id)
        assert events[-1].abandon_reason == "policy_stop"


def test_reply_route_via_http_also_persists_and_vetoes():
    """The same claim as the test above, but through actual HTTP (real
    utc_now() inside the route, no injected `now`) -- proves the wiring
    itself works, not just handle_reply()'s internals. Real-clock-anchored
    throughout (payment failed_at values derived from utc_now(), like
    scripts/demo_reply.py) so nothing here mixes a synthetic historical
    timestamp against the route's genuinely-current opt-out event."""
    from execution.clock import utc_now

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_url = f"sqlite:///{tmp_dir}/execution.db"
        engine = make_engine(db_url)
        session_factory = make_session_factory(engine)
        session = session_factory()

        first_payment, first_intent = _diagnosed_payment(session, "pay_http_before", utc_now())
        assert first_intent is not None

        llm_client = FakeLLMClient()
        llm_client.classify_response = {"intent": "OPT_OUT", "promised_date": None}
        app = create_reply_app(session_factory, llm_client=llm_client)
        client = TestClient(app)
        response = client.post(
            "/replies",
            json={
                "customer_id": PERSONA.customer_id,
                "payment_id": first_payment.payment_id,
                "reply_text": "STOP",
                "contact_hours": [0, 24],
            },
        )
        assert response.status_code == 200

        fresh_engine = make_engine(db_url)
        fresh_session = make_session_factory(fresh_engine)()
        second_payment, second_intent = _diagnosed_payment(
            fresh_session, "pay_http_after", utc_now(), decline_reason=DeclineReason.INSUFFICIENT_FUNDS
        )
        assert second_intent is None  # vetoed by explicit_opt_out, through real HTTP end to end


def test_reply_route_does_not_fabricate_a_payment_that_was_never_diagnosed():
    session_factory = make_session_factory(make_engine("sqlite:///:memory:"))
    llm_client = FakeLLMClient()
    llm_client.classify_response = {"intent": "OPT_OUT", "promised_date": None}
    app = create_reply_app(session_factory, llm_client=llm_client)
    client = TestClient(app)

    response = client.post(
        "/replies",
        json={"customer_id": "cust_unknown", "payment_id": "pay_never_diagnosed", "reply_text": "STOP"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["payment_found"] is False
    assert body["outcome"] is None


def test_promise_to_pay_through_the_live_route_reevaluates_via_the_real_decide_path():
    """Ties Finding 1 (decide()'s now fix) and Finding 2 (this route)
    together: a PROMISE_TO_PAY reply through the live route re-evaluates
    the actual persisted payment via payment_by_id(), not a fabricated one,
    and produces a real (offset, action) result."""
    session_factory = make_session_factory(make_engine("sqlite:///:memory:"))
    session = session_factory()

    payment, intent = _diagnosed_payment(
        session, "pay_webhook_promise", WINDOW_START, decline_reason=DeclineReason.INSUFFICIENT_FUNDS
    )
    assert intent is not None

    llm_client = FakeLLMClient()
    llm_client.classify_response = {"intent": "PROMISE_TO_PAY", "promised_date": "2026-01-10"}
    app = create_reply_app(session_factory, llm_client=llm_client)
    client = TestClient(app)

    response = client.post(
        "/replies",
        json={
            "customer_id": PERSONA.customer_id,
            "payment_id": payment.payment_id,
            "reply_text": "will pay on 10th Jan",
            "contact_hours": [0, 24],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "PROMISE_TO_PAY"
    assert body["payment_found"] is True
    assert body["outcome"] is not None
    assert body["outcome"]["action_type"] == "send_payment_link"
