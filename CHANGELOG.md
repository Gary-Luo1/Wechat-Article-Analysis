# Changelog

## 2.3.0 - Unreleased

### Changed

- Keep the public product contract link-only: users provide an exact public
  WeChat article URL for reading, scoring, local queueing, and optional sync.
- Release archives and installers no longer ship the legacy subscription setup
  script or a raw `lark-cli` forwarding entry.
- Use user identity plus exact-name management approval for new Base creation;
  portable Bot creation and manager grants are disabled because host-event
  sender identity is not authenticated by this runtime.
- Require `curl_cffi` at runtime so article requests do not silently fall back
  to a plain non-browser TLS fingerprint.
- Preview processed-record cleanup unless `--yes` is supplied, cap aggregate
  `batch-read` content output at 200,000 characters, preserve item retryability
  in batch errors, and mark completed Feishu review records as read.
- Keep URL identity, local queue locking, isolated CLI profile selection, field
  mapping, and Feishu URL-based upsert behavior shared across supported flows.

### Fixed

- Stop article reads immediately on WeChat risk-control pages and HTTP 403/429.
- Reject direct Feishu data commands that could bypass the managed identity,
  target, confirmation, and manager-permission checks.
