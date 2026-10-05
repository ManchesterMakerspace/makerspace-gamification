from datetime import timedelta
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from conftest import oid
from ledger.storage import now
from ledger.ticket_quests import TicketQuests, canonical_id, config, sanitize_jpeg, ticket_key, validate_config
from ledger.worker import Worker


def setup_tickets(joined, *, reporter=1, status="open", category="broken"):
    ledger, store, source, *_ = joined
    ledger.join(str(oid(1)))
    ledger.join(str(oid(2)))
    source.data["fix_tickets"].append({"_id": 735, "reporter_id": oid(reporter), "category": category,
        "status": status, "created_at": now(), "updated_at": now(), "revision": 1})
    service = TicketQuests(ledger)
    service.save_config(str(oid(10)), {"quest_channel_id": "CQUEST", "fix_channel_id": "CFIX",
        "duration_hours": 18, "xp_with_image": 100, "xp_without_image": 66})
    source.data["fix_tickets"][-1]["created_at"] = now()
    return ledger, store, source, service


def open_quest(service, store, source, ticket_id=735):
    service.reconcile(ticket_id)
    quest = store.get("ledger_evidence", ticket_key(ticket_id))
    quest.update(status="open", announcement_ts="900.001", announcement_channel="CQUEST",
                 started_at=now(), deadline=now() + timedelta(hours=18))
    store.put("ledger_evidence", quest)
    return quest


def test_ticket_id_supports_sequence_and_legacy_objectid():
    assert canonical_id(735) == "735"
    assert canonical_id(str(oid(800))) == str(oid(800))
    with pytest.raises(ValueError):
        canonical_id(0)


def test_config_is_validated_and_quests_are_off_until_enabled(joined):
    ledger, store, source, *_ = joined
    service = TicketQuests(ledger)
    source.data["fix_tickets"].append({"_id": 735, "reporter_id": oid(2), "category": "broken", "status": "open"})
    service.reconcile(735)
    assert store.get("ledger_evidence", ticket_key(735)) is None
    with pytest.raises(ValueError, match="channel ID"):
        validate_config({"quest_channel_id": "bad", "fix_channel_id": "CFIX", "duration_hours": 18,
                         "xp_with_image": 100, "xp_without_image": 66})


def test_ticket_event_hint_creates_once_and_uses_current_ticket_state(joined):
    ledger, store, source, service = setup_tickets(joined)
    service.reconcile(735)
    service.reconcile(735)
    quest = store.get("ledger_evidence", ticket_key(735))
    assert quest["status"] == "announcing"
    assert len(store.select("ledger_outbox", {"kind": "ticket_quest_announce"})) == 1
    source.data["fix_tickets"][0]["status"] = "resolved"
    service.reconcile(735)
    # Terminal state before announcement delivery cancels the stale announcement.
    assert store.get("ledger_evidence", ticket_key(735))["status"] == "closed"
    assert store.get("ledger_outbox", "ticket-quest-announcement:735")["status"] == "cancelled"


def test_first_sentence_claim_awards_exactly_once_and_excludes_reporter(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "901.001", "text": "I checked the switch and confirmed the motor is jammed."}, "event-1")
    assert store.get("ledger_evidence", quest["_id"])["winner_member_id"] == str(oid(2))
    assert ledger.participant(str(oid(2)))["xp"] == "66"
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "901.001", "text": "I checked the switch and confirmed the motor is jammed."}, "event-1-retry")
    assert ledger.participant(str(oid(2)))["xp"] == "66"
    # Reporter is excluded even when opted in; their reply is recorded but never wins.
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U1", "ts": "901.002", "text": "I looked at this ticket."}, "event-2")
    assert store.get("ledger_evidence", quest["_id"])["winner_member_id"] == str(oid(2))


def test_jpeg_claim_awards_configured_100_and_two_concurrent_claims_have_one_winner(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    service.worker = MagicMock()
    with patch.object(TicketQuests, "_download_jpeg", return_value=b"jpeg"):
        service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
            "user": "U2", "ts": "903.001", "text": "I checked the belt and confirmed the issue.",
            "files": [{"id": "F1", "mimetype": "image/jpeg"}]}, "event-image")
    assert ledger.participant(str(oid(2)))["xp"] == "100"

    source.data["fix_tickets"].append({"_id": 736, "reporter_id": oid(1), "category": "damaged", "status": "open", "created_at": now()})
    service.worker = None
    service.reconcile(736)
    race = store.get("ledger_evidence", ticket_key(736))
    race.update(status="open", announcement_ts="904.001", announcement_channel="CQUEST", deadline=now() + timedelta(hours=18))
    store.put("ledger_evidence", race)
    ledger.join(str(oid(3)))
    claims = [("U2", "904.002"), ("U3", "904.003")]
    def claim(item):
        uid, ts = item
        return service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": race["announcement_ts"],
            "user": uid, "ts": ts, "text": "I inspected the tool and confirmed the damaged part."}, ts)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(claim, claims))
    race = store.get("ledger_evidence", race["_id"])
    assert race["winner_member_id"] in {str(oid(2)), str(oid(3))}
    assert sum(int(ledger.participant(str(oid(i)))["xp"]) for i in (2, 3)) == 166


