# Slack application manifest and installation

[slack-manifest.json](../slack-manifest.json) is the importable example manifest for The Ledger's single-workspace HTTP app. Replace every `LEDGER_HOST` with the public Cloudflare Tunnel hostname, without a scheme or path (for example `ledger.example.org`). It contains no credentials. Slack's [manifest reference](https://docs.slack.dev/reference/app-manifest/) defines the JSON format.

Check `/kudos` availability before installing; slash-command names can collide with other installed apps. Create an app **from a manifest** in the intended workspace, paste the modified JSON, review permissions, and install it. Save the bot OAuth token as `SLACK_BOT_TOKEN`, the Basic Information signing secret as `SLACK_SIGNING_SECRET`, the workspace ID as `SLACK_TEAM_ID`, and the bot's Slack user ID as `SLACK_BOT_USER_ID`. The last value is a `U...` user ID, not an `A...` app ID or `B...` bot ID; the bot token's `auth.test` response provides `team_id` and `user_id`.

Start web ingress and the tunnel using [deployment](DEPLOYMENT.md), then verify the Events API Request URL in Slack. Bolt responds to Slack's challenge. Enable Events and Interactivity as represented in the manifest; leave Socket Mode disabled. If updating an installed app's scopes, reinstall/re-authorize it before testing. The service uses a supplied bot token and does not implement an OAuth installation/refresh endpoint, so automatic token rotation is disabled in this example.

## Bot icon

Upload [icons/ledger-bot-512.png](../icons/ledger-bot-512.png) in the app's **Basic Information → Display Information → App icon** settings. This is The Ledger's anthropomorphic leatherbound grimoire artwork. Slack scales the uploaded icon for its clients; [icons/ledger-bot-36.png](../icons/ledger-bot-36.png) is the supplied mobile-size preview/export. The manifest sets a matching charcoal app-profile background. Icon upload is a separate installation step; it requires no additional runtime bot permissions. The [asset notes](../icons/README.md) include the original source, final prompt, and sizing guidance.

## HTTP endpoints and commands

| Configuration | HTTPS path |
| --- | --- |
| Events API Request URL | `/slack/events` |
| Every slash command | `/slack/commands` |
| Interactivity and option loading | `/slack/interactions` |

The manifest enables App Home and writable app messages. All seven commands have `should_escape: true` to preserve structured member mentions:

| Command | Function |
| --- | --- |
| `/ledger` | Consent, progress, sponsorship, channel invitations, feedback |
| `/ledger-skills` | Shop/tool skill trees and accessible text |
| `/ledger-quests` | Learning challenges, evidence, collaborative quests |
| `/ledger-mentor` | Success Buddies and verified mentoring |
| `/kudos` | Recipient-first thanks, optional public sharing and invitation |
| `/ledger-project` | Project gallery and feedback threads |
| `/ledger-admin` | Authorized configuration, verification, metrics, pause/resume |

All modals, buttons, checkboxes, and external shop/tool/member dropdowns use the interaction endpoint. They are interaction payloads, not additional event subscriptions or commands.

## Saved opt-in and chat replies

The consent modal saves `ledger_participants.opted_in` synchronously and displays **Opt-in saved** after the transaction succeeds. Repeating `/ledger join` or clicking an old invitation while still opted in shows **Already opted in**, current rank/XP, and any pending history import. It does not reset consent, rules, progress, or invitations. Opting out and joining again still shows the consent form. Older images always reopened consent even for a saved participant; that alone did not mean consent was lost.

HTTP 200 for a chat event confirms receipt, not that a reply has been generated. The complete path is:

`ledger-web → ledger_inbox → ledger-accounting → ledger_outbox → ledger-delivery → Slack`

`ledger-channels` separately handles invitations/removals. All three workers must be running alongside the web service. If web and delivery run alone, chat events and history imports remain queued; welcome messages wait for the missing import. Current workers report `HistoryImportPending`, defer the welcome without spending delivery retries, and log `check ledger-accounting`. Older images reported only `RuntimeError` and could exhaust their retries. Already-failed jobs need the targeted retry procedure in [operations](OPERATIONS.md#recovery); do not erase consent or re-award XP.

```sh
docker compose up -d --build --no-deps ledger-web ledger-accounting ledger-delivery ledger-channels
docker compose ps --all
docker compose logs --since=5m ledger-accounting ledger-delivery ledger-channels
```

Accounting logs `Slack event processed ... outcome=reply_queued` when it queues a reply; otherwise it reports a fixed reason such as `ignored_unlinked_identity`, `ignored_unregistered_channel`, or `ignored_unaddressed_channel_message`. No message body is logged. Pending `slack_event` inbox jobs point to accounting; pending `conversation` outbox jobs point to delivery. Check workers use the same `LEDGER_URI`/`LEDGER_DATABASE` as web if queued jobs never appear to them. Check Slack errors such as `missing_scope` or `invalid_auth` in delivery logs, and reinstall the Slack app after scope changes.

Participating members get replies in DMs. In registered private Ledger channels, address the bot with `@The Ledger` or continue a thread the bot is participating in; ordinary channel chatter and unregistered channels do not prompt replies. Nonparticipants with a valid linked identity receive an opt-in invitation in response to a DM. Chat narration does not itself execute administrative commands or award XP; use the command/forms for actions. Ensure `message.im` and `app_mention` subscriptions, `message.groups` for private-channel thread replies, and the matching manifest scopes are installed. `SLACK_BOT_USER_ID` must be the bot's `U...` user ID.

## Troubleshooting "the app did not respond"

Slack needs an HTTP acknowledgment within three seconds of invoking a command ([Slack's command guide](https://docs.slack.dev/interactivity/implementing-slash-commands/)). Healthy `/health` and `/ready` responses do not verify this path: readiness checks Mongo connectivity and Ledger replica-set topology, not collection read/write authorization, callback reachability, or worker delivery.

1. In the Slack app's **Slash Commands**, check each command's Request URL is exactly `https://YOUR_HOST/slack/commands`. Event subscriptions and interactivity have their own URLs above. Leave Socket Mode disabled. Editing the repository manifest alone does not update an installed Slack app.
2. Send an **unsigned** probe through the public hostname (replace `YOUR_HOST`):

   ```sh
   curl -i --max-time 5 -X POST 'https://YOUR_HOST/slack/commands' \
     -H 'Content-Type: application/x-www-form-urlencoded' --data 'command=%2Fledger'
   ```

   Expect **401** from Bolt; this request cannot run a command. A redirect/login, challenge, 404, or gateway error indicates a routing/access issue. Cloudflared's origin is `http://ledger-web:3000`, with paths preserved; Slack must reach callback paths without an Access login or browser challenge. A 401 proves unsigned traffic reaches verification, not that Slack's signing secret is correct.
3. Reproduce one real `/ledger` invocation while watching:

   ```sh
   docker compose logs --follow --since=5m ledger-web cloudflared
   ```

   The supplied image logs method, path, HTTP status, and duration, without query strings, headers, or bodies. Callback failures and responses taking at least 2.5 seconds also produce a warning. Listener errors record exception class and numeric Mongo error code without the potentially sensitive exception text. To install these diagnostics from an older image, run `docker compose up -d --build --no-deps ledger-web` first.

   | Result for the real Slack request | Next check |
   | --- | --- |
   | No web access-log entry | Slack Request URL, Socket Mode, Cloudflare route/Access/WAF; ensure the image includes access logging |
   | 401 | `SLACK_SIGNING_SECRET` from this app's Basic Information, host clock, and unmodified request body |
   | 403 with `Workspace not allowed` | `SLACK_TEAM_ID` must match the installed workspace's `T...` ID |
   | 404 | Exact callback path; remove trailing slash or unintended path prefix |
   | 500/503 | Safe listener/ingress error record; `OperationFailure code=13` means Mongo authorization failed. Check source reads and Ledger writes using [the role examples](MONGODB_ACCESS.md), and complete initialization/bootstrap |
   | 200 taking around three seconds or more | Database lookup/transaction latency or synchronous Slack modal API calls; AI generation is outside ingress |
   | Prompt 200, but no later DM | Inspect `ledger-accounting` and `ledger-delivery` logs and the inbox/outbox; acknowledgment succeeded |

After changing `.env`, recreate the affected containers with `docker compose up -d --no-deps ledger-web ledger-accounting ledger-delivery ledger-channels`; a plain restart does not apply changed Compose environment values. Do not disable signature verification or acknowledge failed persistence to hide the error. Share only redacted diagnostic lines; keep request payloads, signing secrets, tokens, Mongo URIs, and `response_url` private.

## Event subscriptions

| Bot event | Handler purpose | Scope used |
| --- | --- | --- |
| `app_home_opened` | Publish the participant's gallery/progress Home view | No additional event scope |
| `app_mention` | Threaded responses when The Ledger is addressed | `app_mentions:read` |
| `message.im` | Onboarding/opt-out DMs and conversations; message edits/deletions | `im:history` |
| `message.groups` | Authorized private-channel conversations and context edits/deletions | `groups:history` |
| `member_joined_channel` | Check consent/rank and remove unauthorized game-channel joins | `groups:read` for private channels |
| `member_left_channel` | Record voluntary departure and respect re-invitation rules | `groups:read` for private channels |
| `user_change` | Refresh active human identity/deactivation state | `users:read` |

Slack delivers `message.im` and `message.groups` as `type: "message"`; `message_changed` and `message_deleted` are subtypes, not separate manifest entries. Channel events require the bot to belong to the channel. The worker filters channel processing to registered Ledger private channels. See Slack's [channel membership event contract](https://docs.slack.dev/reference/events/member_joined_channel/) and [App Home event contract](https://docs.slack.dev/reference/events/app_home_opened/).

## Bot permissions

| Scope | Calls or events that need it |
| --- | --- |
| `commands` | All slash commands and their interactive flows |
| `app_mentions:read` | `app_mention` event |
| `chat:write` | `chat.postMessage` for DMs, notifications, kudos, project threads, and replies |
| `groups:read` | `conversations.info`, `conversations.members`, private-channel membership events |
| `groups:history` | `message.groups`, including edits/deletions; private project thread visibility |
| `groups:write` | `conversations.create` with `is_private`, `conversations.invite`, `conversations.kick` |
| `im:history` | `message.im`, including edits/deletions |
| `im:write` | `conversations.open` for recipient DMs and file delivery |
| `users:read` | `users.info` for human/active checks and `user_change` |
| `files:write` | Skill-tree image/text and rank art via `files.getUploadURLExternal` / `files.completeUploadExternal` (`files_upload_v2`) |

`groups:write` is needed for [private-channel removal](https://docs.slack.dev/reference/methods/conversations.kick/) as well as [invitations](https://docs.slack.dev/reference/methods/conversations.invite/); invite-only permission would not cover opt-out cleanup. [External file uploads](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/) require `files:write`. `views.open`, `views.update`, `views.publish`, and `chat.getPermalink` need an authenticated bot but no extra scopes beyond this set for the supported flows.

No user-token scopes, public-channel history/management, workspace administration, email lookup, reaction write, incoming webhooks, or app-level Socket Mode token are needed. Custom emoji shortcodes render in Slack without calling `emoji.list`. Ordinary app tokens do not bypass workspace policy on creating private channels or removing members; verify those permissions with workspace administrators during the pilot.

The bot must be invited to existing private game channels before `ledger bootstrap`. Bootstrap creates new private channels if none are supplied. Test join/leave reconciliation with real members: native manual invitations can briefly expose a private channel before removal, as documented in the accepted design.
