.PHONY: run test clean

run:
	PYTHONPATH="" /opt/core/venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8080

test:
	PYTHONPATH="" /opt/core/venv/bin/python3 -m pytest tests/ -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; \
	rm -f /tmp/core_session_*.xml /tmp/import_core.xml /tmp/exec_script.py 2>/dev/null; \
	true