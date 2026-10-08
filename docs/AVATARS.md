# Participant avatars

Personalized fantasy avatars default on for active participants. `/ledger
preferences` offers an opt-out, one owned JPEG/PNG/WebP reference strictly below
2,000,000 bytes, and reference removal. Opt-out restores the rank image and
cancels generation/notices, tombstones the current pair, and deletes its bot-owned
files after the default Home is confirmed. Preferences survive leaving/rejoining. An uploaded
reference replaces the Slack photo; its original user-owned file is retained.
The first confirmed avatar DM explains the opt-out location.

## Generation and privacy

Joining, promotion, downward rank correction, newly recorded shop completion, and independently verified
individual/cooperative/catalog quest completions reserve jobs in the existing
Mongo transactions. Duplicate events do not reserve duplicate jobs. Pending
requests coalesce for sixty seconds; milestones during generation reserve a
follow-up. Join jobs wait for history import. The avatar worker reserves a New
York calendar-day backfill on startup and checks for a new day at most once every
five minutes while its loop is available. Reservation checks are throttled even
after database failures, in both avatar-only and combined workers. Backfill
pages at most 100 participants, skips active jobs/opt-outs, and repairs missing,
deleted or wrong-rank avatars.

Context contains rank, saved objective image description, configured Slack
bio/interests, up to forty tool names, ten active accepted and ten recent
completed quests, and twelve own retained messages of at most 600 characters.
Observed posts require the current original text, configured channel, observation
notice and generation. Observation opt-out excludes channel posts; own retained
messages to The Ledger may still personalize enabled avatars. Other authors,
bot replies, expired/deleted/prior-consent messages and sensitive credential,
billing, access-code or internal-note text are excluded. The deployed narrator
tokenizer fits the prompt. Successful composition removes raw chat and
composition messages from the job, retaining the resulting visual prompt.

Dominant shop means the most distinct currently non-revoked, available tool
clearances, with latest checkout then shop ID as tie-breakers. These are checkout
clearance records, not equipment-use frequency. Models grant no XP, safety
clearance, staff role or accounting authority.

Four reference slots are identity (custom reference or Slack photo), prior
character continuity, bundled current-rank style, and shop. Missing optional
images are omitted. Unavailable custom references never substitute the profile
photo. References are normalized to bounded PNGs. The default shop reference is a
local neutral workshop drawing; operators can mount `<shop-id>.png` assets in
`LEDGER_AVATAR_SHOP_ASSET_DIR` on the avatar worker.

The runtime generates one 1280-square PNG, forty diffusion steps, true CFG 1.0,
and a reserved seed. Pillow normalizes orientation, flattens transparency,
strips metadata and exports RGB JPGs at exactly 1254-square and 512-square,
quality 95. Both confirmed Slack uploads must exist before atomic pair
activation. Failure preserves the old pair. Files are shared only with the
participant's Ledger DM. Slack may show upload shares before the final composed
notification; that notification follows activation and displays the 512 JPG
with a link to the full-resolution file.
Confirmed upload receipts survive an opt-out during upload and reserve cleanup.
Cleanup waits for the generation to finish or cancel, and protects current files.
Failed candidates that never activated bypass Home replacement confirmation;
activated candidates still require it, including their upload-receipt cleanup.
Activated jobs never regenerate when their original worker lease is reclaimed.

Home and character-sheet rendering resolve the current avatar from Mongo. Home
snapshots include avatar revision; normal Home opens detect stale revisions.
Missing, wrong-rank or opted-out avatars use the rank artwork. After confirmed
Home replacement, retryable cleanup deletes superseded bot-generated files and
temporary generation artifacts, never the current pair or user-owned reference.
Old DM links disappear after obsolete files are deleted.
Runtime receipt deletion uses a separate durable `avatar_runtime_ack` delivery
job. It retries with capped backoff until the supervisor recovers, independently
of Slack and local-file cleanup, without consuming their retry budget. Keep the
runtime URL/key configured until pending acknowledgment jobs have drained.

## Storage and metrics

`ledger_avatars` is Ledger-owned and uses `LEDGER_URI`:

| ID prefix | Contents |
| --- | --- |
| `current:<member>` | Rank/revision, username, exact Qwen-image prompt, `avatar`/`avatar512` Slack references, sizes/checksums, token usage, seconds and UTC `job_end_time` |
| `job:<job-id>` | Frozen policy/prompt selection, sanitized context, reference checksums, model/settings/seed, upload stages and bounded attempt metrics |
| `reference:<member>` | User-owned file ID, validation checksum, objective description; removal creates a tombstone |
| `state:<member>`, `trigger:<hash>` | Sequence and deterministic milestone deduplication |
| `notice:<member>` | Confirmed latest DM receipt and first-notification timestamp |

Jobs remain in `ledger_outbox`; the global runtime lease uses `ledger_catalog`.
The role examples explicitly grant find/insert/update/createIndex on
`ledger_avatars`. No new source write or wildcard privilege is introduced.
Existing Slack scopes cover profile reads, file upload/deletion and DMs.

