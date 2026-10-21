"""Opt-in disposable PostgreSQL validation; never targets a production-named DB."""
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
pytest.importorskip("psycopg")
from ledger.anomaly.state import State


@pytest.mark.skipif(not os.environ.get("ANOMALY_TEST_DATABASE_URL"), reason="Requires a disposable PostgreSQL database")
def test_checkpoint_restart_and_transaction_rollback():
    uri = os.environ["ANOMALY_TEST_DATABASE_URL"]
    assert urlparse(uri).path.lstrip("/").startswith("ledger_anomaly_test_"), "Use an explicitly disposable ledger_anomaly_test_* DB"
    key = "test:" + uuid4().hex
    state = State(uri)
    state.initialize()
    try:
        with pytest.raises(RuntimeError):
            with state.atomic():
                state.capture(key, key, {"safe": True}, {"token": key})
                raise RuntimeError("crash before commit")
        assert state.checkpoint(key) is None
        assert state.capture(key, key, {"safe": True}, {"token": key})
        state.close()
        state = State(uri)
        assert state.checkpoint(key) == {"token": key}
        assert not state.capture(key, key, {"safe": True}, {"token": key})
        with pytest.raises(RuntimeError):
            with state.atomic():
                state.advance_checkpoint(key, {"idle": 1})
                raise RuntimeError("crash before checkpoint commit")
        assert state.checkpoint(key) == {"token": key}
        for idle in range(100):
            state.advance_checkpoint(key, {"idle": idle})
        state.close()
        state = State(uri)
        assert state.checkpoint(key) == {"idle": 99}
        assert state.connection.execute("SELECT count(*) AS n FROM anomaly_events WHERE collection=%s", (key,)).fetchone()["n"] == 1
    finally:
        state.connection.execute("DELETE FROM anomaly_events WHERE id=%s", (key,))
        state.connection.execute("DELETE FROM anomaly_checkpoints WHERE collection=%s", (key,))
        state.close()
