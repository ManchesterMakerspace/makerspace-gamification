import json

import pytest

from conftest import oid
from ledger import views
from ledger.admin_access import command_eligible, help_text, invite
from ledger.authority import Authority
from ledger.conversations import restricted_answer
from ledger.domain import Denied
from ledger.slack_app import SlackUI
from ledger.storage import enqueue
from ledger.worker import Worker
from test_slack import form
from test_worker import claim


def member(n):
    return str(oid(n))


@pytest.fixture
def admins(joined, monkeypatch):
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CREVIEW')
    l, _, _, _, _, slack = joined
    slack.conversations_info.return_value = {'channel': {'is_private': True, 'is_member': True}}
    l.join(member(10))
    return joined


@pytest.mark.parametrize('rank', [0, 1, 2, 6])
def test_admin_command_and_help_use_source_role_at_any_rank(admins, rank):
    l, s, _, comp, _, slack = admins
    p = l.participant(member(10)); p['rank'] = rank; s.put('ledger_participants', p)
    assert command_eligible(l, member(10))
    assert '/ledger-admin invite' in json.dumps(views.home(l, member(10)))
    ui = SlackUI(l, comp)
    ui.command({'user_id': 'U10', 'command': '/ledger-admin', 'text': 'invite', 'trigger_id': 'T'}, slack)
    assert slack.views_open.call_args.kwargs['view']['callback_id'] == 'admin_invitation'


@pytest.mark.parametrize('target', [1, 3, 10])
def test_ineligible_accounts_receive_no_command_or_help(joined, target):
    l, _, _, comp, _, slack = joined
    assert not command_eligible(l, member(target))
    assert help_text(l, member(target)) == ''
    assert '/ledger-admin' not in json.dumps(views.home(l, member(target)))
    assert restricted_answer(l, member(target), 'Try /ledger-admin invite.')
    for text in ('', 'help', 'review', 'invite'):
        with pytest.raises(Denied, match='unavailable'):
            SlackUI(l, comp).command({'user_id': f'U{target}', 'command': '/ledger-admin', 'text': text, 'trigger_id': 'T'}, slack)
    slack.views_open.assert_not_called()


def test_delegates_only_see_scoped_review_help(joined):
    l, _, _, comp, _, slack = joined
    Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Independent reviewer')
    help = help_text(l, member(2))
    assert 'approve' in help and 'ranks' not in help and 'invite' not in help and 'publish-quest' not in help and 'complete-quest' not in help
    SlackUI(l, comp).command({'user_id': 'U2', 'command': '/ledger-admin', 'text': 'help', 'trigger_id': 'T'}, slack)
    assert 'approve' in json.dumps(slack.views_open.call_args.kwargs['view'])
    SlackUI(l, comp).command({'user_id': 'U2', 'command': '/ledger-admin', 'text': '', 'trigger_id': 'T'}, slack)
    assert 'approve' in json.dumps(slack.views_open.call_args.kwargs['view'])
    l.leave(member(2))
    assert help_text(l, member(2)) == ''


def test_invitation_picker_filters_opt_in_revocation_and_invalid_identities(admins):
    l, s, src, comp, _, _ = admins
    src.data['members'][3]['status'] = 'revoked'
    src.data['members'][4]['merged_at'] = 'merged'
    src.data['slack_users'][5]['invalidated_at'] = 'invalid'
    s.put('ledger_catalog', {'_id': 'identity:' + member(7), 'deactivated': True})
    s.put('ledger_catalog', {'_id': 'identity:' + member(8), 'bot': True})
    options = SlackUI(l, comp).options({'user': {'id': 'U10'}, 'action_id': 'invite_recipient', 'value': ''})['options']
    assert {o['value'] for o in options} == {member(3), member(9), member(11)}
    for n in (1, 4, 5, 6, 7, 8, 10):
        with pytest.raises(Denied):
            invite(l, member(10), member(n), '', '', f'invalid-{n}')


@pytest.mark.parametrize('sender,expected', [('The Archivist', 'The Archivist'), ('', 'Maker10 Test')])
def test_modal_invitation_keeps_sender_private_and_does_not_enroll(admins, sender, expected):
    l, s, _, comp, api, slack = admins
    ui = SlackUI(l, comp)
    view = views.admin_invitation()
    body = form(view, {'invite_recipient': member(3), 'sender': sender, 'message': 'Come make something!'}, 'U10')
    ui.submission(body, slack)
    ui.submission(body, slack)
    jobs = s.select('ledger_outbox', {'kind': 'admin_invitation'})
    assert len(jobs) == 1
    Worker(l, comp, slack).outbox(claim(s, jobs[0]['_id']))
    sent = slack.chat_postMessage.call_args.kwargs
    assert sent['channel'] == 'DU3' and expected in sent['text'] and 'Come make something!' in sent['text']
    if sender:
        assert 'Maker10' not in json.dumps(sent) and 'U10' not in json.dumps(sent) and member(10) not in json.dumps(sent)
    assert sent['blocks'][-1]['elements'][0]['value'] == ''
    assert not l.participant(member(3))
    api.complete.assert_not_called()


@pytest.mark.parametrize('change', ['actor_leave', 'role_loss', 'recipient_join', 'recipient_revoked'])
def test_invitation_rechecks_delivery_authorization(admins, change):
    l, s, src, comp, _, slack = admins
    invite(l, member(10), member(3), 'The Archivist', '', 'stale')
    if change == 'actor_leave': l.leave(member(10))
    if change == 'role_loss': src.data['members'][9]['role'] = 'member'
    if change == 'recipient_join': l.join(member(3))
    if change == 'recipient_revoked': src.data['members'][2]['status'] = 'revoked'
    with pytest.raises(Denied): Worker(l, comp, slack).outbox(claim(s, 'admin-invitation:stale'))
    slack.chat_postMessage.assert_not_called()


