from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bson import json_util
from pymongo.errors import ServerSelectionTimeoutError
import pytest

from conftest import oid
from ledger.arrivals import Arrivals, checkin_time
from ledger.conversations import converse, self_progress_question
from ledger.domain import Denied
from ledger.engagement import Engagement
from ledger.query_tools import QueryTools, QUERY_TOOL
from ledger.storage import now
from ledger.worker import Worker, ingest_mqtt
from test_worker import claim
from test_progress_quests_delegation import member, set_participant


def observation(env, monkeypatch, n=1, source='event', text='Shared useful project feedback', category='recognition', xp=3):
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    monkeypatch.setenv('LEDGER_OBSERVATION_AUDIT_ONLY', 'false')
    l, s, *_ = env
    p = set_participant(l, s, n)
    p['observation_notice_delivered_at'] = now() - timedelta(minutes=1)
    s.put('ledger_participants', p)
    source = 'message:CCHAT:' + source
    s.put('ledger_context', {'_id': source, 'kind': 'message', 'member_id': member(n), 'text': text, 'at': str(now().timestamp())})
    doc = Engagement(l).capture(member(n), source, 'message', text, 'CCHAT', now())
    return {'member_id': member(n), 'evidence': [doc['_id']], 'category': category, 'xp': xp, 'reason': 'Observed constructive contribution.', 'confidence': 0.99}


def arrival(env, monkeypatch, identifier=900, n=1):
    monkeypatch.setenv('LEDGER_WELCOMES', 'true')
    l, s, src, *_ = env
    p = set_participant(l, s, n, rank=1)
    src.data['cards'].append({'_id': oid(identifier + 1000), 'uid': 'PRIVATE_CARD_' + str(identifier), 'member_id': oid(n), 'validity': 'activeMember'})
    src.data['checkins'].append({'_id': oid(identifier), 'uid': 'PRIVATE_CARD_' + str(identifier), 'timeOf': int(now().timestamp() * 1000)})
    s.put('ledger_channels', {'_id': f'membership:{member(n)}:rank:1', 'kind': 'membership', 'present': True, 'voluntary_leave': False})
    return Arrivals(l, draw=lambda: 0), str(oid(identifier))


@pytest.mark.parametrize('args', [
    {}, {'collection': 'members'}, {'collection': 'shops', '$where': 'evil'}, {'collection': 'shops', 'member_id': 'another'},
    {'collection': 'shops', 'limit': True}, {'collection': 'shops', 'limit': 26}, {'collection': 'shops', 'limit': 0},
    {'collection': 'shops', 'search': 'x' * 101}, {'collection': 'shops', 'search': {'$ne': ''}},
    {'collection': 'tools', 'shop_id': {'$ne': None}}, {'collection': 'tools', 'shop_id': 'bad-id'},
    {'collection': 'tools', 'out_of_service': 'false'},
])
def test_query_rejects_arbitrary_arguments(joined, args):
    with pytest.raises(ValueError):
        QueryTools(joined[0], member(1)).call('query_makerspace', args)


def test_query_excludes_disabled_parents_and_retains_downtime_tools(joined):
    l, _, src, *_ = joined
    src.data['shops'][0]['disabled'] = True
    src.data['tools'][4]['out_of_service'] = True
    src.data['tools'][5]['disabled'] = True
    result = QueryTools(l, member(1)).call('query_makerspace', {'collection': 'tools', 'out_of_service': True})
    assert len(result['results']) == 1 and result['results'][0]['_id'] == member(321)
    result = QueryTools(l, member(1)).query({'collection': 'tools', 'limit': 25})
    assert all(r['shop_id'] != member(201) and r['_id'] != member(322) for r in result['results'])
    assert result['retrieved_at'] and result['status'] == 'ok'


def test_queries_project_only_caller_clearances_and_escape_search(joined):
    l, _, src, *_ = joined
    src.data['tool_checkouts'] = [
        {'_id': oid(901), 'member_id': oid(1), 'tool_id': oid(311), 'approved_by_id': oid(10), 'internal_notes': 'SECRET'},
        {'_id': oid(902), 'member_id': oid(2), 'tool_id': oid(312)},
        {'_id': oid(903), 'member_id': oid(1), 'tool_id': oid(313), 'revoked_at': now()},
    ]
    results = QueryTools(l, member(1)).query({'collection': 'tool_checkouts'})['results']
    assert results == [{'_id': member(901), 'tool_id': member(311)}]
    assert not QueryTools(l, member(1)).query({'collection': 'shops', 'search': '.*'})['results']
    assert QueryTools(l, member(1)).query({'collection': 'tools', 'limit': 1})['truncated']


