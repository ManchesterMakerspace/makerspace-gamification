import argparse
from copy import deepcopy
from datetime import timedelta
import io
from unittest.mock import patch

from pymongo.errors import PyMongoError
from slack_sdk.errors import SlackApiError

from ledger.sponsorship_reminder import (eligible_participants, load_variants, parse_days, run)
from ledger.storage import now
from ledger.worker import Worker

from conftest import oid


def options(days, **kwargs):
    return argparse.Namespace(days=days, variants=None, dry_run=kwargs.get("dry_run", False),
        generate=kwargs.get("generate", False), verbose=kwargs.get("verbose", False),
        debug=kwargs.get("debug", False), limit=kwargs.get("limit"), member=kwargs.get("member"))


def test_days_select_never_and_stale_inviters(joined):
    ledger, store, *_ = joined
    giver, recipient = str(oid(1)), str(oid(3))
    ledger.sponsor(giver, recipient)
    row = store.get("ledger_relationships", f"sponsor-invite:{giver}:{recipient}")
    row["at"] = now() - timedelta(days=31)
    store.put("ledger_relationships", row)

    never = eligible_participants(ledger, "never")
    stale = eligible_participants(ledger, 30)
    assert [row["member_id"] for row in never] == [str(oid(2))]
    assert [row["member_id"] for row in stale] == [giver]
    assert parse_days("NEVER") == "never"
    assert parse_days("14") == 14


def test_dry_run_prints_prompt_without_writes_or_posts(joined):
    ledger, store, _, composer, _, slack = joined
    original = deepcopy(store.data)
    output = io.StringIO()
    totals = run(ledger, composer, slack, options("never", dry_run=True), stdout=output, stderr=io.StringIO())
    assert totals == {"sent": 0, "updated": 0, "failed": 0, "mongo_updated": 0}
    assert '"action": "send"' in output.getvalue()
    assert store.data == original
    slack.chat_postMessage.assert_not_called()
    slack.chat_update.assert_not_called()


def test_dry_run_generate_prints_text_without_writes(joined):
    ledger, store, _, composer, api, slack = joined
    original = deepcopy(store.data)
    api.complete.return_value = "Invite someone you know if they are interested."
    output = io.StringIO()
    run(ledger, composer, slack, options("never", dry_run=True, generate=True),
        stdout=output, stderr=io.StringIO())
    assert "generated_text" in output.getvalue()
    assert "qwen_prompt" not in output.getvalue()
    assert store.data == original
    slack.chat_postMessage.assert_not_called()


def test_new_reminder_saves_receipt_then_recent_run_updates_same_dm(joined):
    ledger, store, _, composer, _, slack = joined
    # Member 1 has previously invited someone, so target only their old invite history.
    giver, recipient = str(oid(1)), str(oid(3))
    ledger.sponsor(giver, recipient)
    row = store.get("ledger_relationships", f"sponsor-invite:{giver}:{recipient}")
    row["at"] = now() - timedelta(days=31)
    store.put("ledger_relationships", row)

    first = run(ledger, composer, slack, options(30), stdout=io.StringIO(), stderr=io.StringIO())
    receipt = ledger.participant(giver)
    sent_at = receipt["reminder_sent_at"]
    assert first["sent"] == 1 and first["mongo_updated"] == 1
    assert receipt["reminder_ts"] == "123.456"
    assert receipt["reminder_channel"] == "DU1"

    second = run(ledger, composer, slack, options(30), stdout=io.StringIO(), stderr=io.StringIO())
    assert second["updated"] == 1
    assert second["mongo_updated"] == 1
    assert ledger.participant(giver)["reminder_sent_at"] == sent_at
    assert ledger.participant(giver)["reminder_accepted_template"]
    assert slack.chat_update.call_args.kwargs["channel"] == "DU1"
    assert slack.chat_update.call_args.kwargs["ts"] == "123.456"


def test_custom_variant_file_has_all_required_categories():
    assert set(load_variants()) == {"reminder_never", "reminder_previous", "invitation_accepted"}


