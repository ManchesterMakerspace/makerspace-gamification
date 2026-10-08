import base64
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from threading import Event, Thread
from unittest.mock import MagicMock

from PIL import Image
import pytest

from conftest import oid
from ledger import avatars
from ledger.avatar_runtime import Runtime, RuntimeBusy
from ledger.cli import outbox_filters
from ledger.domain import Denied, Ledger
from ledger.storage import now
from ledger.views import home, preferences
from ledger.worker import HistoryImportPending, Worker


def image_bytes():
    buffer = BytesIO()
    Image.new("RGBA", (1280, 1280), (80, 90, 100, 150)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def setup(env, tmp_path):
    ledger, store, source, composer, api, slack = env
    member = str(oid(1))
    ledger.join(member)
    p = ledger.participant(member)
    p["import_pending"] = False
    store.put("ledger_participants", p)
    api.complete_with_usage.return_value = {"content": "A centered fantasy artisan in a warm workshop.",
        "usage": {"prompt_tokens": 90, "completion_tokens": 20, "total_tokens": 110}}
    api.tokenize.return_value = 512
    slack.users_profile_get.return_value = {"profile": {}}
    slack.users_info.side_effect = lambda user: {"user": {"id": user, "name": "maker", "is_bot": False, "profile": {}}}
    slack.files_upload_v2.side_effect = [{"files": [{"id": "F_FULL"}]}, {"files": [{"id": "F_SMALL"}]}]
    slack.files_info.side_effect = lambda file: {"file": {"id": file, "permalink": "https://example.slack.com/files/" + file}}
    runtime = MagicMock()
    runtime.generate.return_value = {"image": base64.b64encode(image_bytes()).decode(), "metrics": {"peak_memory_mb": 42}}
    worker = Worker(ledger, composer, slack)
    pipeline = avatars.AvatarPipeline(worker, runtime=runtime, directory=tmp_path)
    job = store.claim("ledger_outbox", now() + timedelta(seconds=61), kinds=["avatar_generate"])
    return ledger, store, api, slack, runtime, worker, pipeline, job, member


def test_exact_jpg_pair_and_transparency():
    pair = avatars.jpg_pair(image_bytes())
    for key, size in (("avatar", 1254), ("avatar512", 512)):
        with Image.open(BytesIO(pair[key])) as image:
            assert image.format == "JPEG" and image.mode == "RGB"
            assert image.size == (size, size)
            assert not image.getexif()


def test_generation_activation_notice_and_metrics(setup, capsys):
    ledger, store, api, slack, runtime, worker, pipeline, job, member = setup
    pipeline.generate(job)
    row = avatars.current(store, member)
    assert row["avatar"]["file_id"] == "F_FULL" and row["avatar512"]["file_id"] == "F_SMALL"
    assert row["job_end_time"] and row["duration_seconds"] >= 0
    assert row["token_usage"]["prompt_generation"]["total_tokens"] == 110
    assert '"slack_username": "maker"' in capsys.readouterr().out
    assert home(ledger, member)["blocks"][2]["slack_file"]["id"] == "F_FULL"
    notice = store.claim("ledger_outbox", kinds=["avatar_notice"])
    avatars.deliver(worker, notice)
    assert "uncheck Allow personalized avatars" in slack.chat_postMessage.call_args.kwargs["text"]
    assert slack.chat_postMessage.call_args.kwargs["blocks"][1]["slack_file"]["id"] == "F_SMALL"
    avatars.deliver(worker, notice)
    assert slack.chat_postMessage.call_count == 1
    pipeline.idle()
    runtime.unload.assert_called_once()


def test_partial_upload_retries_without_inference(setup):
    ledger, store, api, slack, runtime, worker, pipeline, job, member = setup
    slack.files_upload_v2.side_effect = [{"files": [{"id": "F_FULL"}]}, OSError(), {"files": [{"id": "F_SMALL"}]}]
    with pytest.raises(OSError):
        pipeline.generate(job)
    assert avatars.current(store, member) is None
    worker.finish("ledger_outbox", job, "pending")
    retry = store.claim("ledger_outbox", kinds=["avatar_generate"])
    pipeline.generate(retry)
    assert runtime.generate.call_count == api.complete_with_usage.call_count == 1
    assert slack.files_upload_v2.call_count == 3
    assert len(store.get("ledger_avatars", "job:" + job["_id"])["attempt_metrics"]) == 2


def test_file_info_failure_keeps_confirmed_upload_id(setup):
    _, store, api, slack, runtime, worker, pipeline, job, member = setup
    slack.files_info.side_effect = OSError()
    with pytest.raises(OSError):
        pipeline.generate(job)
    assert store.get("ledger_avatars", "job:" + job["_id"])["avatar"]["file_id"] == "F_FULL"
    slack.files_info.side_effect = lambda file: {"file": {"id": file, "permalink": "https://example.slack.com/files/" + file}}
    worker.finish("ledger_outbox", job, "pending")
    pipeline.generate(store.claim("ledger_outbox", kinds=["avatar_generate"]))
    assert slack.files_upload_v2.call_count == 2
    assert runtime.generate.call_count == api.complete_with_usage.call_count == 1


def test_identity_preflight_failures_record_every_attempt_and_terminal_end(setup, capsys):
    _, store, api, slack, runtime, worker, pipeline, job, _ = setup
    job.update(status="pending", attempts=0, available_at=now())
    store.put("ledger_outbox", job)
    worker.avatar_pipeline = pipeline
    slack.users_info.side_effect = OSError("Transient users.info transport failure")
    for attempt in range(1, 11):
        assert worker.step("ledger_outbox", kinds=["avatar_generate"])
        stored_job = store.get("ledger_outbox", job["_id"])
        row = store.get("ledger_avatars", "job:" + job["_id"])
        assert stored_job["attempts"] == attempt
        assert len(row["attempt_metrics"]) == attempt
        metric = row["attempt_metrics"][-1]
        assert metric["attempt"] == attempt and metric["outcome"] == "failed"
        assert metric["job_end_time"] and metric["duration_seconds"] >= 0
        assert metric["token_usage"] == {}
        if attempt < 10:
            assert stored_job["status"] == "pending"
            stored_job["available_at"] = now()
            store.put("ledger_outbox", stored_job)
    assert stored_job["status"] == row["status"] == "failed"
    assert row["job_end_time"] == row["attempt_metrics"][-1]["job_end_time"]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 10
    assert [event["attempt"] for event in events] == list(range(1, 11))
    assert all(event["event"] == "avatar_job" and event["slack_username"] == "U1"
               and event["job_end_time"] for event in events)
    assert store.get("ledger_catalog", avatars.RUNTIME_LEASE)["until"] <= now()
    runtime.generate.assert_not_called()
    api.complete_with_usage.assert_not_called()
    slack.files_upload_v2.assert_not_called()


def test_invalid_cached_avatar_restores_default_and_queues_repair(setup):
    ledger, store, _, _, _, _, pipeline, job, member = setup
    pipeline.generate(job)
    avatars.invalidate(ledger, member, "F_FULL")
    assert avatars.visible(ledger, member) is None
    assert avatars.revision(ledger, member) == "default"
    assert store.exists("ledger_outbox", {"kind": "avatar_generate", "status": "pending"})


def test_second_reference_selection_supersedes_first_without_deleting_user_file(setup, monkeypatch):
    ledger, store, api, slack, _, worker, _, _, member = setup
    ledger.preferences(member, True, True, reference_file="F_USER_ONE")
    ledger.preferences(member, True, True, reference_file="F_USER_TWO")
    first = store.claim("ledger_outbox", kinds=["avatar_reference"])
    with pytest.raises(Denied):
        avatars.save_reference(worker, first)
    second = store.claim("ledger_outbox", kinds=["avatar_reference"])
    slack.files_info.side_effect = lambda file: {"file": {"id": file, "user": "U1", "size": 100,
                                                         "url_private": "https://files.slack.com/ref.png"}}
    monkeypatch.setattr(avatars, "download", lambda *args: image_bytes())
    api.complete.return_value = "An illustrated maker wearing a workshop apron."
    avatars.save_reference(worker, second)
    assert avatars.custom_reference(store, member)["file_id"] == "F_USER_TWO"
    assert store.get("ledger_outbox", second["_id"])["status"] == "working"
    slack.files_delete.assert_not_called()
    ledger.preferences(member, True, True, remove_reference=True)
    assert avatars.custom_reference(store, member) is None


def test_oversize_and_non_owned_references_do_not_replace_current(setup):
    _, store, _, slack, _, worker, _, generation, member = setup
    job = {**generation, "kind": "avatar_reference", "payload": {**generation["payload"], "file_id": "F_USER"}}
    for owner, size in (("U2", 100), ("U1", 2_000_000)):
        slack.files_info.side_effect = lambda file: {"file": {"id": file, "user": owner, "size": size}}
        with pytest.raises(Denied):
            avatars.save_reference(worker, job)
    assert avatars.custom_reference(store, member) is None


def test_idle_is_not_polled_after_confirmed_unload(setup):
    _, _, _, _, runtime, _, pipeline, _, _ = setup
    pipeline.idle()
    pipeline.idle()
    runtime.unload.assert_called_once()


def test_opt_out_during_inference_cannot_activate(setup):
    ledger, store, api, slack, runtime, worker, pipeline, job, member = setup
    image = runtime.generate.return_value
    def withdraw(*args):
        ledger.preferences(member, True, True, avatars=False)
        return image
    runtime.generate.side_effect = withdraw
    with pytest.raises(Denied):
        pipeline.generate(job)
    assert avatars.current(store, member) is None
    slack.files_upload_v2.assert_not_called()
    assert store.get("ledger_avatars", "job:" + job["_id"])["status"] == "cancelled"


def test_opt_out_during_upload_retains_file_for_cleanup(setup):
    ledger, store, _, slack, _, _, pipeline, job, member = setup
    def withdraw(**kwargs):
        ledger.preferences(member, True, True, avatars=False)
        return {"files": [{"id": "F_CANCELLED"}]}
    slack.files_upload_v2.side_effect = withdraw
    with pytest.raises(Denied):
        pipeline.generate(job)
    assert avatars.current(store, member) is None
    assert store.get("ledger_avatars", "job:" + job["_id"])["avatar"]["file_id"] == "F_CANCELLED"
    assert store.exists("ledger_outbox", {"kind": "avatar_cleanup", "payload.files": ["F_CANCELLED"]})


def test_reclaimed_activated_job_never_regenerates_after_spool_cleanup(setup):
    _, _, api, slack, runtime, _, pipeline, job, _ = setup
    pipeline.generate(job)
    for path in pipeline.directory.iterdir():
        path.unlink()
    pipeline.generate(job)
    assert runtime.generate.call_count == api.complete_with_usage.call_count == 1
    assert slack.files_upload_v2.call_count == 2


def test_rank_change_during_generation_keeps_old_pair(setup):
    ledger, store, api, slack, runtime, worker, pipeline, job, member = setup
    old = {"_id": "current:" + member, "kind": "current", "slack_id": "U1", "rank": 1, "revision": "old",
           "avatar": {"file_id": "F_OLD"}, "avatar512": {"file_id": "F_OLD_SMALL"}}
    store.put("ledger_avatars", old)
    image = runtime.generate.return_value
    def promote(*args):
        p = ledger.participant(member)
        p["rank"] = 2
        store.put("ledger_participants", p)
        return image
    runtime.generate.side_effect = promote
    with pytest.raises(Denied):
        pipeline.generate(job)
    assert avatars.current(store, member) == old


def test_global_runtime_lease_defers_competing_worker(setup):
    _, store, api, _, runtime, _, pipeline, job, _ = setup
    store.put("ledger_catalog", {"_id": avatars.RUNTIME_LEASE, "owner": "other", "until": now() + timedelta(minutes=1)})
    with pytest.raises(HistoryImportPending):
        pipeline.generate(job)
    runtime.generate.assert_not_called()
    api.complete_with_usage.assert_not_called()


def test_preferences_preserve_other_fields_and_restore_default(setup):
    ledger, store, _, _, _, _, _, job, member = setup
    p = ledger.participant(member)
    p["preferences"]["future_preference"] = "keep"
    store.put("ledger_participants", p)
    ledger.preferences(member, False, True, avatars=False)
    assert ledger.participant(member)["preferences"] == {"future_preference": "keep", "observation": False,
                                                         "arrival_mentions": True, "avatars": False}
    assert store.get("ledger_outbox", job["_id"])["status"] == "cancelled"
    assert avatars.visible(ledger, member) is None
    view = preferences(ledger, member)
    assert any(b.get("block_id") == "avatar_reference" for b in view["blocks"])
    ledger.preferences(member, False, True, avatars=True)
    assert store.exists("ledger_outbox", {"kind": "avatar_generate", "status": "pending"})


def test_opt_out_retires_pair_and_deletes_only_after_default_home(setup):
    ledger, store, _, slack, _, worker, pipeline, job, member = setup
    pipeline.generate(job)
    ledger.preferences(member, True, True, avatars=False)
    assert avatars.current(store, member) is None
    assert store.get("ledger_avatars", "current:" + member)["kind"] == "current_removed"
    cleanup = store.claim("ledger_outbox", kinds=["avatar_cleanup"])
    while cleanup and cleanup["_id"] != "avatar-opt-out:" + job["_id"]:
        cleanup = store.claim("ledger_outbox", kinds=["avatar_cleanup"])
    assert cleanup
    with pytest.raises(HistoryImportPending):
        avatars.deliver(worker, cleanup)
    slack.files_delete.assert_not_called()
    store.put("ledger_homes", {"_id": member, "published_avatar_revision": "default"})
    avatars.deliver(worker, cleanup)
    assert {c.kwargs["file"] for c in slack.files_delete.call_args_list} == {"F_FULL", "F_SMALL"}
    ledger.preferences(member, True, True, avatars=True)
    assert avatars.visible(ledger, member) is None


def test_downward_rank_correction_cancels_inflight_and_queues_replacement(setup):
    ledger, store, _, _, _, _, pipeline, job, member = setup
    p = ledger.participant(member)
    p["rank"] = 2
    store.put("ledger_participants", p)
    before = p.get("avatar_generation", 0)
    ledger.correct_rank(str(oid(10)), member, 1, "Independent correction")
    assert ledger.participant(member)["avatar_generation"] == before + 1
    assert store.get("ledger_outbox", job["_id"])["status"] == "cancelled"
    with pytest.raises(Denied):
        pipeline.generate(job)
    replacement = store.select("ledger_outbox", {"kind": "avatar_generate", "status": "pending"})
    assert len(replacement) == 1
    assert replacement[0]["payload"]["avatar_generation"] == before + 1


def test_milestones_coalesce_but_inflight_gets_followup(setup):
    ledger, store, _, _, _, _, _, job, member = setup
    first = ledger.store.atomic(lambda s: avatars.request(Ledger(s, ledger.sources), member, "quest:one"))
    second = ledger.store.atomic(lambda s: avatars.request(Ledger(s, ledger.sources), member, "shop:one"))
    assert first == second and first != job["_id"]
    ledger.store.atomic(lambda s: avatars.request(Ledger(s, ledger.sources), member, "quest:one"))
    assert len(store.select("ledger_outbox", {"kind": "avatar_generate"})) == 2


def test_midnight_keys_follow_new_york_and_dedupe(env):
    ledger, store, *_ = env
    for clock in (datetime(2026, 11, 1, 3, 59, tzinfo=timezone.utc), datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc),
                  datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)):
        avatars.backfill(ledger, clock)
    assert store.get("ledger_inbox", "avatar-backfill:2026-10-31")
    assert store.get("ledger_inbox", "avatar-backfill:2026-11-01")
    assert len(store.select("ledger_inbox", {"kind": "avatar_backfill"})) == 2


