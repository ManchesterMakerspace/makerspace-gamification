"""Transactional review outbox events and recoverable Slack message updates."""
from datetime import timedelta
import hashlib
import json
import os
from uuid import NAMESPACE_URL, uuid4, uuid5

from slack_sdk.errors import SlackApiError

from .domain import Denied
from .messages import button, escape, section
from .quest_policy import REVIEWED_KINDS, generated
from .storage import enqueue, now

COLLECTIONS = ("ledger_evidence", "ledger_quests", "ledger_relationships")
FIELDS = ("review_message_ts", "review_channel_id", "review_message_fingerprint",
          "review_notice_job_id", "review_notice_fingerprint", "review_delivery_lock", "review_post_token")


class ReviewDeliveryBusy(RuntimeError):
    """Another worker holds this activity's delivery lease; defer without spending retries."""


def channel_id():
    return os.environ.get("LEDGER_QUEST_REVIEW_CHANNEL_ID", "").strip()


def watched(collection):
    return bool(channel_id() and collection in COLLECTIONS)


def activities(store, collection, doc):
    """Yield activity locators, mutable field owners, and bounded review facts."""
    kind = doc.get("kind")
    if collection == "ledger_evidence" and kind in ("submission", "quest_submission"):
        if doc.get("quest_link"):
            return  # The top-level quest submission owns its specialized review.
        quest = store.get("ledger_quests", doc.get("quest_revision")) if kind == "quest_submission" else None
        yield None, doc, {"type": "Quest completion" if quest else "Learning/activity evidence",
            "title": (quest or {}).get("title", doc.get("achievement", "Submission")),
            "status": doc.get("status"), "pending": doc.get("status") == "pending",
            "description": doc.get("description", ""), "member": doc.get("member_id"),
            "reviewer": doc.get("reviewer"), "reason": doc.get("reason", ""),
            "acknowledged": len(doc.get("acknowledged", [])), "learners": len(doc.get("learners", []))}
    if collection == "ledger_quests" and kind in REVIEWED_KINDS:
        yield None, doc, {"type": "Quest publication", "title": doc.get("title", "Quest"),
            "generated_quest": generated(doc), "target_rank": doc.get("target_rank"), "quest_type": doc.get("quest_type"),
            "disciplines": doc.get("disciplines", []), "shop_ids": doc.get("shop_ids", []), "tool_ids": doc.get("tool_ids", []),
            "status": doc.get("status"), "pending": doc.get("status") == "pending_review",
            "description": doc.get("description", ""), "criteria": doc.get("criteria", ""),
            "member": doc.get("creator"), "reviewer": doc.get("reviewer"),
            "reason": doc.get("reason") or doc.get("disable_reason", ""), "superseded_by": doc.get("superseded_by")}
    if collection == "ledger_quests" and kind not in REVIEWED_KINDS and "contributions" in doc:
        parent = doc
    elif collection == "ledger_relationships" and kind == "quest_project":
        parent = store.get("ledger_quests", doc.get("quest_revision")) or {}
        verified = [c for c in doc.get("contributions", {}).values() if c.get("status") == "verified"]
        ready = len(verified) >= 2 and {d["name"] for d in parent.get("disciplines", [])} <= {c.get("role") for c in verified}
        status = "pending_completion" if doc.get("status") == "open" and ready else doc.get("status")
        yield None, doc, {"type": "Shared project completion", "title": parent.get("title", "Quest"),
            "quest_revision": doc.get("quest_revision"),
            "status": status, "pending": status == "pending_completion", "description": doc.get("outcome", doc.get("description", "")),
            "member": None, "reviewer": doc.get("reviewer"), "reason": doc.get("disable_reason", "")}
    else:
        return
    for member, contribution in doc.get("contributions", {}).items():
        status = contribution.get("status")
        if doc.get("status") not in ("open", "completed") or parent.get("status") in ("disabled", "withdrawn", "rejected"):
            status = "closed"
        elif doc.get("status") == "completed" and status == "pending":
            status = "closed"
        yield member, contribution, {"type": "Group quest contribution", "title": parent.get("title", "Quest"),
            "status": status, "pending": status == "pending", "description": contribution.get("description", ""),
            "role": contribution.get("role"), "member": member, "reviewer": contribution.get("reviewer"),
            "reason": contribution.get("reason") or doc.get("disable_reason") or parent.get("disable_reason", "")}


