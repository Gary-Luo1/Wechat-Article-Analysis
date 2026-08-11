# Differential Security Review

## Executive Summary

| Severity | Open | Fixed in review |
|---|---:|---:|
| P0 | 0 | 0 |
| P1 | 0 | 1 |
| P2 | 0 | 2 |
| P3 | 0 | 0 |

**Overall risk:** High-risk permission and external-write change, reduced to no
known open P0-P2 findings after remediation.

**Recommendation:** Conditional approval after the final full regression and
independent review. Real Feishu creation, permission grant, write, and readback
remain outside this local review.

## Scope and Baseline

- Baseline: `41e62a7b364ae4026f11b2f399dcfd965eb3b8e7`
- Worktree at intake: 42 changed/untracked paths, approximately 1,003 additions
  and 615 deletions.
- Strategy: focused review of all high-risk identity, permission, external-call,
  configuration, runtime, installer, and packaging changes; surface review of
  documentation and tests.
- High-risk entry points:
  - `manage feishu-host-context`
  - `manage feishu-manager-access`
  - `manage feishu-create-base`
  - `process done --feishu`
  - `process sync-feishu`

## Security Invariants

1. Article text, chat text, and manually supplied identifiers are untrusted.
2. The portable runtime must never create a Bot-owned Base or grant manager
   access because it cannot authenticate host-event sender identity.
3. Management-access consent must be separate from identity detection and must
   apply only to the exact Base/table approved by the user.
4. A real article write must stay behind a single-article confirmation boundary.
5. Target, identity, App, sender, or permission-scope changes invalidate stale
   execution approvals.
6. Resource tokens and credentials must not appear in public command output,
   logs, release archives, or subprocess errors.

## Findings and Remediation

### [P1][Fixed] Host-context sender could be forged through CLI input

**Attack scenario:** a prompt-controlled process set a supported Agent environment
signal, supplied an arbitrary App ID and sender Open ID through stdin, approved
matching names, and reached the Bot resource grant path.

**Remediation:** portable Bot Base creation and the manager-grant helper are
removed. New Base creation requires user identity and the exact scoped approval.
Bot identity is limited to an existing Base selected by the user, so forged host
context cannot create a resource or assign `full_access`; sender Open ID is not
persisted as manager state.

### [P1][Fixed] Management approval was not scoped to the target names

**Attack scenario:**

1. Import a trusted Agent sender.
2. Approve management access while discussing Base A/table A.
3. Invoke Base creation with Base B/table B.
4. The old implementation checked only `manager_access=approved`, so the sender
   could receive `full_access` to a different resource than the one approved.

**Remediation:**

- Configuration v12 stores `manager_access_base_name` and
  `manager_access_table_name`.
- `feishu-manager-access --mode approve` requires both exact names.
- Bot Base creation rejects a name mismatch before any create or grant call.
- Destination, identity, App, or trusted-context changes clear the approval.

**Regression evidence:**

- `test_manager_access_approval_is_scoped_to_exact_base_and_table`
- `test_identity_change_clears_stale_agent_manager_context`

### [P2][Fixed] Identity changes retained stale Agent manager context

Changing from Bot to User identity could retain an earlier Agent binding and
sender. Identity changes now clear Agent binding, source, App/profile, manager,
and manager-access scope, requiring a new trusted host-context import.

### [P2][Fixed] Version 11 unscoped approval needed a safe migration

Version 11 could contain `manager_access=approved` without target names. Version
12 migrates that state to `undecided` instead of reusing an unsafe approval or
making the complete configuration unreadable.

## Previously Remediated Paths Rechecked

- Non-dry-run `sync-feishu --all` fails before queue iteration or external calls.
- Actual retries use `sync-feishu --link` and preserve failed items as pending.
- The public `feishu-manager --open-id` command is removed.
- Bot Base creation and manager grants are disabled. Host context may bind a Bot
  only to an existing Base selected by the user.
- Existing Base URL input is bounded, HTTPS-only, host-restricted, table-specific,
  read from stdin, and redacted from output.
- Three project adapters use the Link Reviewer product description, enforced by
  release validation.

## Blast Radius

| Function/state | Direct callers | Risk | Notes |
|---|---:|---|---|
| `_feishu_create_base` | 1 CLI dispatch | High | Creates a user-owned Base, persists recovery anchor, runs preflight |
| `_import_feishu_host_context` | 1 CLI dispatch | High | Validates transient sender input and establishes Bot App binding |
| `_feishu_manager_access` | 1 CLI dispatch | High | Persists permission consent |
| `_sync_entry` | 3 processing paths | High | Performs external record upsert |
| `validate_config` / migration | repository-wide config reads | Medium | Configuration v12 compatibility boundary |

## Verification and Coverage

Focused adversarial checks cover:

- missing/mismatched host runtime source;
- non-Agent Bot creation and grant attempts;
- undecided and declined manager access;
- exact target-name permission scope;
- destination and identity state invalidation;
- v11 approval migration;
- non-dry-run bulk-sync rejection;
- single-link retry, low-score force, and failure retention;
- adapter and release-package consistency.

Final full-suite and release results are recorded in the task handoff rather than
frozen here, so this report does not overstate evidence if later code changes.

## Trust Boundary and Limitations

- Agent runtime signals prevent accidental standalone host-context promotion but
  do not authenticate sender identity. No permission grant relies on them.
- No real WeChat account or Feishu tenant operation was authorized or executed.
- Mocked provider tests do not prove real App scopes, Base creation, permission
  propagation, field discovery, upsert, or readback.
- Two environment-dependent tests may remain skipped; their exact status must be
  reported from the final test run.
