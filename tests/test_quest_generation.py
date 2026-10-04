from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import random
from threading import Event
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from conftest import oid
from ledger.domain import Denied
from ledger.generate_quest import main, parser
from ledger.messages import ChatAPI
from ledger.prompt_matrix import PromptMatrix, REQUIRED_ROLES, bundled_matrix, validate_matrix
from ledger.quest_generation import QuestGenerator, SlackHistory, rank_weight
from ledger.quest_policy import LEDGER_AUTHOR, QUEST_SCHEMA, sanitize, validate_definition
from ledger.storage import now
from test_messages import server


def proposal(quest_type="individual"):
    return {"title": "Make a useful measuring jig", "description": "Build a safe measuring jig and improve it with feedback.",
        "criteria": "Demonstrate repeatable measurements and document the improvement.", "shop_ids": [], "tool_ids": [],
        "disciplines": [] if quest_type == "individual" else [
            {"name": "Design", "expectation": "Document the design and feedback."},
            {"name": "Fabrication", "expectation": "Build and demonstrate the working jig."}]}


@pytest.fixture
def generator(joined):
    l, s, src, composer, api, slack = joined
    for participant in s.select("ledger_participants"):
        participant["import_pending"] = False
        s.put("ledger_participants", participant)
    api.model = "test-model"
    api.tokenize.return_value = 2000
    api.quest_response.return_value = json.dumps(proposal())
    slack.conversations_info.return_value = {"channel": {"is_private": True, "is_member": True, "is_ext_shared": False}}
    slack.conversations_history.return_value = {"messages": [], "response_metadata": {}}
    slack.conversations_replies.return_value = {"messages": []}
    return QuestGenerator(l, api, PromptMatrix(), slack, "CSTAFF"), joined


def test_cli_defaults_and_help_require_no_services():
    assert vars(parser().parse_args([])) == {"quest_type": "individual", "rank": None, "dry_run": False, "seed": None, "request_id": None}
    with patch("ledger.generate_quest.dependencies") as dependencies, pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    dependencies.assert_not_called()


def test_cli_failures_do_not_print_credentials_or_provider_bodies(capsys):
    with patch("ledger.generate_quest.dependencies", side_effect=ValueError("mongodb://SECRET:password/private")):
        assert main(["--request-id", "safe-id"]) == 1
    captured = capsys.readouterr()
    assert "SECRET" not in captured.err and "password" not in captured.err
    assert "safe-id" in captured.out


def test_weight_favors_population_inactivity_and_lower_supply():
    baseline = rank_weight(10, 0.5, 0.5, 2, 1)
    assert rank_weight(20, 0.5, 0.5, 2, 1) > baseline
    assert rank_weight(10, 0.1, 0.2, 2, 1) > baseline
    assert rank_weight(10, 0.5, 0.5, 1, 0) > baseline
    assert rank_weight(0, 0, 0, 0, 0) == 0


def test_metrics_use_original_valid_activity_and_current_cohort(generator):
    g, (l, s, src, _, _, slack) = generator
    stamp = now()
    src.data["tool_checkouts"] = [
        {"_id": oid(700), "member_id": oid(1), "tool_id": oid(311), "checked_out_at": stamp - timedelta(days=1)},
        {"_id": oid(701), "member_id": oid(2), "tool_id": oid(311), "checked_out_at": stamp - timedelta(days=90)},
        {"_id": oid(702), "member_id": oid(2), "tool_id": oid(311), "checked_out_at": stamp, "revoked_at": stamp}]
    src.data["volunteer_credits"] = [
        {"_id": oid(710), "member_id": oid(2), "created_at": stamp, "status": "approved", "credit_value": 1},
        {"_id": oid(711), "member_id": oid(2), "status": "reversal", "reversal_of_id": oid(710)}]
    slack.conversations_history.side_effect = lambda channel, **kw: {"messages": [
        {"user": "U2", "ts": str(stamp.timestamp()), "text": "Trying a new project"}] if channel == "CCHAT" else []}
    metrics, _ = g.metrics(stamp)
    assert metrics[0]["N"] == 2 and metrics[0]["V"] == 0.5 and metrics[0]["C"] == 0.5
    s.put("ledger_awards", {"_id": "import", "member_id": str(oid(2)), "kind": "checkout", "delta": "100", "at": stamp})
    assert g.metrics(stamp)[0][0]["V"] == 0.5
    p = l.participant(str(oid(1)))
    p["rank"] = 2
    s.put("ledger_participants", p)
    assert {m["rank"]: m["V"] for m in g.metrics(stamp)[0]} == {1: 0, 2: 1}


