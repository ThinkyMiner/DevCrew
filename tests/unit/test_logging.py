# tests/unit/test_logging.py
import json
import logging

from app.config.logging import configure_logging, run_id_var


def test_log_line_is_json_with_run_id(capsys):
    configure_logging(json_to_stderr=True)
    token = run_id_var.set("run-123")
    try:
        logging.getLogger("team.test").info("hello", extra={"event": "unit"})
    finally:
        run_id_var.reset(token)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["run_id"] == "run-123"
    assert payload["event"] == "unit"
