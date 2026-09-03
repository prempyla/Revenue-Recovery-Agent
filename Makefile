.PHONY: help install test demo reply-demo crash-demo charts clean

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

help:
	@echo "Razorpay Revenue Recovery Agent -- available targets:"
	@echo "  make install     create .venv and install dependencies"
	@echo "  make test        run the full test suite (245+ tests)"
	@echo "  make demo        seeded end-to-end run through the real execution pipeline"
	@echo "  make reply-demo  customer replies STOP -> live opt-out -> vetoes the next decide()"
	@echo "  make crash-demo  kill -9 a worker mid-dispatch, restart it, reconcile the result"
	@echo "  make charts      regenerate the three evaluation charts in docs/charts/"

install:
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "Done. Copy .env.example to .env and fill in test-mode keys before running"
	@echo "anything that touches a real API (create_demo_link, run_webhook_server)."
	@echo "Nothing below this needs real keys -- everything runs against fakes."

test:
	$(PYTHON) -m pytest tests/ -q

demo:
	$(PYTHON) scripts/demo_run.py

reply-demo:
	$(PYTHON) scripts/demo_reply.py

crash-demo:
	$(PYTHON) scripts/demo_crash_recovery.py

charts:
	$(PYTHON) docs/charts/generate_charts.py

clean:
	find . -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -not -path "./.venv/*" -delete