def test_incomplete_activity_is_neutral(generator):
    g, (l, s, *_ ) = generator
    p = l.participant(str(oid(1)))
    p["import_pending"] = True
    s.put("ledger_participants", p)
    with patch.object(g.history, "channel", return_value=([], False)):
        metrics, _ = g.metrics(now())
    assert metrics[0]["V"] == metrics[0]["C"] == 0.5
    assert not metrics[0]["verified_complete"] and not metrics[0]["chat_complete"]


def test_active_threads_make_chat_activity_coverage_neutral(generator):
    g, (_, _, _, _, _, slack) = generator
    stamp = now()
    slack.conversations_history.return_value = {"messages": [{"user": "U1", "ts": str(stamp.timestamp()),
        "text": "Project feedback", "reply_count": 20, "latest_reply": str(stamp.timestamp())}]}
    metric = g.metrics(stamp)[0][0]
    assert metric["C"] == 0.5 and not metric["chat_complete"]
    assert "thread participation" in metric["chat_coverage_notes"][0]


def test_explicit_empty_rank_random_selection_and_disabled_rank(generator):
    g, _ = generator
    assert g.context(6, random.Random(1), now())["rank"] == 6
    assert g.context(None, random.Random(1), now())["rank"] == 1
    with pytest.raises(ValueError, match="enabled"):
        g.context(7, random.Random(1), now())
    with patch.object(g, "metrics", return_value=([], {})), pytest.raises(ValueError, match="specify --rank"):
        g.context(None, random.Random(1), now())


def test_history_paginates_honors_retry_and_excludes_bots(generator):
    g, (_, _, _, _, _, slack) = generator
    sleep = MagicMock()
    history = SlackHistory(slack, sleep)
    response = MagicMock(status_code=429, headers={"Retry-After": "2"})
    response.get.return_value = "ratelimited"
    slack.conversations_history.side_effect = [SlackApiError("rate", response),
        {"messages": [{"user": "U1", "ts": "100", "text": "first"}], "response_metadata": {"next_cursor": "next"}},
        {"messages": [{"user": "U2", "ts": "101", "text": "second"}]}]
    messages, complete = history.channel("CCHAT", now() - timedelta(days=30), now())
    assert complete and len(messages) == 2
    assert slack.conversations_history.call_args.kwargs["cursor"] == "next"
    sleep.assert_called_once_with(2)
    assert not history.human({"user": "U1", "bot_id": "B123"})
    assert not history.human({"user": "U1", "subtype": "file_share"})


def test_inspiration_includes_nonparticipants_and_filters_sensitive_material(generator):
    g, (_, _, _, _, _, slack) = generator
    stamp = now()
    slack.users_info.side_effect = lambda user: {"user": {"id": user, "real_name": "Alice Example", "is_bot": False}}
    messages = [{"user": "UUNLINKED", "ts": str(stamp.timestamp()),
                 "text": "Alice Example suggests <@U1> make a jig; alice@example.com", "attachments": [{"text": "SECRET"}], "reply_count": 1},
                {"user": "UUNLINKED", "ts": str(stamp.timestamp() - 1), "text": "Door code: 1234"},
                {"user": "UBOT", "bot_id": "B1", "ts": str(stamp.timestamp()), "text": "Original kudos body"}]
    rows, _ = g.history.inspiration("CCHAT", messages, stamp - timedelta(days=14), stamp)
    rendered = json.dumps(rows)
    assert len(rows) == 1 and "jig" in rendered
    assert all(value not in rendered for value in ("Alice", "alice@example.com", "SECRET", "1234", "Original kudos", "<@"))
    assert sanitize("api_key: secret") == ""


