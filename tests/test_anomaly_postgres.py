"""Opt-in disposable PostgreSQL validation; never targets a production-named DB."""
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
pytest.importorskip("psycopg")
from ledger.anomaly.state import State
from datetime import datetime, timezone


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


@pytest.mark.skipif(not os.environ.get('ANOMALY_TEST_DATABASE_URL'), reason='Requires a disposable PostgreSQL database')
def test_security_atomic_evidence_rollups_restart_and_reporting_permissions():
    from psycopg import sql, errors
    uri = os.environ['ANOMALY_TEST_DATABASE_URL']
    assert urlparse(uri).path.lstrip('/').startswith('ledger_anomaly_test_')
    key = 'security-test:' + uuid4().hex
    role = 'security_reader_' + uuid4().hex
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    event = {'id': key, 'source_key': key, 'kind': 'profile_change', 'at': now, 'member_id': 'member1', 'actor_id': None}
    state = State(uri)
    state.connection.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
    state.initialize(reporting_user=role)
    try:
        with pytest.raises(RuntimeError):
            with state.atomic():
                state.capture(key, key, {'safe': True}, {'token': key})
                state.security_replace(key, [event])
                state.security_put_rollup(now, key, {'at': now, 'kind': key, 'value': 1})
                raise RuntimeError('crash')
        assert not state.security_events(source=key)
        assert state.checkpoint(key) is None
        assert not any(r['kind']==key for r in state.security_rollups())
        with state.atomic():
            state.capture(key, key, {'safe': True}, {'token': key})
            assert state.security_replace(key, [event])
            state.security_put_rollup(now, key, {'at': now, 'kind': key, 'value': 1})
            state.processed(key)
        state.close()
        state = State(uri)
        assert state.security_events(source=key)==[event]
        assert not state.security_replace(key,[event])
        assert state.security_events(kind='profile_change',member='member1',since=now)
        assert state.checkpoint(key)=={'token':key}
        assert state.security_replace(key,[])
        assert not state.security_get_event(key)
        state.put('security_report_metrics',key,{'report':'account_changes','cohort':'profile_change','value':1,'sample_size':1})
        state.connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        assert not state.connection.execute("SELECT * FROM reporting.cohort_metrics WHERE report='security_account_changes'").fetchall()
        for table in ('anomaly_security_events','anomaly_security_rollups','anomaly_documents'):
            with pytest.raises(errors.InsufficientPrivilege):
                state.connection.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table)))
        state.connection.execute('RESET ROLE')
        state.put('security_report_metrics',key,{'report':'account_changes','cohort':'profile_change','value':5,'sample_size':5})
        state.connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        assert state.connection.execute("SELECT * FROM reporting.cohort_metrics WHERE report='security_account_changes'").fetchone()['value']==5
        assert not state.connection.execute("SELECT has_table_privilege(current_user,'reporting.cohort_metrics','DELETE') AS allowed").fetchone()['allowed']
        with pytest.raises((errors.InsufficientPrivilege, errors.ObjectNotInPrerequisiteState)):
            state.connection.execute("DELETE FROM reporting.cohort_metrics")
    finally:
        state.connection.execute('RESET ROLE')
        state.connection.execute('DELETE FROM anomaly_events WHERE id=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_checkpoints WHERE collection=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_documents WHERE key=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_security_events WHERE source_key=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_security_rollups WHERE kind=%s',(key,))
        state.connection.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        state.connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        state.close()
