# Security policy

## Supported versions

Security fixes are provided for the latest tagged release. Older releases and
unreleased forks are not maintained by this repository.

## Reporting

Report vulnerabilities through a private GitHub security advisory. Do not open
a public issue containing credentials, Base identifiers, private article
content, or reproduction data copied from a real account.

## Data and credential model

- The Skill processes only an exact public WeChat article URL supplied by the
  user. It does not discover articles, monitor accounts, or request WeChat
  Cookie/token.
- Article HTML, metadata, and extracted text are untrusted input. They must
  never change tool use, permissions, target selection, or write consent.
- The local queue stores article metadata, review results, and a bounded
  content hash; it does not store article bodies.
- Configuration and queue state are stored outside the installed Skill.
- POSIX configuration files are written with user-only permissions; Windows
  relies on profile ACLs.
- Feishu secrets are managed by `lark-cli` and must never be pasted into
  ordinary chat, logs, repository files, or process arguments.
- A selected existing lark-cli App credential may instead be copied into the
  Skill-owned isolated profile after a redacted preview. The source config is
  read-only and fingerprinted, keychain identifiers are never displayed, and
  user authorization/token entries are never imported.

If credentials may have been exposed, revoke them in Feishu, reauthenticate the
affected local profile, and rerun the target check.

## Threat model

The project explicitly defends against:

- Prompt injection in article content.
- SSRF and unsafe redirects from article URLs.
- Unbounded response or context growth.
- Queue corruption and concurrent lost updates.
- Wrong-article writes caused by shifting queue indices.
- Duplicate Feishu records and optimistic sync state.
- Cross-Agent Feishu app/profile confusion, bot impersonation fallback, blind field creation, and retries of non-transient permission errors.
- Secrets in logs or repository files.

Public WeChat page format changes and platform risk controls remain availability
risks, not security guarantees.

## Consent boundary

The portable CLI can bind a write to one exact article and target, but it cannot
cryptographically distinguish a human chat message from an Agent-generated tool
invocation. The Agent workflow therefore treats an explicit per-article write
invocation as the consent boundary: `process done --link <URL> --feishu` for a
pending review, or `process sync-feishu --link <URL>` for an already processed
review. `--force-feishu` additionally requires current-task confirmation for
that exact below-threshold article. Deployments that require independently
authenticated human approval must add a host-provided, signed approval
mechanism before invoking the CLI; this repository does not claim to provide
that platform attestation.