def test_volunteer_dates_use_new_york_calendar_and_rails_claimable_statuses(joined):
    l, _, src, *_ = joined
    clock = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)  # October 3 in New York.
    today = datetime(2026, 10, 3, tzinfo=timezone.utc)
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    src.data['volunteer_events'] = [
        {'_id': oid(901), 'title': 'Today', 'status': 'open', 'event_date': today, 'attendee_ids': [oid(1)], 'internal_notes': 'SECRET'},
        {'_id': oid(902), 'title': 'Past', 'status': 'open', 'event_date': yesterday},
        {'_id': oid(903), 'title': 'Undated', 'status': 'open'},
        {'_id': oid(904), 'title': 'Closed', 'status': 'closed', 'event_date': tomorrow},
    ]
    src.data['volunteer_tasks'] = [
        {'_id': oid(910 + i), 'title': status, 'status': status, 'claimed_by_id': oid(2), 'verified_by_id': oid(10)}
        for i, status in enumerate(('available', 'reusable', 'repeatable', 'recurring', 'claimed', 'pending', 'closed'))]
    src.data['volunteer_tasks'].append({'_id': oid(920), 'title': 'Cooling', 'status': 'recurring', 'next_available': tomorrow})
    with patch('ledger.query_tools.now', return_value=clock):
        events = QueryTools(l, member(1)).query({'collection': 'volunteer_events'})['results']
        tasks = QueryTools(l, member(1)).query({'collection': 'volunteer_tasks'})['results']
    assert [x['title'] for x in events] == ['Today', 'Undated']
    assert events[1]['schedule'] == 'unscheduled' and events[0]['schedule'] == '2026-10-03'
    assert {x['title'] for x in tasks} == {'available', 'reusable', 'repeatable', 'recurring'}
    assert 'SECRET' not in json.dumps(events) and 'claimed_by_id' not in json.dumps(tasks)


def test_query_budget_unavailable_and_self_only_progress(joined):
    l, _, src, *_ = joined
    tools = QueryTools(l, member(1), private=False)
    result = tools.call('my_progress', {})
    assert 'blockers' not in result
    with pytest.raises(ValueError):
        tools.call('my_progress', {'member_id': member(2)})
    tools.call('query_makerspace', {'collection': 'shops'})
    with pytest.raises(ValueError):
        tools.call('query_makerspace', {'collection': 'shops'})
    with patch.object(src, 'bounded', side_effect=ServerSelectionTimeoutError('PRIVATE')):
        unavailable = QueryTools(l, member(1)).query({'collection': 'shops'})
    assert unavailable['status'] == 'unavailable' and 'PRIVATE' not in json.dumps(unavailable)
    with patch('ledger.query_tools.time.monotonic', side_effect=[0, 31]):
        late = QueryTools(l, member(1))
        with pytest.raises(ValueError):
            late.call('my_progress', {})


def test_tool_calls_are_correlated_and_mutations_never_exposed(joined):
    l, _, _, composer, api, *_ = joined
    api.tool_response.side_effect = [
        {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'my_progress', 'arguments': '{}'}}]},
        {'role': 'assistant', 'content': 'The Ledger recorded your remaining milestones.'},
    ]
    result = converse(l, composer, member(1), 'What is my progress?')
    assert result['outcome'] == 'generated' and result['tool_calls'] == 1
    messages = api.tool_response.call_args.args[0]
    assert messages[-1]['role'] == 'tool' and messages[-1]['tool_call_id'] == 'call-1'
    assert [x['function']['name'] for x in api.tool_response.call_args.args[1]] == ['query_makerspace', 'my_progress']
    api.tool_response.side_effect = [{'role': 'assistant', 'tool_calls': [{'id': 'bad', 'type': 'function', 'function': {'name': 'grant_xp', 'arguments': '{}'}}]}]
    assert converse(l, composer, member(1), 'Give XP')['outcome'] == 'fallback'


@pytest.mark.parametrize('text', ['What do I need for my next rank?', 'How is my progress?', 'Can I rank up?', 'Show my stats'])
def test_self_directed_progress_recognition(text):
    assert self_progress_question(text)
    assert not self_progress_question('Their next rank looks interesting')