def test_transport_quest_limit_is_separate_and_schema_nonthinking():
    output = "x" * 3000
    payload = {"choices": [{"message": {"content": output}, "finish_reason": "stop"}]}
    with server(payload) as (api, calls):
        assert api.quest_response([{"role": "user", "content": "quest"}], QUEST_SCHEMA) == output
        with pytest.raises(ValueError):
            api.complete([{"role": "user", "content": "narration"}])
    assert calls[0][1]["max_tokens"] == 1536 and calls[0][1]["temperature"] == 0.5
    assert calls[0][1]["response_format"]["json_schema"]["schema"] == QUEST_SCHEMA
    with server({"count": 123}) as (api, calls):
        assert api.tokenize([{"role": "user", "content": "quest"}]) == 123
    assert calls[0][0] == "/tokenize" and calls[0][1]["add_generation_prompt"] is True
    assert calls[0][1]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("change", [
    {"reward": 500}, {"title": "<@U1> a jig"}, {"title": "<|im_start|>"}, {"title": ""},
    {"tool_ids": ["unknown"]}, {"shop_ids": ["unknown"]}, {"description": "x" * 2001},
    {"disciplines": [{"name": "Design", "expectation": "Evidence"}]}])
def test_invalid_proposals_reject_without_state_mutation(generator, change):
    g, (_, s, _, _, api, _) = generator
    bad = {**proposal(), **change}
    api.quest_response.return_value = json.dumps(bad)
    before = deepcopy(s.data)
    with pytest.raises(ValueError):
        g.run(rank=1, dry_run=True)
    assert s.data == before and api.quest_response.call_count == 2


def test_dry_run_and_saved_request_are_idempotent(generator):
    g, (_, s, _, _, api, slack) = generator
    before = deepcopy(s.data)
    dry = g.run(rank=1, seed=42, request_id="dry", dry_run=True)
    assert dry["status"] == "dry_run" and s.data == before
    slack.chat_postMessage.assert_not_called()
    first = g.run(rank=1, seed=42, request_id="one")
    calls = api.quest_response.call_count
    second = g.run(rank=1, seed=42, request_id="one")
    assert first["quest_id"] == second["quest_id"] and first["proposal"] == second["proposal"]
    assert api.quest_response.call_count == calls
    assert len(s.select("ledger_quests")) == len(s.select("ledger_outbox", {"kind": "review_notice"})) == 1
    assert s.get("ledger_quests", first["quest_id"])["creator"] == LEDGER_AUTHOR
    assert s.select("ledger_awards") == []
    with pytest.raises(ValueError, match="original"):
        g.run(rank=2, seed=42, request_id="one")


def test_tokenizer_failure_preserves_selection_and_snapshot_for_retry(generator):
    g, (_, s, _, _, api, slack) = generator
    api.tokenize.side_effect = TimeoutError("PRIVATE")
    with pytest.raises(TimeoutError):
        g.run(seed=7, request_id="retry")
    saved = s.get("ledger_context", "quest-input:retry")["value"]
    assert s.select("ledger_quests") == []
    api.tokenize.side_effect = None
    result = g.run(seed=7, request_id="retry")
    assert result["rank"] == saved["rank"]
    assert len(s.select("ledger_quests")) == 1


def test_expired_unfinished_input_requires_new_request(generator):
    g, (_, s, _, _, api, _) = generator
    api.quest_response.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        g.run(rank=1, request_id="expired")
    snapshot = s.get("ledger_context", "quest-input:expired")
    snapshot["expires_at"] = now() - timedelta(days=1)
    s.put("ledger_context", snapshot)
    with pytest.raises(ValueError, match="expired"):
        g.run(rank=1, request_id="expired")


