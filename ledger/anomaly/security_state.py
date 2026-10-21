"""Indexed private security evidence and incremental hourly rollups."""
from copy import deepcopy
from datetime import timedelta

from .privacy import timestamp

DDL = """
CREATE TABLE IF NOT EXISTS anomaly_security_events (
 id text PRIMARY KEY, source_key text NOT NULL, kind text NOT NULL,
 at timestamptz NOT NULL, member_id text, actor_id text, data jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS anomaly_security_source ON anomaly_security_events(source_key);
CREATE INDEX IF NOT EXISTS anomaly_security_time ON anomaly_security_events(at,id);
CREATE INDEX IF NOT EXISTS anomaly_security_kind_time ON anomaly_security_events(kind,at);
CREATE INDEX IF NOT EXISTS anomaly_security_member_time ON anomaly_security_events(member_id,at);
CREATE INDEX IF NOT EXISTS anomaly_security_actor_time ON anomaly_security_events(actor_id,at);
CREATE TABLE IF NOT EXISTS anomaly_security_rollups (
 bucket timestamptz NOT NULL, kind text NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(bucket,kind));
"""


class SecurityState:
    def security_events(self, since=None, until=None, *, source=None, kind=None, member=None, actor=None):
        if not hasattr(self, 'connection'):
            rows = [v for _, v in self.rows('security_events') if
                    (since is None or timestamp(v['at']) >= timestamp(since)) and
                    (until is None or timestamp(v['at']) < timestamp(until)) and
                    (source is None or v['source_key'] == source) and (kind is None or v['kind'] == kind) and
                    (member is None or v.get('member_id') == member) and (actor is None or v.get('actor_id') == actor)]
            return sorted(rows, key=lambda r: (r['at'], r['id']))
        clauses, params = [], []
        for clause, value in (('at >= %s', since), ('at < %s', until), ('source_key = %s', source),
                              ('kind = %s', kind), ('member_id = %s', member), ('actor_id = %s', actor)):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        query = 'SELECT data FROM anomaly_security_events' + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
        with self.lock:
            return [r['data'] for r in self.connection.execute(query + ' ORDER BY at,id', params).fetchall()]

    def security_get_event(self, event_id):
        if not hasattr(self, 'connection'):
            return self.get('security_events', event_id)
        with self.lock:
            row = self.connection.execute('SELECT data FROM anomaly_security_events WHERE id=%s', (event_id,)).fetchone()
            return row['data'] if row else None

    def security_replace(self, source, events):
        """Replace one source's contributions and dirty affected hours in one transaction."""
        with self.atomic():
            previous = self.security_events(source=source)
            if sorted(previous, key=lambda r: r['id']) == sorted(events, key=lambda r: r['id']):
                return False
            if hasattr(self, 'connection'):
                from psycopg.types.json import Jsonb
                self.connection.execute('DELETE FROM anomaly_security_events WHERE source_key=%s', (source,))
                for row in events:
                    self.connection.execute('INSERT INTO anomaly_security_events(id,source_key,kind,at,member_id,actor_id,data) '
                        'VALUES(%s,%s,%s,%s,%s,%s,%s)', (row['id'], source, row['kind'], row['at'], row.get('member_id'), row.get('actor_id'), Jsonb(row)))
            else:
                for row in previous:
                    self.delete('security_events', row['id'])
                for row in events:
                    self.put('security_events', row['id'], row)
            revision = self.get('security_meta', 'revision', {'value': 0})['value'] + 1
            self.put('security_meta', 'revision', {'value': revision})
            clock = timestamp(self.get('security_control', 'learned_clock', {}).get('at'))
            if previous or clock and any(timestamp(r['at']) < clock-timedelta(hours=1) for r in events):
                self.put('security_control', 'learning_correction', {'revision': revision})
            for bucket in {timestamp(r['at']).replace(minute=0, second=0, microsecond=0).isoformat() for r in previous + events}:
                self.put('security_dirty', bucket, {'revision': revision})
            return True

    def security_put_rollup(self, bucket, kind, data):
        if not hasattr(self, 'connection'):
            self.put('security_rollups', bucket + ':' + kind, data)
            return
        from psycopg.types.json import Jsonb
        with self.lock:
            self.connection.execute('INSERT INTO anomaly_security_rollups(bucket,kind,data) VALUES(%s,%s,%s) '
                'ON CONFLICT(bucket,kind) DO UPDATE SET data=EXCLUDED.data', (bucket, kind, Jsonb(data)))

    def security_rollups(self, since=None, until=None):
        if not hasattr(self, 'connection'):
            return [deepcopy(v) for _, v in self.rows('security_rollups') if
                    (since is None or timestamp(v['at']) >= timestamp(since)) and
                    (until is None or timestamp(v['at']) < timestamp(until))]
        clauses, params = [], []
        for clause, value in (('bucket >= %s', since), ('bucket < %s', until)):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        with self.lock:
            return [r['data'] for r in self.connection.execute('SELECT data FROM anomaly_security_rollups' +
                (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY bucket,kind', params).fetchall()]

    def security_prune(self, months):
        if not hasattr(self, 'connection'):
            return
        # Open incidents and notifications retain their supporting evidence.
        self.connection.execute("DELETE FROM anomaly_security_events e WHERE at < now() - %s * interval '1 month' "
            "AND NOT EXISTS (SELECT 1 FROM anomaly_documents d WHERE namespace='security_incidents' "
            "AND (d.data->>'status' IN ('open','shadow') OR EXISTS (SELECT 1 FROM anomaly_documents j "
            "WHERE j.namespace='security_deliveries' AND j.data->>'incident_id'=d.key AND j.data->>'status'='pending')) "
            "AND d.data->'evidence' ? e.id)", (months,))
        self.connection.execute("DELETE FROM anomaly_security_rollups WHERE bucket < now() - %s * interval '1 month'", (months,))
        self.connection.execute("DELETE FROM anomaly_documents d WHERE namespace='security_incidents' "
            "AND d.data->>'status' IN ('resolved','retracted') AND (d.data->>'created_at')::timestamptz < now() - %s * interval '1 month' "
            "AND NOT EXISTS (SELECT 1 FROM anomaly_documents j WHERE j.namespace='security_deliveries' "
            "AND j.data->>'incident_id'=d.key AND j.data->>'status'='pending')", (months,))