def test_stale_invitation_modal_and_help_are_blocked_on_opt_out(admins):
    l, s, _, comp, _, slack = admins
    ui = SlackUI(l, comp)
    Worker(l, comp, slack).admin_command(member(10), ['help'], 'admin-help')
    l.leave(member(10))
    with pytest.raises(Denied): ui.submission(form(views.admin_invitation(), {'invite_recipient': member(3)}, 'U10'), slack)
    with pytest.raises(Denied): Worker(l, comp, slack).outbox(claim(s, 'dm:admin-help'))
    slack.chat_postMessage.assert_not_called()


def test_admin_review_invite_and_opt_out_removal_run_while_paused(admins):
    l, s, _, comp, api, slack = admins
    w = Worker(l, comp, slack)
    s.put('ledger_catalog', {'_id': 'control', 'paused': True})
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    slack.conversations_invite.assert_called_once_with(channel='CREVIEW', users='U10')
    l.leave(member(10))
    assert w.step('ledger_outbox', kinds=['remove'])
    assert any(c.kwargs == {'channel': 'CREVIEW', 'user': 'U10'} for c in slack.conversations_kick.call_args_list)
    api.complete.assert_not_called()


def test_only_source_admins_receive_automatic_review_invites(admins):
    l, s, src, comp, _, slack = admins
    l.join(member(11))
    src.data['members'][2]['role'] = 'resource_manager'
    l.join(member(3))
    jobs = s.select('ledger_outbox', {'kind': 'review_channel_invite'})
    assert [j['payload']['member_id'] for j in jobs] == [member(10)]
    assert Worker(l, comp, slack).step('ledger_outbox', kinds=['review_channel_invite'])
    src.data['members'][9]['role'] = 'member'
    l.reconcile(member(10))
    removal = next(j for j in s.select('ledger_outbox', {'kind': 'remove'}) if j['payload'].get('review_channel'))
    Worker(l, comp, slack).outbox(claim(s, removal['_id']))
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')


def test_delayed_review_invite_does_not_survive_leave_or_consent_generation(admins):
    l, s, _, comp, _, slack = admins
    job = s.select('ledger_outbox', {'kind': 'review_channel_invite'})[0]
    l.leave(member(10)); l.join(member(10))
    with pytest.raises(Denied): Worker(l, comp, slack).outbox(claim(s, job['_id']))
    assert Worker(l, comp, slack).step('ledger_outbox', kinds=['review_channel_invite'])
    removal = next(j for j in s.select('ledger_outbox', {'kind': 'remove'}) if j['payload'].get('review_channel'))
    Worker(l, comp, slack).outbox(claim(s, removal['_id']))
    slack.conversations_kick.assert_not_called()


def test_opt_out_during_review_invite_is_compensated(admins):
    l, _, _, comp, _, slack = admins
    slack.conversations_invite.side_effect = lambda **kwargs: l.leave(member(10))
    assert Worker(l, comp, slack).step('ledger_outbox', kinds=['review_channel_invite'])
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')


@pytest.mark.parametrize('info', [{'is_private': False, 'is_member': True}, {'is_private': True, 'is_member': False}, {'is_private': True, 'is_member': True, 'is_ext_shared': True}])
def test_review_invites_validate_private_channel(admins, info):
    l, s, _, comp, _, slack = admins
    slack.conversations_info.return_value = {'channel': info}
    job = s.select('ledger_outbox', {'kind': 'review_channel_invite'})[0]
    with pytest.raises(ValueError): Worker(l, comp, slack).outbox(claim(s, job['_id']))
    slack.conversations_invite.assert_not_called()


def test_admin_roles_from_chat_and_rank_do_not_grant_command(joined):
    l, s, _, comp, _, slack = joined
    p = l.participant(member(1)); p.update(rank=6, role='admin'); s.put('ledger_participants', p)
    assert not command_eligible(l, member(1))
    l.notify(member(1), 'status', {'summary': 'General member information'}, 'generated-help')
    comp.api.complete.return_value = 'Use /ledger-admin invite.'
    Worker(l, comp, slack).outbox(claim(s, 'dm:generated-help'))
    assert '/ledger-admin' not in json.dumps(slack.chat_postMessage.call_args.kwargs)


def test_review_membership_reconciles_configuration_and_retries_failed_invitation(admins, monkeypatch):
    l, s, _, comp, _, slack = admins
    w = Worker(l, comp, slack)
    job = s.select('ledger_outbox', {'kind': 'review_channel_invite'})[0]
    job['status'] = 'failed'; s.put('ledger_outbox', job)
    l.reconcile(member(10))
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CNEW')
    l.reconcile(member(10))
    assert w.step('ledger_outbox', kinds=['remove'])
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    assert slack.conversations_invite.call_args.kwargs['channel'] == 'CNEW'
    assert not s.select('ledger_channels', {'kind': 'channel', 'channel_id': 'CNEW'})


def test_empty_configuration_does_not_queue_review_invite(joined, monkeypatch):
    l, s, *_ = joined
    monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', raising=False)
    l.join(member(10))
    assert not s.select('ledger_outbox', {'kind': 'review_channel_invite'})


def test_review_cleanup_uses_saved_slack_identity_when_mapping_is_lost(admins):
    l, s, src, comp, _, slack = admins
    w = Worker(l, comp, slack)
    assert w.step('ledger_outbox', kinds=['review_channel_invite'])
    src.data['slack_users'][9]['invalidated_at'] = 'invalid'
    l.reconcile(member(10))
    assert w.step('ledger_outbox', kinds=['remove'])
    slack.conversations_kick.assert_called_once_with(channel='CREVIEW', user='U10')