def test_submission_failure_reuses_validated_text_without_inference(generator):
    g, (_, s, _, _, api, _) = generator
    original = s.atomic
    def fail_submit(callback):
        if callback.__name__ == "submit":
            raise RuntimeError("transaction unavailable")
        return original(callback)
    with patch.object(s, "atomic", side_effect=fail_submit), pytest.raises(RuntimeError):
        g.run(rank=1, request_id="composed-retry")
    audit = s.get("ledger_evidence", "quest-generation:composed-retry")
    assert audit["status"] == "composed" and audit["proposal"] == proposal()
    calls = api.quest_response.call_count
    api.quest_response.side_effect = RuntimeError("must not regenerate")
    result = g.run(rank=1, request_id="composed-retry")
    assert result["proposal"] == audit["proposal"] and api.quest_response.call_count == calls


def test_concurrent_same_request_has_one_owner_and_submission(generator):
    g, (_, s, _, _, api, _) = generator
    entered, release = Event(), Event()
    def response(*args):
        entered.set()
        assert release.wait(5)
        return json.dumps(proposal())
    api.quest_response.side_effect = response
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(g.run, rank=1, request_id="concurrent")
        assert entered.wait(5)
        with pytest.raises(ValueError, match="already running"):
            g.run(rank=1, request_id="concurrent")
        release.set()
        first.result()
    assert api.quest_response.call_count == 1
    assert len(s.select("ledger_quests")) == len(s.select("ledger_outbox", {"kind": "review_notice"})) == 1


def test_completed_examples_deduplicate_logical_quests_and_anonymize(generator):
    g, (_, s, *_ ) = generator
    for version in range(2):
        s.put("ledger_quests", {"_id": f"revision-{version}", "kind": "member_quest", "logical_id": "one-logical",
            "creator": str(oid(1)), "reviewer": str(oid(10)), "target_rank": 6, "status": "published", **proposal()})
        s.put("ledger_evidence", {"_id": f"completion-{version}", "kind": "quest_completion", "quest_revision": f"revision-{version}"})
    examples = g.examples(random.Random(42))
    assert len(examples) == 1 and examples[0]["target_rank"] == 6


def test_new_role_is_required_and_old_remote_override_retains_current():
    assert "ledger_quest_author" in REQUIRED_ROLES
    matrix = bundled_matrix()
    import re
    incomplete = re.sub(r'<role id="ledger_quest_author">.*?</role>', "", matrix["text"], flags=re.S)
    with pytest.raises(ValueError):
        validate_matrix(incomplete)
    policy = PromptMatrix("https://docs.google.com/document/d/abcdefgh/edit")
    with patch("ledger.prompt_matrix.fetch_google_doc", return_value=incomplete):
        policy.refresh()
    assert policy.snapshot() == matrix


def test_configured_review_hook_reserves_exactly_one_deterministic_notice(generator, monkeypatch):
    g, (_, s, *_ ) = generator
    monkeypatch.setenv("LEDGER_QUEST_REVIEW_CHANNEL_ID", "CSTAFF")
    result = g.run(rank=1, request_id="hooked")
    notices = s.select("ledger_outbox", {"kind": "review_notice"})
    assert [j["_id"] for j in notices] == [result["notice_id"]]
    assert s.get("ledger_quests", result["quest_id"])["review_notice_job_id"] == result["notice_id"]


def test_resource_context_uses_real_source_projection_allowlist(generator):
    from ledger.sources import FIELDS
    g, (_, _, src, *_ ) = generator
    original = src.bounded
    def bounded(collection, query, fields, limit):
        assert not set(fields) - set(FIELDS[collection].split())
        return original(collection, query, fields, limit)
    with patch.object(src, "bounded", side_effect=bounded):
        result = g.run(rank=1, dry_run=True)
    assert result["status"] == "dry_run"