Each attempt prints structured JSON to STDOUT with job ID, Slack username,
outcome, duration in seconds, UTC end time and token usage, and stores the same
metrics in Mongo. Narrator usage is request-local input/output/total tokens.
Diffusion has no ordinary output-token count: provider usage, encoder counts and
diffusion metrics are separate; unavailable counts are null, never fabricated.
Attempt duration includes loading/uploads, excludes queue delay and subsequent
Home/DM delivery. Successful `job_end_time` denotes activation; terminal failed
attempts also have end times.

## Deployment and lifecycle

The optional `avatars` Compose profile adds `ledger-avatars` and `ledger-image`.
The narrator stays running. Ordinary delivery handles notices/cleanup and shares
the avatar spool volume. No Docker socket or public inference route is added.

1. Pin `VLLM_OMNI_IMAGE` to an immutable image digest verified for Qwen-Image-2.1
   on the target Linux ARM64/GB10. The example `latest` value is a discovery
   placeholder, not a production pin. The image build checks the pipeline import.
   The [official serving recipe](https://recipes.vllm.ai/Qwen/Qwen-Image-2.1)
   currently points to development support in vLLM-Omni PR #7759; select an image
   containing that implementation rather than assuming a tagged release supports
   it. Verify multiple-reference serving on the target GPU before enabling workers.
2. Set a separate `LEDGER_AVATAR_RUNTIME_KEY` of at least 24 characters. Set
   `LEDGER_AVATAR_MODEL_REVISION` to the deployed checkpoint revision. To freeze
   every component, mount a pinned local model snapshot and use its path in the
   runtime command; a serving revision flag may not reach every component loader.
3. Configure Slack custom-profile IDs in `LEDGER_AVATAR_BIO_FIELD_ID` and
   `LEDGER_AVATAR_INTERESTS_FIELD_ID`. Unset fields are omitted. Set
   `LEDGER_AVATAR_CONTEXT_LIMIT` to the narrator window (default 8192).
4. Apply the updated Mongo role and run `ledger init`. Start with
   `docker compose --profile avatars up -d --build ledger-image ledger-avatars ledger-delivery`.
5. `LEDGER_AVATAR_RUNTIME_COMMAND` optionally accepts JSON argv, never shell
   code. Default: `vllm serve Qwen/Qwen-Image-2.1 --omni --host 127.0.0.1 --port
   8091`. Overrides must preserve that loopback API address. Test any offload or
   quantization flags on the selected runtime first.

The authenticated supervisor provides `/generate`, `/status`, `/unload`, `/ack`
and `/health`. Disk receipts let a repeated request ID with identical inputs
recover its generated image after a lost response. Changed inputs with the same
ID are rejected. Its lock rejects concurrent generation/unload with HTTP 409.
GPU process logs are suppressed to keep prompts/images out of logs. Failure or
timeout terminates the entire process group; Compose init reaps descendants.

The worker renews its job/global leases every thirty seconds. Lease expiry or a
lost HTTP response never authorizes overlapping GPU work: the supervisor remains
single-flight until completion/termination. When no runnable jobs remain, the
worker requests unloading. Delayed retries do not keep the model loaded. An
independent supervisor watchdog unloads after 120 idle seconds if a worker
disconnects; SIGTERM also unloads the GPU subprocess.

For another GPU/host, run the same supervisor there and configure
`LEDGER_AVATAR_RUNTIME_URL` and matching key in the application environment.
Use a private network, HTTPS across untrusted networks, and host/Compose GPU
selection. All workers for one Ledger database must use the **same logical
supervisor**. Drain/stop the old supervisor before switching hosts; never deploy
independent simultaneous supervisors. OOM preserves the old avatar and retries;
move the image service to another GPU rather than automatically unloading the
narrator.

Prompt Matrix version 49 and the paired `avatar.json` set cover avatar
policy. XML schema/required roles remain unchanged. Google Doc overrides require
the same policy update and explicit reload; no external document is edited or
published, and existing reserved jobs retain their policy snapshot.

Run `python -m ledger.prompt_matrix`, `python -m ledger.prompt_library` and
`python -m pytest`. Deployment checks must exercise real multiple-reference
generation, narrator coexistence, sequential inference, OOM recovery, GPU memory
release, both JPG sizes, participant file access, first-DM opt-out text, Home
replacement, reference validation and deletion. Those checks need deployment
hardware and Slack credentials; automated tests do not claim to perform them.

Administrators and board members who are opted in can use `/ledger-admin avatar @member` to inspect a participant’s current avatar, saved visual prompt, reference manifest, model/settings, token usage, duration, completion time and attempt metrics. Raw conversations, policy snapshots and credentials are excluded. The force-generate/regenerate button invalidates queued or running candidates and reserves an immediate replacement on the sequential avatar queue, keeping the current pair until successful activation. Repeated clicks on the same button reserve only one replacement. Custom-avatar opt-out is honored at inspection, selection, enqueue and delivery. `/ledger-admin avatar` opens a participant picker filtered to eligible opted-in participants with personalized avatars enabled. Submission opens the selected participant’s avatar and saved generation details; only the optional force-generate/regenerate button queues generation. No rank, XP or consent is changed.