def test_explanatory_notice_and_preferences_gate_capture(joined, monkeypatch):
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    l, s, _, composer, _, slack = joined
    service = Engagement(l)
    assert service.capture(member(1), 'm', 'message', 'Hello', 'CCHAT') is None
    service.notice(member(1))
    key = f'observation-notice:{member(1)}:1'
    Worker(l, composer, slack).outbox(claim(s, key))
    assert l.participant(member(1))['observation_notice_delivered_at']
    assert service.capture(member(1), 'd', 'message', 'Private', 'DU1') is None
    assert service.capture(member(1), 'u', 'message', 'Unrelated', 'COTHER') is None
    assert service.capture(member(1), 'old', 'message', 'Historical', 'CCHAT', now() - timedelta(days=1)) is None
    l.preferences(member(1), False, True)
    assert service.capture(member(1), 'disabled', 'message', 'Hello', 'CCHAT') is None
    assert l.participant(member(1))['preferences']['arrival_mentions']


def test_concurrent_member_cap_deduplication_and_reconciliation_preserve_awards(joined, monkeypatch):
    l, s, *_ = joined
    proposals = [observation(joined, monkeypatch, source=str(i), xp=4) for i in range(6)]
    service = Engagement(l)
    def commit(i):
        try:
            return service.commit(proposals[i], 'concurrent-' + str(i))['status']
        except Denied:
            return 'denied'
    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(commit, range(6)))
    assert outcomes.count('committed') == 3 and outcomes.count('denied') == 3
    assert l.participant(member(1))['xp'] == '12'
    done = next(i for i, value in enumerate(outcomes) if value == 'committed')
    service.commit(proposals[done], 'concurrent-' + str(done))
    assert l.participant(member(1))['xp'] == '12'
    l.reconcile(member(1))
    assert l.participant(member(1))['xp'] == '12'
    day = now().astimezone(ZoneInfo('America/New_York')).date().isoformat()
    assert s.get('ledger_evidence', 'ai-budget:' + day)['positive'] == 12


def test_workspace_positive_cap_deductions_and_corrections_never_replenish(joined, monkeypatch):
    l, s, *_ = joined
    service = Engagement(l)
    for n in range(1, 9):
        proposal = observation(joined, monkeypatch, n=n, source='workspace-' + str(n), xp=13 if n < 8 else 9)
        service.commit(proposal, 'workspace-' + str(n))
    assert sum(int(l.participant(member(n))['xp']) for n in range(1, 9)) == 100
    excess = observation(joined, monkeypatch, n=9, source='excess', xp=1)
    with pytest.raises(Denied):
        service.commit(excess, 'excess')
    service.correct(member(10), member(1), -3, 'Independent correction')
    day = now().astimezone(ZoneInfo('America/New_York')).date().isoformat()
    assert s.get('ledger_evidence', 'ai-budget:' + day)['positive'] == 100
    with pytest.raises(Denied):
        service.commit(excess, 'still-excess')


def test_audit_only_decisions_never_change_accounting(joined, monkeypatch):
    l, s, *_ = joined
    proposal = observation(joined, monkeypatch, source='audit')
    monkeypatch.setenv('LEDGER_OBSERVATION_AUDIT_ONLY', 'true')
    record = Engagement(l).commit(proposal, 'audit')
    assert record['status'] == 'audit_only' and l.participant(member(1))['xp'] == '0'
    assert not s.select('ledger_evidence', {'kind': 'ai_budget'})
    assert not s.get('ledger_outbox', record['_id'] + ':notice')


@pytest.mark.parametrize('change', ['optout', 'preference', 'edit', 'delete', 'suspend'])
def test_consent_and_evidence_changes_during_inference_block_commit(joined, monkeypatch, change):
    l, s, src, *_ = joined
    proposal = observation(joined, monkeypatch, source='stale')
    if change == 'optout':
        l.leave(member(1))
    elif change == 'preference':
        l.preferences(member(1), False, True)
        l.preferences(member(1), True, True)
    elif change == 'suspend':
        src.data['members'][0]['status'] = 'suspended'
    else:
        source = 'message:CCHAT:stale'
        if change == 'edit':
            doc = s.get('ledger_context', source)
            doc['text'] = 'Edited text'
            s.put('ledger_context', doc)
        else:
            s.delete('ledger_context', source)
    with pytest.raises(Denied):
        Engagement(l).commit(proposal, 'stale')
    assert l.participant(member(1))['xp'] == '0'