def test_chat_context_is_own_live_current_consent_only(setup):
    ledger, store, _, _, _, worker, _, _, member = setup
    generation = ledger.participant(member)["consent_generation"]
    for id, author, channel, consent, expiry, text in [
        ("ok", member, "DU1", generation, 1, "I like woodworking"),
        ("other", str(oid(2)), "DU1", generation, 1, "Other private interests"),
        ("old", member, "DU1", generation - 1, 1, "Old interests"),
        ("expired", member, "DU1", generation, -1, "Expired interests"),
        ("channel", member, "CCHAT", generation, 1, "Unobserved interests"),
        ("secret", member, "DU1", generation, 1, "my password is private")]:
        store.put("ledger_context", {"_id": id, "kind": "message", "member_id": author, "channel": channel,
            "consent_generation": consent, "participating": True, "expires_at": now() + timedelta(days=expiry), "at_order": 1, "text": text})
    facts = avatars.context(worker, member, {"profile": {}})
    assert facts["chat"] == [{"source": "ok", "text": "I like woodworking"}]


def test_cleanup_waits_for_home_and_never_deletes_current(setup):
    ledger, store, _, slack, _, worker, pipeline, job, member = setup
    store.put("ledger_avatars", {"_id": "current:" + member, "kind": "current", "rank": 1, "revision": "new",
        "slack_id": "U1", "avatar": {"file_id": "F_NEW"}, "avatar512": {"file_id": "F_NEW_SMALL"}})
    cleanup = {**job, "kind": "avatar_cleanup", "payload": {"member_id": member, "files": ["F_OLD", "F_NEW"]}}
    with pytest.raises(HistoryImportPending):
        avatars.deliver(worker, cleanup)
    store.put("ledger_homes", {"_id": member, "published_avatar_revision": "new"})
    avatars.deliver(worker, cleanup)
    slack.files_delete.assert_called_once_with(file="F_OLD")


