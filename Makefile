.PHONY: run test clean

run:
	PYTHONPATH="" /opt/core/venv/bin/python3 app.py

test:
	PYTHONPATH="" /opt/core/venv/bin/python3 -m pytest tests/ -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; \
	true