def test_trimming_keeps_recent_context_from_both_source_channels(generator):
    g, (_, _, _, _, api, _) = generator
    snapshot = g.context(1, random.Random(1), now())
    stamp = now().timestamp()
    snapshot["data"]["chat"] = [{"ref": f"CCHAT:{stamp}", "channel": "chat", "text": "Rare channel context"}] + [
        {"ref": f"CRANK1:{stamp + i}", "channel": "rank:1", "text": "Many recent excerpts"} for i in range(1, 20)]
    api.tokenize.side_effect = lambda messages: 9000 if len(json.loads(messages[1]["content"])["chat"]) > 2 else 2000
    fitted = g.fit(snapshot, "individual")
    assert {c["channel"] for c in fitted["data"]["chat"]} == {"chat", "rank:1"}
    assert len(snapshot["data"]["chat"]) == 20


def test_required_history_failure_is_not_empty_history(generator):
    g, (_, s, _, _, api, slack) = generator
    slack.conversations_history.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        g.run(rank=1, request_id="source-failure")
    api.quest_response.assert_not_called()
    assert s.select("ledger_quests") == [] and not s.get("ledger_outbox", "quest-review-notice:source-failure")


def test_optional_threads_prioritize_latest_activity_and_report_failure(generator):
    g, (_, _, _, _, _, slack) = generator
    stamp = now()
    roots = [{"user": "U1", "ts": str((stamp - timedelta(days=i + 1)).timestamp()), "text": "Project discussion",
              "reply_count": 1, "latest_reply": str((stamp - timedelta(minutes=6 - i)).timestamp())} for i in range(6)]
    response = MagicMock(status_code=403, headers={})
    slack.conversations_replies.side_effect = SlackApiError("unavailable", response)
    _, notes = g.history.inspiration("CCHAT", roots, stamp - timedelta(days=14), stamp)
    assert [call.kwargs["ts"] for call in slack.conversations_replies.call_args_list] == [r["ts"] for r in reversed(roots)][0:5]
    assert notes == ["optional thread replies unavailable"]


def test_thread_only_names_are_redacted_from_every_excerpt_before_prompting(generator):
    g, (_, _, _, _, api, slack) = generator
    stamp = now()
    root = {"user": "U1", "ts": str((stamp - timedelta(minutes=5)).timestamp()),
        "text": "Cecilia Threadmaker recommended a measuring jig.", "reply_count": 2}
    slack.conversations_history.side_effect = lambda channel, **kwargs: {"messages": [root] if channel == "CCHAT" else []}
    profiles = {"U1": "Alice Rootmaker", "U2": "Benjamin Replymaker", "U3": "Cecilia Threadmaker"}
    slack.users_info.side_effect = lambda user: {"user": {"id": user, "real_name": profiles[user], "is_bot": False}}
    slack.conversations_replies.return_value = {"messages": [
        {"user": "U2", "ts": str((stamp - timedelta(minutes=2)).timestamp()), "text": "Cecilia Threadmaker shared useful feedback."},
        {"user": "U3", "ts": str((stamp - timedelta(minutes=1)).timestamp()), "text": "Benjamin Replymaker should try adjustable stops."}]}
    g.run(rank=1, dry_run=True)
    prompt = json.dumps(api.quest_response.call_args.args[0])
    assert 'measuring jig' in prompt and 'adjustable stops' in prompt
    for name in profiles.values():
        assert all(part not in prompt for part in name.split())


@pytest.mark.parametrize('field', ['title', 'description', 'criteria'])
@pytest.mark.parametrize('excerpt,copied', [
    ('Try an adjustable stop for repeatable cuts.', 'Try an adjustable stop for repeatable cuts.'),
    ('Some initial discussion of materials and measurements that the model should not repeat. '
     'Use the unusual copper jig with a purple stop and record seven careful trial cuts.',
     'Use the unusual copper jig with a purple stop and record seven careful trial cuts.')])
def test_copy_guard_rejects_short_and_later_chat_quotes(generator, field, excerpt, copied):
    g, (_, s, _, _, api, slack) = generator
    stamp = now()
    slack.conversations_history.side_effect = lambda channel, **kwargs: {"messages": [
        {"user": "U1", "ts": str(stamp.timestamp()), "text": excerpt}] if channel == "CCHAT" else []}
    api.quest_response.return_value = json.dumps({**proposal(), field: copied})
    with pytest.raises(ValueError):
        g.run(rank=1, request_id='quoted')
    assert not s.select('ledger_quests') and not s.select('ledger_outbox', {'kind': 'review_notice'})
    assert api.quest_response.call_count == 2