def test_avatar_queue_is_disjoint():
    assert outbox_filters("avatars")["kinds"] == avatars.GENERATION
    assert set(avatars.GENERATION) <= set(outbox_filters("outbox")["exclude"])


def test_runtime_single_flight_and_lost_response_replay(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime._load = MagicMock()
    runtime._unload = MagicMock()
    entered, release = Event(), Event()
    def respond(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return MagicMock(json=lambda: {"data": [{"b64_json": "image"}]})
    monkeypatch.setattr("ledger.avatar_runtime.requests.post", respond)
    request = {"request_id": "one", "prompt": "portrait", "references": [base64.b64encode(b"ref").decode()], "seed": 1}
    result = []
    thread = Thread(target=lambda: result.append(runtime.generate(request)))
    thread.start()
    assert entered.wait(5)
    with pytest.raises(RuntimeBusy):
        runtime.generate({**request, "request_id": "two"})
    with pytest.raises(RuntimeBusy):
        runtime.unload()
    release.set()
    thread.join(5)
    assert result and runtime.generate(request) == result[0]
    assert runtime._load.call_count == 1
    with pytest.raises(ValueError):
        runtime.generate({**request, "prompt": "different"})


def test_runtime_timeout_terminates_inference(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime._load = MagicMock()
    runtime._unload = MagicMock()
    monkeypatch.setattr("ledger.avatar_runtime.requests.post", MagicMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):
        runtime.generate({"request_id": "one", "prompt": "portrait", "references": ["cmVm"], "seed": 1})
    runtime._unload.assert_called_once()
    assert runtime.active is None
