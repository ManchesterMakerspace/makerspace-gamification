# Progress, reviewed quests, and The System

## Member interfaces

`/ledger stats` opens your character sheet with authoritative rank, XP, deepest cleared skill, shop milestones, mentoring, volunteer contributions, and achievements. **Next rank**, **Browse quests**, **Skill tree**, **Achievements**, and **Preferences** match the Home controls. `/ledger progress` shows cumulative requirements from your pinned rules, deficits, remaining XP, and up to three optional next steps. Pending history imports, staff holds, membership eligibility, and the highest configured rank are shown privately. The Ledger explains facts; it cannot promise promotion.

Ask about your progress in a DM, mention, active Ledger thread, or a clear self-directed question in a registered Ledger channel. Replies stay in context and include a private detail button. Membership eligibility details stay private.

`/ledger-quests` or `/ledger-quests list` opens a searchable title dropdown. Details show readable description, observable criteria, exact target rank, prerequisites, approved reward, creator attribution, verification, and actions. Existing learning challenges and cooperative quests remain available through the same browser. `/ledger-skills` remains the complete skill tree.

At rank slot 3, `/ledger-quests create` unlocks. Author quests for enabled slots at least two below your own. **Help draft with The Ledger** produces editable suggestions in the background; you explicitly submit. The System cannot publish a quest. Publication requires an independent authorized reviewer, who sets a whole reward from 0–500 XP. Publication earns nothing. Default classification is a challenge; specialized catalog milestones keep their existing evidence requirements and do not add duplicate milestone XP.

Acceptance requires your exact target rank and current prerequisites. Your accepted revision, rank, and reward are preserved after rank-up. Each logical quest pays once per member across revisions. Authors cannot accept, contribute to, complete, publish, or verify their own quests. Each independently verified completion notifies the author once when still eligible for delivery. `/ledger-quests edit <revision>` submits a fresh immutable revision for review.

Rank corrections, suspension, revocation, or invalid identity can disable authored quests and block outstanding awards. Completed history remains. Restoration requires a new reviewed revision; disabled accepted revisions remain unavailable. Author opt-out retains reviewed published quests but stops authoring and game notifications.

## Preferences and recognition

Preferences independently control audit-only observation and arrival mentions. You can disable either without leaving Ledger. Opt-in covers explained observation, and a notice must be delivered before it activates. The System observes only new eligible messages in registered Ledger channels, kudos issuance metadata, and verified volunteer activity. It excludes DMs, original kudos text, unrelated channels, bots, historical imports, and replays.

Observation suggestions are always audit-only, including when `LEDGER_OBSERVATION_AUDIT_ONLY=false`. Python records the proposed category and XP as `proposed_delta`, with applied `delta: 0`. Suggestions do not change XP or rank, consume budgets, send warnings or recognition messages, or publish achievements. A separate authorized deterministic or human decision is required before changing accounting; this runtime has no automatic proposal approval path. Regular earned XP and independently reviewed quest rewards continue normally.

Existing finalized discretionary records, historical budget consumption and authorized append-only staff corrections are retained. Audit validation still bounds suggestions and requires evidence/consent, including delivered-warning causality for repeat-imitation proposals. Ordinary gratitude or similar wording is insufficient.

Arrival mentions are optional. A fresh canonical check-in has a reserved 20% default chance of a greeting, with a durable ten-day cooldown across rank changes. Greetings use only your current available rank channel and respect voluntary departures. The System receives no card identifier. An uncertain Slack send retains the cooldown.

## Operator review and authority

`/ledger-admin delegates` lets current staff select one opted-in eligible member, explicit capabilities, scope, and a required reason. Admins/board members can grant global, explicit-shop, or quest scopes. Resource managers can grant only within their current assigned shops. All relevant shops must be covered; unscoped evidence requires global authority unless a specific quest grant applies. No self-grants or onward delegation.

Capabilities are `quest_publish`, `quest_complete`, `learning_review`, and `mentoring_review`. Rank and descriptive titles grant nothing. `/ledger-admin review` filters the queue by actual authorization. `/ledger-admin publish-quest <revision>` and `reject-quest <revision>` open publication/reward forms. `approve <evidence-id>` and `reject <evidence-id> <reason>` review pending evidence and quest completions. Existing `verify-quest <quest> @member` verifies cooperative contributions; contributors cannot verify their group. Authors can `/ledger-quests withdraw <revision>`; staff with the complete scope can `/ledger-admin disable-quest <revision> <reason>`. Admins/board members can `/ledger-admin correct-ai <decision-id> <signed-XP-delta> <reason>` for an independent append-only correction linked to the original decision, without replenishing budgets.

Delegates cannot configure ranks/prompts, attest membership, grant tool clearance, issue arbitrary XP, reverse finalized approvals, or delegate. Python rechecks independence, current participation/membership/identity, grant capability/scope/version, and grantor authority immediately before a review commit. Audits retain grant ID/version. Explicit revocation writes the same grant document as review use, serializing with approval. Opt-out revokes all received grants in the consent transaction. Source revocation events and reconciliation permanently revoke grants; live checks deny them before cleanup. Restoring membership or rejoining never revives grants. Legitimate past approvals remain; staff corrections are separate.

## Technical implementation and rollout

All application-authored member-facing text uses **The Ledger** or **The System**. Implementation model names remain confined to technical configuration and operator documentation. Member-authored kudos and other supplied content are preserved.