def test_profiles_discovered_in_second_channel_redact_first_channel(generator):
    g, (_, s, _, _, api, slack) = generator
    stamp = now()
    slack.users_info.side_effect = lambda user: {'user': {'id': user, 'is_bot': False,
        'profile': {'display_name': 'Zelda Threadonly' if user == 'U3' else 'Root Maker'}}}
    roots = {'CCHAT': {'user': 'U1', 'ts': str((stamp - timedelta(minutes=4)).timestamp()),
        'text': 'Zelda Threadonly suggested adjustable stops.'},
        'CRANK1': {'user': 'U1', 'ts': str((stamp - timedelta(minutes=3)).timestamp()),
        'text': 'Feedback on the measuring jig.', 'reply_count': 1}}
    slack.conversations_history.side_effect = lambda channel, **kwargs: {'messages': [roots[channel]] if channel in roots else []}
    slack.conversations_replies.return_value = {'messages': [{'user': 'U3',
        'ts': str((stamp - timedelta(minutes=1)).timestamp()), 'text': 'Try a safer clamp position.'}]}
    result = g.run(rank=1, request_id='cross-channel')
    rendered = json.dumps(api.quest_response.call_args.args[0])
    saved = json.dumps(s.get('ledger_context', 'quest-input:cross-channel'), default=str)
    assert 'Zelda' not in rendered + saved and 'Threadonly' not in rendered + saved
    assert 'adjustable stops' in rendered and result['status'] == 'submitted'


@pytest.mark.parametrize('field', ['name', 'expectation'])
def test_cooperative_discipline_text_cannot_copy_chat(generator, field):
    g, (_, _, _, _, api, _) = generator
    snapshot = g.context(1, random.Random(1), now())
    snapshot['data']['chat'] = [{'text': 'Share careful jig measurements.'}]
    bad = proposal('cooperative')
    bad['disciplines'][0][field] = 'SHARE   careful\njig measurements.'
    api.quest_response.return_value = json.dumps(bad)
    with pytest.raises(ValueError): g.compose(snapshot, 'cooperative')


def test_short_excerpt_matches_whole_words_and_allows_paraphrase(generator):
    g, (_, _, _, _, api, _) = generator
    snapshot = g.context(1, random.Random(1), now())
    snapshot['data']['chat'] = [{'text': 'jig'}]
    good = {**proposal(), 'title': 'Use a jigsaw', 'description': 'Build a measuring fixture and refine its design.',
        'criteria': 'Demonstrate consistent safe cuts.'}
    api.quest_response.return_value = json.dumps(good)
    assert g.compose(snapshot, 'individual') == good


@pytest.mark.parametrize('composed', [False, True])
def test_legacy_unfinished_inputs_and_composed_proposals_require_new_request(generator, composed):
    g, (_, s, _, _, api, _) = generator
    with patch.object(g, 'fit', side_effect=TimeoutError):
        with pytest.raises(TimeoutError): g.run(rank=1, request_id='legacy')
    audit = s.get('ledger_evidence', 'quest-generation:legacy')
    audit['prompt_version'] = 1
    if composed: audit.update(status='composed', proposal=proposal(), retained_inputs={})
    s.put('ledger_evidence', audit)
    saved = deepcopy(s.get('ledger_context', 'quest-input:legacy'))
    with pytest.raises(ValueError, match='new request ID'): g.run(rank=1, request_id='legacy')
    assert s.get('ledger_context', 'quest-input:legacy') == saved
    assert not s.select('ledger_quests')
    api.quest_response.assert_not_called()


def test_legacy_submitted_request_stays_idempotently_readable(generator):
    g, (_, s, _, _, api, _) = generator
    first = g.run(rank=1, request_id='legacy-submitted')
    audit = s.get('ledger_evidence', 'quest-generation:legacy-submitted')
    audit['prompt_version'] = 1; s.put('ledger_evidence', audit)
    api.quest_response.reset_mock()
    assert g.run(rank=1, request_id='legacy-submitted')['quest_id'] == first['quest_id']
    api.quest_response.assert_not_called()