def test_invitation_acceptance_queues_and_delivers_recent_followup(joined):
    ledger, store, _, composer, _, slack = joined
    inviter, accepted = str(oid(1)), str(oid(3))
    participant = ledger.participant(inviter)
    participant.update(reminder_ts="123.456", reminder_sent_at=now(), reminder_channel="DU1")
    store.put("ledger_participants", participant)
    ledger.sponsor(inviter, accepted)
    ledger.join(accepted, sponsor=inviter)

    job = store.get("ledger_outbox", f"sponsorship-reminder-accepted:{accepted}")
    assert job and job["kind"] == "sponsorship_reminder_followup"
    assert job["payload"]["member_id"] == inviter
    worker = Worker(ledger, composer, slack)
    assert worker.step("ledger_outbox", kinds=["sponsorship_reminder_followup"])
    text = slack.chat_update.call_args.kwargs["text"]
    assert "Maker3 Test" in text
    assert "<#CCHAT>" in text
    assert store.get("ledger_outbox", job["_id"])["status"] == "done"


def test_invitation_delivery_followup_is_only_queued_for_recent_receipt(joined):
    ledger, store, _, composer, _, slack = joined
    worker = Worker(ledger, composer, slack)
    inviter, recipient = str(oid(1)), str(oid(3))
    assert not worker.queue_sponsorship_reminder_followup(inviter, "invitation_sent", recipient)
    participant = ledger.participant(inviter)
    participant.update(reminder_ts="123.456", reminder_sent_at=now(), reminder_channel="DU1")
    store.put("ledger_participants", participant)
    assert worker.queue_sponsorship_reminder_followup(inviter, "invitation_sent", recipient)
    assert store.get("ledger_outbox", f"sponsorship-reminder-invited:{inviter}:{recipient}")


def test_successful_sponsor_invitation_delivery_queues_followup(joined):
    ledger, store, _, composer, _, slack = joined
    inviter, recipient = str(oid(1)), str(oid(3))
    participant = ledger.participant(inviter)
    participant.update(reminder_ts="123.456", reminder_sent_at=now(), reminder_channel="DU1")
    store.put("ledger_participants", participant)
    ledger.sponsor(inviter, recipient)
    invitation_job = store.select("ledger_outbox", {"kind": "message", "payload.type": "invitation"})[0]
    invitation_job.update(status="working", lease="test-lease")
    store.put("ledger_outbox", invitation_job)

    worker = Worker(ledger, composer, slack)
    worker.outbox(invitation_job)

    followup = store.get("ledger_outbox", f"sponsorship-reminder-invited:{inviter}:{recipient}")
    assert followup and followup["payload"]["event"] == "invitation_sent"


def test_verbose_reports_slack_429_and_prints_failure_summary(joined):
    ledger, _, _, composer, _, slack = joined

    class Response(dict):
        status_code = 429
        headers = {"Retry-After": "19"}

    slack.chat_postMessage.side_effect = SlackApiError("ratelimited", Response(error="ratelimited"))
    stdout, stderr = io.StringIO(), io.StringIO()
    totals = run(ledger, composer, slack, options("never", verbose=True), stdout=stdout, stderr=stderr)
    assert totals["failed"] == 2
    assert "Slack 429 backoff" in stderr.getvalue()
    assert '"failed_message_operations": 2' in stdout.getvalue()


def test_debug_reports_mongo_receipt_write_error_without_counting_send_failure(joined):
    ledger, store, _, composer, _, slack = joined
    original_put = store.put

    def put(collection, document):
        if collection == "ledger_participants" and document.get("member_id") == str(oid(2)):
            raise PyMongoError("database unavailable")
        return original_put(collection, document)

    with patch.object(store, "put", side_effect=put):
        stdout, stderr = io.StringIO(), io.StringIO()
        totals = run(ledger, composer, slack, options("never", debug=True), stdout=stdout, stderr=stderr)
    assert totals["sent"] == 2
    assert totals["failed"] == 0
    assert totals["mongo_updated"] == 1
    assert "Mongo operation failed" in stderr.getvalue()
    assert '"messages_sent": 2' in stdout.getvalue()