def fingerprint(facts, destination=None):
    return hashlib.sha256(json.dumps({"channel": channel_id() if destination is None else destination, **facts}, sort_keys=True, default=str).encode()).hexdigest()


def prepare(store, collection, doc, previous):
    """Called by owned-store puts inside their transaction; never calls Slack or AI."""
    old = {path: (owner, facts) for path, owner, facts in activities(store, collection, previous or {})}
    for path, owner, facts in activities(store, collection, doc):
        prior, before = old.get(path, ({}, {}))
        if previous is None:
            # New revisions must not inherit another activity's Slack address.
            for field in FIELDS:
                owner.pop(field, None)
        else:
            # Address/job metadata survives reconstructed records. Ephemeral
            # locks and post reservations must be removable after delivery.
            for field in FIELDS[:-2]:
                if field not in owner and field in prior:
                    owner[field] = prior[field]
        if not (facts["pending"] or before.get("pending") or owner.get("review_notice_job_id") or owner.get("review_message_ts")):
            continue
        current = fingerprint(facts)
        if owner.get("review_message_fingerprint") == current and owner.get("review_channel_id") == channel_id():
            continue
        job = store.get("ledger_outbox", owner.get("review_notice_job_id")) if owner.get("review_notice_job_id") else None
        if owner.get("review_notice_fingerprint") == current and job and job["status"] in ("pending", "working"):
            continue
        key = ("quest-review-notice:" + doc["generation_id"].removeprefix("quest-generation:")
               if previous is None and collection == "ledger_quests" and doc.get("generation_id")
               else "review-notice:" + str(uuid4()))
        owner.update(review_notice_job_id=key, review_notice_fingerprint=current)
        enqueue(store, "ledger_outbox", key, "review_notice", {"collection": collection, "activity_id": doc["_id"], "contributor": path})


def locate(store, payload):
    collection = payload["collection"]
    if collection not in COLLECTIONS:
        raise ValueError("Invalid review activity collection.")
    doc = store.get(collection, payload["activity_id"])
    if doc:
        for path, owner, facts in activities(store, collection, doc):
            if path == payload.get("contributor"):
                return doc, owner, facts
    return None


def reconcile(store):
    """Backfill pending activities and retry terminal failures after configuration changes."""
    if not channel_id():
        return
    for collection in COLLECTIONS:
        for doc in store.select(collection):
            if not list(activities(store, collection, doc)):
                continue
            def refresh(s):
                current = s.get(collection, doc["_id"])
                if current:
                    s.put(collection, current)
            store.atomic(refresh)


def render(payload, facts):
    state = "Awaiting review" if facts["pending"] else "Review closed: " + str(facts["status"])
    lines = ["*The Ledger · " + state + "*", "*" + escape(facts["type"] + ": " + str(facts["title"])[:100]) + "*",
             "Activity ID: `" + escape(payload["activity_id"]) + "`"]
    for key, label in (("member", "Member"), ("role", "Discipline"), ("reviewer", "Reviewer"), ("superseded_by", "Reviewed revision")):
        if facts.get(key):
            value = "The Ledger" if key == "member" and facts.get("generated_quest") else str(facts[key])
            lines.append(("Author" if key == "member" and facts.get("generated_quest") else label) + ": " + escape(value))
    for key, label in (("description", "Evidence"), ("criteria", "Criteria"), ("reason", "Reason")):
        if facts.get(key):
            lines.append("*" + label + "*\n" + escape(str(facts[key])[:2000]))
    if facts.get("learners"):
        lines.append(f"Learner acknowledgments: {facts['acknowledged']}/{facts['learners']}")
    if facts.get("target_rank") is not None:
        lines.append("Target rank slot: " + escape(str(facts['target_rank'])))
    if facts.get("generated_quest"):
        lines.append("Quest type: " + escape(facts["quest_type"]))
        lines.extend(escape(d["name"] + ": " + d["expectation"]) for d in facts["disciplines"])
        lines.append("Shop/tool prerequisites: " + escape(", ".join(facts["shop_ids"] + facts["tool_ids"]) or "None"))
    if facts["pending"] and facts["type"] == "Shared project completion":
        lines.append("Review shared outcome: `/ledger-admin complete-quest " + escape(str(facts['quest_revision'])) + "`")
    # Split into valid Slack sections rather than truncating the review evidence.
    blocks = [section(line[i:i + 2900]) for line in lines for i in range(0, len(line), 2900)]
    if facts["pending"] and facts.get("generated_quest"):
        blocks.append({"type": "actions", "elements": [button("Review quest", "review_ledger_quest", payload["activity_id"])]})
    return "\n".join(lines), blocks