def test_reply_profile_failure_omits_all_chat_instead_of_leaking_cross_mentions(generator):
    g, (_, s, _, _, api, slack) = generator
    stamp = now()
    slack.conversations_history.side_effect = lambda channel, **kwargs: {'messages': [{
        'user': 'U1', 'ts': str((stamp - timedelta(minutes=5)).timestamp()),
        'text': 'Cecilia Threadmaker recommended a measuring jig.', 'reply_count': 2}] if channel == 'CCHAT' else []}
    response = MagicMock(status_code=403, headers={})
    def profile(user):
        if user == 'U3': raise SlackApiError('unavailable', response)
        return {'user': {'id': user, 'real_name': 'Root Maker', 'is_bot': False}}
    slack.users_info.side_effect = profile
    slack.conversations_replies.return_value = {'messages': [
        {'user': 'U2', 'ts': str((stamp - timedelta(minutes=2)).timestamp()), 'text': 'Cecilia Threadmaker provided feedback.'},
        {'user': 'U3', 'ts': str((stamp - timedelta(minutes=1)).timestamp()), 'text': 'Try adjustable stops.'}]}
    result = g.run(rank=1, request_id='profile-failure')
    rendered = json.dumps(api.quest_response.call_args.args[0])
    saved = json.dumps(s.get('ledger_context', 'quest-input:profile-failure'), default=str)
    assert 'Cecilia' not in rendered + saved and result['status'] == 'submitted'
    assert result['retained_inputs']['messages'] == 0


def test_short_reply_only_names_and_escaped_names_are_redacted(generator):
    g, (_, _, _, _, _, slack) = generator
    stamp = now()
    slack.users_info.side_effect = lambda user: {'user': {'id': user, 'real_name': 'Li' if user == 'U3' else 'Root Maker'}}
    root = {'user': 'U1', 'ts': str((stamp - timedelta(minutes=5)).timestamp()),
        'text': 'L&#105; suggested a line jig with a limit stop.', 'reply_count': 1}
    slack.conversations_replies.return_value = {'messages': [{
        'user': 'U3', 'ts': str((stamp - timedelta(minutes=1)).timestamp()), 'text': 'Try safer clamps.'}]}
    rows, _ = g.history.inspiration('CCHAT', [root], stamp - timedelta(days=14), stamp)
    assert rows[0]['text'].startswith('[identity removed] suggested a line jig with a limit stop.')


def test_quote_guard_canonicalizes_slack_reserved_entities(generator):
    g, (_, _, _, _, api, _) = generator
    snapshot = g.context(1, random.Random(1), now())
    snapshot['data']['chat'] = [{'text': 'Try the purple jig &amp; test every stop.'}]
    api.quest_response.return_value = json.dumps({**proposal(), 'description': 'Try the purple jig & test every stop.'})
    with pytest.raises(ValueError): g.compose(snapshot, 'individual')


def test_completed_example_joins_and_anonymizes_original_submission(generator):
    g, (_, s, *_ ) = generator
    s.put("ledger_quests", {"_id": "approved-definition", "kind": "member_quest", "logical_id": "past",
        "creator": str(oid(2)), "reviewer": str(oid(10)), "status": "published", "target_rank": 5, **proposal()})
    s.put("ledger_evidence", {"_id": "past-submission", "kind": "quest_submission", "status": "approved",
        "member_id": str(oid(1)), "reviewer": str(oid(10)), "description": "Maker1 Test demonstrated accurate cuts."})
    s.put("ledger_evidence", {"_id": "past-completion", "kind": "quest_completion", "quest_revision": "approved-definition",
        "submission_id": "past-submission", "member_id": str(oid(1))})
    example = g.examples(random.Random(1))[0]
    assert "demonstrated accurate cuts" in example["outcome"] and "Maker1" not in example["outcome"]
