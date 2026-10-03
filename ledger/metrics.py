"""Aggregate pilot measurements without logging member bodies or source records."""
from collections import Counter


def snapshot(store):
    people = store.select('ledger_participants')
    evidence = store.select('ledger_evidence')
    jobs = store.select('ledger_outbox')
    compositions = [j['composed'] for j in jobs if j.get('composed')]
    inference_jobs = [j for j in store.select('ledger_inbox') if j['kind'] == 'engagement']
    decisions = [e for e in evidence if e.get('kind') == 'ai_decision']
    return {
        'participants': len(people), 'opted_in': sum(p['opted_in'] for p in people),
        'ranks': dict(Counter(str(p['rank']) for p in people)),
        'mentoring_sessions': sum(p.get('metrics', {}).get('mentoring', 0) for p in people),
        'verified_learning': sum(e.get('status') == 'approved' and e.get('achievement') in ('first_build', 'challenge') for e in evidence),
        'feedback_received': sum(e.get('kind') == 'feedback' for e in evidence),
        'projects': len(store.select('ledger_projects')),
        'completed_group_quests': len(store.select('ledger_quests', {'status': 'completed'})),
        'deliveries': dict(Counter(j['status'] for j in jobs)),
        'generation_attempts': len(compositions),
        'fallbacks': sum(c['outcome'] == 'fallback' for c in compositions),
        'shared_deliveries': sum(j['status'] == 'done' and j['payload'].get('audience') == 'shared' for j in jobs),
        'inference_latency_seconds': [c['latency'] for c in compositions if 'latency' in c],
        'queue_backlog': sum(j['status'] in ('pending', 'working') for j in jobs + store.select('ledger_inbox')),
        'engagement_jobs': dict(Counter(j['status'] for j in inference_jobs)),
        'engagement_decisions': dict(Counter(e['status'] for e in decisions)),
        'budget_rejections': sum(e.get('kind') == 'ai_rejection' and e.get('reason_code') == 'budget' for e in evidence),
        'deductions': sum(e['category'] == 'imitation_deduction' and e['status'] == 'committed' for e in decisions),
        'delegation_revocations': sum(e.get('kind') == 'delegation_audit' and e.get('action') == 'revoked' for e in evidence),
        'delegation_failures': sum(j.get('last_error') == 'Denied' for j in store.select('ledger_inbox')),
        'arrival_outcomes': dict(Counter(e['status'] for e in evidence if e.get('kind') == 'arrival')),
    }