def test_ignores_nonthread_and_non_sentence_messages(joined):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    assert not service.response_event({"type": "message", "channel": "CQUEST", "user": "U2",
        "ts": "902.001", "text": "This is a sentence."}, "event")
    service.response_event({"type": "message", "channel": "CQUEST", "thread_ts": quest["announcement_ts"],
        "user": "U2", "ts": "902.002", "text": "I checked it"}, "event")
    assert store.get("ledger_evidence", quest["_id"]).get("winner_member_id") is None


def test_file_share_thread_reply_can_qualify_with_jpeg(joined):
    ledger, store, source, _ = setup_tickets(joined)
    quest = open_quest(TicketQuests(ledger), store, source)
    worker = Worker(ledger, joined[3], joined[5], bot_id="UBOT")
    with patch.object(TicketQuests, "_download_jpeg", return_value=b"sanitized-jpeg"):
        result = worker.event({"type": "message", "subtype": "file_share", "channel": "CQUEST",
            "thread_ts": quest["announcement_ts"], "user": "U2", "ts": "905.001",
            "text": "I checked the belt and confirmed the issue.",
            "files": [{"id": "F1", "mimetype": "image/jpeg"}]}, "file-share-event")
    assert result is True
    assert ledger.participant(str(oid(2)))["xp"] == "100"


def test_jpeg_download_stream_stops_at_limit_before_pillow(joined):
    ledger, _, _, _, _, slack = joined
    worker = Worker(ledger, joined[3], slack, bot_id="UBOT")
    slack.files_info.return_value = {"file": {"mimetype": "image/jpeg",
        "url_private_download": "https://files.slack.com/private/F1"}}
    worker.slack.token = "xoxb-test"

    class Response:
        headers = {}
        closed = False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.closed = True
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"x" * (12 * 1024 * 1024)
            yield b"overflow"

    response = Response()
    with patch("ledger.ticket_quests.requests.get", return_value=response), \
         patch("ledger.ticket_quests.sanitize_jpeg") as sanitize:
        assert TicketQuests(ledger, worker)._download_jpeg({"image_file_id": "F1"}) is None
    sanitize.assert_not_called()
    assert response.closed


def test_image_filename_is_unique_for_each_response():
    quest = {"ticket_key": "735"}
    first = {"_id": "ticket-quest-response:abc123", "slack_id": "U2"}
    second = {"_id": "ticket-quest-response:def456", "slack_id": "U2"}
    assert TicketQuests._image_filename(quest, first) != TicketQuests._image_filename(quest, second)


@pytest.mark.parametrize("change", [("category", "other"), ("status", "awaiting_review")])
def test_quest_closes_when_ticket_becomes_ineligible(joined, change):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"][0][change[0]] = change[1]
    service.reconcile(735)
    closed = store.get("ledger_evidence", quest["_id"])
    assert closed["status"] == "closed"
    assert closed["outcome"] == "ineligible"
    assert store.get("ledger_outbox", f"ticket-quest-update:{quest['_id']}:ineligible")


@pytest.mark.parametrize("targeted", [True, False])
def test_deleted_ticket_closes_quest_during_targeted_or_hourly_reconciliation(joined, targeted):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"].clear()
    service.reconcile(735) if targeted else service.reconcile()
    closed = store.get("ledger_evidence", quest["_id"])
    assert closed["status"] == "closed"
    assert closed["outcome"] == "deleted"
    assert closed["final_ticket_status"] == "deleted"
    assert store.get("ledger_outbox", f"ticket-quest-update:{quest['_id']}:deleted")


def test_reply_to_deleted_ticket_closes_quest_instead_of_leaving_it_open(joined):
    ledger, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"].clear()
    handled = service.response_event({"type": "message", "channel": "CQUEST",
        "thread_ts": quest["announcement_ts"], "user": "U2", "ts": "906.001",
        "text": "I checked the tool and confirmed the issue."}, "deleted-ticket-reply")
    assert handled is True
    closed = store.get("ledger_evidence", quest["_id"])
    assert closed["status"] == "closed" and closed["outcome"] == "deleted"
    assert ledger.participant(str(oid(2)))["xp"] == "0"


