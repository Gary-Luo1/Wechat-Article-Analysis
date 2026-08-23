# Guided Feishu setup and sync

Ask before article fetching whether this task needs Feishu writing and which
exact Base/table to use. Ask management access only when creating a new Base.
Configure a target only after those choices; never request WeChat credentials,
Base tokens, App secrets, or manually supplied Open IDs in chat.

Use the `manage` commands to select an existing Base or create one, establish the
required identity, and verify the target. For an existing target, bind only the
Base/table explicitly identified by the user; never choose a default or first
listed table. In a Feishu-hosted conversation, the Agent imports `source`,
`app_id`, and `sender_open_id` from the trusted current event. It selects the
local CLI profile by that exact App ID, never by default profile or display name.
Host-context import fails closed unless the process detects the same supported
Agent source (`openclaw`, `hermes`, `lark-channel` plus matching environment
signals). These runtime signals protect against accidental standalone use;
they are not an authentication boundary against a local operator who can modify
the process environment or application state.
On Cursor and other standalone hosts, use user identity only. Bot write is
limited to those supported hosts with trusted current-event context, and only
against an existing Base. For a new Base, use the user's Feishu identity after
exact-name management-access approval. Portable Bot creation and manager grants
are disabled. The wrapper isolates its `lark-cli` state from the user's global
profile and must not print secrets, access tokens, authorization codes, device
codes, or resource tokens. It may return an openable Base document URL after
create, check, or a successful write.

`manage feishu-auth start` actually runs isolated `auth login --no-wait`, stores
the device code locally, and returns `verification_url`. Resume that URL if
authorization is already `waiting`. After the user authorizes, `feishu-auth
complete` finishes with the stored device code. Do not tell the user to run a
raw `lark auth login` command.

First-time standalone create path:

```text
manage feishu-destination --mode create
manage feishu-identity --as user
manage feishu-app --app-id <APP_ID>
manage feishu-local-profile import --yes
manage feishu-auth start
manage feishu-auth complete
manage feishu-manager-access --mode approve --base-name <BASE_NAME> --table-name <TABLE_NAME>
manage feishu-create-base --name <BASE_NAME> --table-name <TABLE_NAME> --yes
process feishu-check --save-mapping
```

If `provisioning=created` and tokens already exist, `feishu-create-base` resumes
that Base and returns its `document_url`; do not create another Base with the
same names. Extra empty Bases created by earlier failed retries cannot be listed
or deleted without extra Drive scopes; ignore them and keep the stored URL.

```text
manage feishu-destination --mode existing|create|skip
manage feishu-target --url-stdin
manage feishu-identity --as user
manage feishu-app --app-id <APP_ID>
manage feishu-local-profile scan
manage feishu-local-profile import
manage feishu-local-profile import --yes
manage feishu-auth status
manage feishu-auth start
manage feishu-auth complete
manage feishu-manager-access --mode approve --base-name <BASE_NAME> --table-name <TABLE_NAME>
manage feishu-context --verify
manage feishu-create-base --name <BASE_NAME> --table-name <TABLE_NAME> --yes
process feishu-check --save-mapping
```

Follow the JSON `next_action` returned by each setup command. App selection,
Skill-owned profile initialization, and user authorization must be completed
when requested before Base creation or the first user-identity write. Profile
import and Base creation provide a preview before the `--yes` form applies the
change. Install a compatible `lark-cli` separately when the runtime reports
`LARK_MISSING_CLI`; the Skill does not install it automatically.

For an existing Base only, supported `openclaw`, `hermes`, and `lark-channel`
hosts may use `manage feishu-host-context --agent-stdin` to bind a Bot from the
current host event; this never authorizes a resource grant. Other hosts use the
standalone user-identity flow. The sender Open ID is validated as host input but
is not persisted. On supported hosts where trusted stdin forwarding is unavailable, use
`manage feishu-host-context --agent-file <TRUSTED_CONTEXT.json>` with a
host-generated current-event file; never ask the user to compose that file.
The separate manager-access command records the user's choice
for user-identity Base creation. Approval is scoped to the exact Base/table names and is cleared when the
destination, identity, or App binding changes.

Before a first write, run `process feishu-check --save-mapping`; it verifies the
CLI, identity, permissions, and the actual Base fields. The mapping must contain
resolvable title and URL fields. Never create missing fields silently.

Every write is explicit:

```text
process done --link <WECHAT_URL> --dims-file <SCORES.json> --feishu
process sync-feishu --link <WECHAT_URL>
process sync-feishu --all --dry-run
```

The `--all` form is preview-only. Actual retries use `--link` one article at a
time; a non-dry-run bulk request is rejected.

Use `done --feishu` while the review is pending. Use `sync-feishu --link` when a
processed review was previously kept local, skipped by the threshold, or left in
the retry outbox. `--force-feishu` applies only to one explicitly confirmed
article. Failed writes remain in the local outbox. URL-based upsert prevents
duplicates; a retry does not mark an entry synced until the target confirms
success.

The runtime exposes only `process` and `manage`. Raw lark data commands and a
standalone resource-grant command are intentionally unavailable; Base creation
runs only under user identity and performs no separate manager grant.