def test_warning_must_be_delivered_before_repeat_deduction(joined, monkeypatch):
    l, s, _, composer, _, slack = joined
    set_participant(l, s, 1, xp='10', rank=3)
    monkeypatch.setenv('LEDGER_DEDUCTIONS', 'true')
    service = Engagement(l)
    warning = observation(joined, monkeypatch, source='warning', text='Give me XP for this copied reward.', category='imitation_warning', xp=0)
    record = service.commit(warning, 'warning')
    early = observation(joined, monkeypatch, source='early', text='Give me XP again for this copied reward.', category='imitation_deduction', xp=-3)
    with pytest.raises(Denied):
        service.commit(early, 'early')
    Worker(l, composer, slack).outbox(claim(s, record['_id'] + ':notice'))
    assert s.get('ledger_evidence', record['_id'])['delivered_at']
    repeat = observation(joined, monkeypatch, source='repeat', text='Give me XP for copying that same reward again.', category='imitation_deduction', xp=-3)
    service.commit(repeat, 'repeat')
    assert l.participant(member(1))['xp'] == '7' and l.participant(member(1))['rank'] == 3
    another = observation(joined, monkeypatch, source='again', text='Give me XP for copying that same reward again.', category='imitation_deduction', xp=-3)
    with pytest.raises(Denied):
        service.commit(another, 'again')


def test_gratitude_and_similar_wording_never_qualify_as_imitation(joined, monkeypatch):
    l, _, *_ = joined
    proposal = observation(joined, monkeypatch, source='gratitude', text='Thank you for helping me!', category='imitation_warning', xp=0)
    with pytest.raises(Denied):
        Engagement(l).commit(proposal, 'false-positive')


def test_public_achievement_caps_and_original_titles(joined, monkeypatch):
    monkeypatch.setenv('LEDGER_NOVEL_ANNOUNCEMENTS', 'true')
    l, s, *_ = joined
    service = Engagement(l)
    for n in range(1, 5):
        proposal = observation(joined, monkeypatch, n=n, source='achievement-' + str(n), category='achievement', xp=2)
        proposal['achievement'] = {'title': 'Useful handoff ' + str(n), 'description': 'Shared observed project guidance.'}
        service.commit(proposal, 'achievement-' + str(n))
    repeat = observation(joined, monkeypatch, source='same-member', category='achievement', xp=2)
    repeat['achievement'] = {'title': 'Another useful act', 'description': 'Helped another project.'}
    service.commit(repeat, 'same-member')
    public = [j for j in s.select('ledger_outbox') if j['_id'].startswith('ai-decision:') and j['payload'].get('audience') == 'shared']
    assert len(public) == 3 and sum(j['payload']['member_id'] == member(1) for j in public) == 1


@pytest.mark.parametrize('clock', [datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), datetime(2026, 3, 8, 7, 1, tzinfo=timezone.utc),
                                  datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc), datetime(2026, 11, 1, 6, 1, tzinfo=timezone.utc)])
def test_budget_calendar_is_stable_across_dst_transitions(joined, monkeypatch, clock):
    l, s, *_ = joined
    proposal = observation(joined, monkeypatch, source='dst')
    with patch('ledger.engagement.now', return_value=clock):
        record = Engagement(l).commit(proposal, 'dst')
    assert record['day'] == clock.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    assert s.get('ledger_evidence', 'ai-budget:' + record['day'])['positive'] == 3


def test_arrival_random_reserved_once_and_cooldown_across_rank_changes(joined, monkeypatch):
    l, s, *_ = joined
    service, key = arrival(joined, monkeypatch)
    first = service.reserve(key)
    assert first['selected'] and first['status'] == 'reserved'
    service.draw = lambda: (_ for _ in ()).throw(AssertionError('Must not reroll'))
    assert service.reserve(key) == first
    service, second = arrival(joined, monkeypatch, identifier=901)
    assert service.reserve(second)['status'] == 'skipped'
    set_participant(l, s, 1, rank=2)
    assert not service.live(first)
    assert s.get('ledger_evidence', 'welcome-cooldown:' + member(1))['arrival'] == first['_id']


def test_arrival_identity_ambiguity_retained_and_stale_packets(joined, monkeypatch):
    l, s, src, *_ = joined
    service, key = arrival(joined, monkeypatch)
    payload = ('insert 123 ' + json_util.dumps({'document': {'_id': oid(900), 'uid': 'DO_NOT_PERSIST_CARD'}})).encode()
    assert not ingest_mqtt(s, 'checkins/insert', payload, retained=True)
    assert not ingest_mqtt(s, 'checkins/update', payload)
    assert ingest_mqtt(s, 'checkins/insert', payload)
    assert 'DO_NOT_PERSIST_CARD' not in json.dumps(s.select('ledger_inbox'), default=str)
    src.data['cards'].append({'_id': oid(9999), 'uid': src.data['cards'][0]['uid'], 'member_id': oid(2)})
    assert service.reserve(key) is None
    src.data['cards'].pop()
    src.data['checkins'][0]['timeOf'] = int((now() - timedelta(minutes=11)).timestamp() * 1000)
    assert service.reserve(key) is None
    assert checkin_time({'time': 'bad'}) is None