def test_ticket_quest_announcement_narration_uses_persisted_composition(joined):
    ledger, store, _, composer, _, slack = joined
    composer.compose = MagicMock()
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    worker.persist_composition = MagicMock(return_value={"text": "The quest has ended."})
    quest_id = ticket_key(735)
    store.put("ledger_evidence", {"_id": quest_id, "kind": "broken_ticket_quest", "ticket_key": "735",
        "status": "closed", "announcement_channel": "CQUEST", "announcement_ts": "900.001",
        "final_ticket_status": "resolved"})
    job = {"_id": "ticket-quest-update:735:closed", "payload": {"quest_id": quest_id, "outcome": "closed"}}
    TicketQuests(ledger, worker)._update_announcement(job)
    worker.persist_composition.assert_called_once()
    composer.compose.assert_not_called()
    assert slack.chat_update.call_args.kwargs["text"].startswith("The quest has ended.")


def test_response_thanks_uses_persisted_composition(joined):
    ledger, store, _, composer, _, slack = joined
    composer.compose = MagicMock()
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    worker.persist_composition = MagicMock(return_value={"text": "Thanks for the repair report."})
    response_id = "ticket-quest-response:abc123"
    quest_id = ticket_key(735)
    store.put("ledger_evidence", {"_id": quest_id, "kind": "broken_ticket_quest", "ticket_key": "735",
        "status": "won", "xp_awarded": 100})
    store.put("ledger_evidence", {"_id": response_id, "kind": "broken_ticket_quest_response", "quest_id": quest_id,
        "slack_id": "U2", "channel": "CQUEST", "thread_ts": "900.001", "text": "I checked the motor.",
        "delivery": {"fix_channel": "CFIX"}, "rails_note_written": True})
    service = TicketQuests(ledger, worker)
    service._deliver_response({"_id": response_id, "payload": {"response_id": response_id, "winner": False}})
    worker.persist_composition.assert_called_once()
    composer.compose.assert_not_called()
    assert slack.chat_postMessage.call_args.kwargs["text"] == "Thanks for the repair report."


@pytest.mark.parametrize("terminal", ["resolved", "rejected", "withdrawn"])
def test_terminal_ticket_closes_unanswered_quest_and_reopen_does_not_revive(joined, terminal):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    source.data["fix_tickets"][0]["status"] = terminal
    service.reconcile(735)
    assert store.get("ledger_evidence", quest["_id"])["status"] == "closed"
    source.data["fix_tickets"][0]["status"] = "open"
    service.reconcile(735)
    assert store.get("ledger_evidence", quest["_id"])["status"] == "closed"


def test_expiration_closes_without_winner(joined):
    _, store, source, service = setup_tickets(joined)
    quest = open_quest(service, store, source)
    quest["deadline"] = now() - timedelta(seconds=1)
    store.put("ledger_evidence", quest)
    service.reconcile(735)
    assert store.get("ledger_evidence", quest["_id"])["status"] == "expired"


def test_jpeg_sanitizer_strips_exif_and_rejects_non_jpeg():
    image = Image.new("RGB", (4, 3), "red")
    exif = Image.Exif()
    exif[270] = "private camera note"
    source = BytesIO()
    image.save(source, format="JPEG", exif=exif)
    result = sanitize_jpeg(source.getvalue())
    assert Image.open(BytesIO(result)).getexif() == {}
    png = BytesIO()
    image.save(png, format="PNG")
    assert sanitize_jpeg(png.getvalue()) is None


def test_jpeg_sanitizer_rejects_excessive_dimensions_before_transform():
    source = MagicMock(format="JPEG", width=5000, height=5000)
    with patch("ledger.ticket_quests.Image.open", return_value=source), \
         patch("ledger.ticket_quests.ImageOps.exif_transpose") as transpose:
        assert sanitize_jpeg(b"jpeg") is None
    transpose.assert_not_called()


def test_config_save_records_revision_and_audit(joined):
    ledger, store, *_ = joined
    service = TicketQuests(ledger)
    saved = service.save_config(str(oid(10)), {"quest_channel_id": "CQUEST", "fix_channel_id": "CFIX",
        "duration_hours": 24, "xp_with_image": 120, "xp_without_image": 70})
    assert config(ledger)["revision"] == 1
    assert saved["enabled"] is True
    assert store.get("ledger_evidence", "broken-ticket-quest-config:1")["actor"] == str(oid(10))