No new collections: grants/acceptances use `ledger_relationships`; immutable quests use `ledger_quests`; observations, decisions, daily budgets, warnings, audits, and cooldowns use `ledger_evidence`; awards use `ledger_awards`; jobs and context keep their existing collections. `_id` serializes daily workspace/member budgets and cooldown reservations. Additional indexes cover quest targets/creators, grant delegate/grantor/scope/status, acceptances, and evidence review/day queries.

Source reads use the official PyMongo client through `MLAB_URI`; owned writes use `LEDGER_URI`. Query tools expose only `shops`, `tools`, `tool_checkouts`, `volunteer_tasks`, and `volunteer_events`, fixed fields, escaped literal search, caller-only clearances, mandatory enabled-parent filters, Rails claimable task statuses, and New York date-only event/cooldown boundaries. Internal `checkins`/`cards` reads are exclusively for canonical arrival identity. MQTT subscribes explicitly to `checkins/insert` independently of query permissions, and excludes retained packets. Review [exact Mongo roles](MONGODB_ACCESS.md) before rollout.

The narration transport rejects tool calls. The separate conversation transport validates every tool request and correlates results with `tool_call_id`, at most three calls in thirty seconds. Query reads use a combined two-second server-query budget; catalog joins fail closed above 1,000 enabled shops/tools. Tool unavailable results are distinct from empty results. [Slack external selects](https://docs.slack.dev/reference/block-kit/block-elements/select-menu-element/), [hash-protected view updates](https://docs.slack.dev/reference/methods/views.update/), [function calling](https://developers.openai.com/api/docs/guides/function-calling), and [PyMongo projections](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/query/project/) document the transport contracts.

Switches are independent:

| Environment setting | Default | Effect |
| --- | --- | --- |
| `LEDGER_OBSERVATION` | `false` | Capture/evaluate new eligible observations after notices |
| `LEDGER_OBSERVATION_AUDIT_ONLY` | `true` | Compatibility setting; proposals remain audit-only even when false |
| `LEDGER_DEDUCTIONS` | `false` | Permit validation of repeat-imitation audit suggestions; cannot apply deductions |
| `LEDGER_NOVEL_ANNOUNCEMENTS` | `false` | Legacy finalized-notification delivery gate; new proposals never publish |
| `LEDGER_WELCOMES` | `false` | Permit reserved arrival greetings |
| `LEDGER_WELCOME_PROBABILITY` | `0.2` | Once-per-arrival selection probability |

Deploy interfaces, review/delegation, and prompts first with optional features disabled. Run `ledger init` to add indexes and update the least-privilege source role. Verify real Mongo permissions, Slack external-select options, modal hashes, approval/revocation conflicts on the replica set, and bot membership. Enable observation to inspect audit proposals/rejections. Automatic recognition, deductions and proposal announcements cannot be enabled by flags. Configure welcomes independently. Cancelled/failed explanatory notices are requeued for the current active consent generation after re-enablement; their deterministic ID is retained, retry attempts reset and the old lease removed. Pending/working/done notices and opted-out/older generations are not reopened.

`ledger-engagement` consumes only engagement inbox jobs. Accounting excludes those jobs; channel cleanup has its own worker. Accounting, delegation revocation, opt-out, and channel removal do not depend on inference or Google Docs. `/ledger-admin metrics` exposes backlog, conversation latency, engagement outcomes, budget rejections, deductions, delegation revocations/failures, arrival outcomes, and delivery statuses without bodies or private source records.

Compose adds `--enable-auto-tool-choice --tool-call-parser qwen3_coder` and preserves non-thinking generation. The [configured NVIDIA checkpoint](https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4) recommends this parser. **Deployed-image compatibility remains a live verification step:** check the image's parser support, startup, authenticated tool round-trip and normal narration before enabling observation. No live model, Slack, Mongo, or Google Doc verification is implied by local tests.

The matrix is policy version 6/schema 1, with required `quest_author`, `ai_observer`, and `delegated_reviewer` entries. Expanded role coverage raises the bounded matrix export limit to 24 KiB. An existing Google Doc override requires the same complete policy update and an explicit `/ledger-admin reload-prompts`; implementation never edits/publishes it externally. Existing reserved deliveries retain their saved policy/prompt/text. The 23 JSON prompt sets stay separate; the five requested completion types now have five paired variations/version 2. `delivery.json` version 2 varies sender acknowledgments/receipts using recipient mentions and recorded destination outcomes.

Local validation uses `PYTHONDONTWRITEBYTECODE=1` with both prompt validators and the full pytest suite. The original planning baseline was 167 passed/one Mongo integration skipped; consult the current run for the expanded suite. Replica-set concurrency is additionally covered by opt-in Mongo integration tests when a test endpoint is supplied.

## Chat and peer thanks without joining

You may send and receive `/kudos` and chat with The Ledger without opting in. Pick an optional emoji in the sending modal; it accompanies the recipient DM and optional public post. Your original text stays unchanged. Joining controls kudos XP and game access, with the same repeat-giving limits.

Before joining, The Ledger answers general questions about itself and XP and may suggest `/ledger join` to see behind the curtain or start your journey. Specific ranks, quests and rules become available after joining. Participant conversations cover current/lower rank names and details and what remains for the next promotion, without revealing other higher-rank details or quests beyond your rank. In joined channels, address The Ledger by name or mention, continue one of its threads, or ask a relevant question containing `?`. Shop/tool answers use available catalog facts, knowledge and relevant conversation history; unknown information receives "I don't know." Chat without opting in does not enable observation.