def test_arrival_mentions_and_unknown_outcome_keep_cooldown(joined, monkeypatch):
    l, s, _, composer, api, slack = joined
    service, key = arrival(joined, monkeypatch)
    reserved = service.reserve(key)
    api.complete.return_value = 'Welcome <@U2> <!here> <@U3>.'
    w = Worker(l, composer, slack)
    job = claim(s, reserved['_id'])
    slack.chat_postMessage.side_effect = TimeoutError('unknown delivery')
    with pytest.raises(TimeoutError):
        w.outbox(job)
    sent = slack.chat_postMessage.call_args.kwargs
    assert sent['text'].count('<@') == 1 and '<@U1>' in sent['text'] and '<!here>' not in sent['text']
    assert sent['channel'] == 'CRANK1'
    assert 'PRIVATE_CARD' not in json.dumps(api.complete.call_args.args, default=str)
    assert s.get('ledger_evidence', reserved['_id'])['status'] == 'attempted'
    w.outbox(job)
    assert slack.chat_postMessage.call_count == 1
    assert s.get('ledger_evidence', 'welcome-cooldown:' + member(1))


def test_arrival_live_preferences_and_favorite_shop_distinct_clearances(joined, monkeypatch):
    l, s, src, *_ = joined
    service, key = arrival(joined, monkeypatch)
    src.data['tool_checkouts'] = [
        {'_id': oid(950 + i), 'member_id': oid(1), 'tool_id': tool} for i, tool in enumerate((oid(311), oid(311), oid(321), oid(322)))]
    assert service.favorite_shop(member(1)) == 'Shop2'
    reserved = service.reserve(key)
    l.preferences(member(1), True, False)
    assert not service.live(reserved)
    assert l.participant(member(1))['preferences']['observation']


def test_tool_conversation_reserves_prompt_policy_and_reuses_text_on_retry(joined):
    l, s, _, composer, api, slack = joined
    w = Worker(l, composer, slack, bot_id='UBOT')
    text = 'What is my progress?'
    api.tool_response.return_value = {'role': 'assistant', 'content': 'The Ledger has your calculated progress.'}
    w.event({'type': 'message', 'user': 'U1', 'channel': 'DU1', 'channel_type': 'im', 'ts': '300.123', 'text': text}, 'conversation-ev')
    key = 'reply:DU1:300.123'
    job = claim(s, key)
    w.outbox(job)
    saved = s.get('ledger_outbox', key)
    assert saved['prompt_selection']['matrix']['version'] == '5'
    assert saved['composed']['prompt_variation'] in ('archivist', 'mentor', 'wry_grimoire')
    assert saved['composed']['prompt_scope'] == 'member:' + member(1)
    api.tool_response.reset_mock()
    w.outbox(job)
    api.tool_response.assert_not_called()
    assert slack.chat_postMessage.call_args.kwargs['blocks'][-1]['elements'][0]['text']['text'] == 'Private progress detail'


def test_staff_correction_route_is_linked_append_only_and_inaccessible_to_delegates(joined, monkeypatch):
    l, s, _, composer, _, slack = joined
    from ledger.authority import Authority
    proposal = observation(joined, monkeypatch, source='correction-route', xp=3)
    decision = Engagement(l).commit(proposal, 'correction-route')
    Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Review only')
    w = Worker(l, composer, slack)
    with pytest.raises(Denied):
        w.admin_command(member(2), ['correct-ai', decision['_id'], '-3', 'No correction authority'], 'forged-correction')
    w.admin_command(member(10), ['correct-ai', decision['_id'], '-3', 'Independent staff review'], 'staff-correction')
    assert l.participant(member(1))['xp'] == '0'
    assert s.get('ledger_evidence', decision['_id']) == decision
    corrections = s.select('ledger_evidence', {'kind': 'ai_correction'})
    assert len(corrections) == 1 and corrections[0]['decision'] == decision['_id']
    assert s.get('ledger_evidence', 'ai-budget:' + decision['day'])['positive'] == 3
