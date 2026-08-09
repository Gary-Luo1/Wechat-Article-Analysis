# Optional Feishu sync

Feishu is outside the article-review path. Configure a target only when the user
asks to write reviewed articles to a Base; never request WeChat credentials.

Use the `manage` commands to select an existing Base or create one, establish the
required identity, and verify the target. The wrapper isolates its `lark-cli`
state from the user's global profile and must not print secrets, access tokens,
or authorization codes.

```text
manage feishu-destination --mode existing|create|skip
manage feishu-identity --as user|bot
manage feishu-context --verify
process feishu-check --save-mapping
```

Before a first write, run `process feishu-check --save-mapping`; it verifies the
CLI, identity, permissions, and the actual Base fields. The mapping must contain
resolvable title and URL fields. Never create missing fields silently.

Every write is explicit:

```text
process done --link <WECHAT_URL> --dims-file <SCORES.json> --feishu
process sync-feishu --all --dry-run
```

`--force-feishu` requires `--feishu` and applies to one article only. Failed
writes remain in the local outbox. URL-based upsert prevents duplicates; a retry
does not mark an entry synced until the target confirms success.
