"""Entrypoint to run the webhook receiver locally against a real SQLite file
and .env-configured secret. See docs/manual_webhook_verification.md for the
full manual verification procedure this exists to support.

Usage: .venv/bin/python -m execution.run_webhook_server
"""

import os

import dotenv
import uvicorn

from .db import make_engine, make_session_factory
from .webhook import create_app

dotenv.load_dotenv()


def main() -> None:
    webhook_secret = os.environ["RAZORPAY_WEBHOOK_SECRET"]
    engine = make_engine("sqlite:///execution.db")
    session_factory = make_session_factory(engine)
    app = create_app(session_factory, webhook_secret)
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
