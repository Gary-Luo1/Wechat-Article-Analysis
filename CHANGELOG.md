# Changelog

## 1.0.0 - 2026-08-23

### Added

- Review an exact public WeChat article URL supplied by the user.
- Extract bounded article content for five-dimension Agent scoring.
- Keep a local queue with URL identity, content fingerprints, inbox state,
  export, and cleanup controls.
- Optionally sync an explicitly confirmed article review to one exact Feishu
  Base table.
- Support existing-target binding and user-authorized Base creation without
  exposing resource tokens.
- Provide installers and adapters for supported Agent runtimes on POSIX and
  Windows.

### Security

- Treat article content and metadata as untrusted data.
- Restrict article requests and redirects to `mp.weixin.qq.com/s`, with response
  and extracted-text limits.
- Require separate current-task authorization before every Feishu article
  write.
- Require Python 3.10+ and patched `curl_cffi`, `requests`, and `urllib3`
  dependency lines.
