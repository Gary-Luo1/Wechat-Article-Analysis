# Guided Feishu setup and sync

Ask before article fetching whether this task needs Feishu writing, which exact
Base/table to use, and whether the user wants management access. Configure a
target only after those choices; never request WeChat credentials, Base tokens,
App secrets, or manually supplied Open IDs in chat.

Use the `manage` commands to select an existing Base or create one, establish the
required identity, and verify the target. For an existing target, bind only the
Base/table explicitly identified by the user; never choose a default or first
listed table. In a Feishu-hosted conversation, the Agent imports `source`,
`app_id`, and `sender_open_id` from the trusted current event. It selects the
local CLI profile by that exact App ID, never by default profile or display name.
Host-context import fails closed unless the process detects the same supported
Agent source. These runtime signals protect against accidental standalone use;
they are not an authentication boundary against a local operator who can modify
the process environment or application state.
For a new Base, use the user's Feishu identity after exact-name management-access
approval. Portable Bot creation and manager grants are disabled; Bot identity is
limited to an existing Base selected by the user. The wrapper isolates its `lark-cli` state from the user's global profile
and must not print secrets, access tokens, authorization codes, or resource
tokens.

```text
manage feishu-destination --mode existing|create|skip
manage feishu-target --url-stdin
manage feishu-identity --as user
manage feishu-manager-access --mode approve --base-name <BASE_NAME> --table-name <TABLE_NAME>
manage feishu-context --verify
manage feishu-create-base --name <BASE_NAME> --table-name <TABLE_NAME> --yes
process feishu-check --save-mapping
```

For an existing Base only, `manage feishu-host-context --agent-stdin` can bind a
Bot from the current host event but never authorizes a
resource grant. The sender Open ID is validated as host input but is not persisted.
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