def deliver(worker, job):
    destination = channel_id() or job["payload"].get("channel", "")
    payload = job["payload"]
    if job["kind"] == "quest_review_notice":
        # Older generated-quest jobs share the same address/lease and renderer;
        # either job may arrive first without posting the proposal twice.
        destination = destination or payload["channel"]
        payload = {"collection": "ledger_quests", "activity_id": payload["quest_id"], "contributor": None}
    if not destination:
        raise Denied("The review channel is not configured.")
    token = job["lease"]
    def reserve(s):
        live = s.get("ledger_outbox", job["_id"])
        if not live or live.get("lease") != token or live["status"] != "working":
            raise Denied("Review delivery was cancelled.")
        item = locate(s, payload)
        if not item:
            return None
        doc, owner, facts = item
        current = fingerprint(facts, destination)
        if owner.get("review_message_fingerprint") == current and owner.get("review_channel_id") == destination:
            return None
        lock = owner.get("review_delivery_lock", {})
        if lock.get("token") != token and lock.get("until", now()) > now():
            raise ReviewDeliveryBusy()
        owner["review_delivery_lock"] = {"token": token, "until": now() + timedelta(seconds=120)}
        owner.setdefault("review_post_token", str(uuid4()))
        s.put(payload["collection"], doc)
        return owner, facts, current
    reserved = worker.store.atomic(reserve)
    if not reserved:
        return
    owner, facts, current = reserved
    try:
        info = worker.slack.conversations_info(channel=destination)["channel"]
        game_channels = {c["channel_id"] for c in worker.store.select("ledger_channels", {"kind": "channel"})}
        if (info.get("is_private") is not True or info.get("is_ext_shared") or info.get("is_archived") or info.get("is_member") is not True
                or destination in game_channels):
            raise ValueError("Use a private, non-external review channel containing The Ledger.")
        worker.assert_live_job(job)
        text, blocks = render(payload, facts)
        ts = owner.get("review_message_ts") if owner.get("review_channel_id") == destination else None
        if ts:
            try:
                response = worker.slack.chat_update(channel=destination, ts=ts, text=text, blocks=blocks)
            except SlackApiError as error:
                if error.response.get("error") != "message_not_found":
                    raise
                ts = None
        if not ts:
            # Stable across uncertain retries; a deleted original gets a distinct key.
            client_id = str(uuid5(NAMESPACE_URL, destination + ":" + payload["activity_id"] + ":" + str(payload.get("contributor"))
                                 + ":" + owner.get("review_message_ts", "") + ":" + owner["review_post_token"]))
            response = worker.slack.chat_postMessage(channel=destination, text=text, blocks=blocks, client_msg_id=client_id)
        posted_ts = response.get("ts")
        if not isinstance(posted_ts, str) or not posted_ts:
            raise ValueError("Slack did not confirm a review message timestamp.")
        def receipt(s):
            item = locate(s, payload)
            if item:
                latest_doc, latest, _ = item
                if latest.get("review_delivery_lock", {}).get("token") == token:
                    latest.update(review_message_ts=posted_ts, review_channel_id=destination, review_message_fingerprint=current)
                    latest.pop("review_post_token", None)
                    s.put(payload["collection"], latest_doc)
        worker.store.atomic(receipt)
    finally:
        def release(s):
            item = locate(s, payload)
            if item:
                doc, latest, _ = item
                if latest.get("review_delivery_lock", {}).get("token") == token:
                    latest.pop("review_delivery_lock", None)
                    s.put(payload["collection"], doc)
        worker.store.atomic(release)
