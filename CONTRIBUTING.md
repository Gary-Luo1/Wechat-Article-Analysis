# Contributing

Thank you for improving WeChat Article Link Reviewer.

## Development setup

Use a repository checkout or the GitHub source archive; the portable release
ZIP intentionally omits tests and development tools.

```bash
python -m pip install -r skills/wechat-article-link-reviewer/requirements.txt
python -m pip install -r requirements-dev.txt
python -m compileall -q skills/wechat-article-link-reviewer/scripts tests tools
python -m pytest -q
python tools/validate_release.py
```

## Pull requests

1. Keep the canonical implementation only in `skills/wechat-article-link-reviewer/scripts/`.
2. Keep repository tests and contributor documentation outside the installable Skill.
3. Add tests for changed behavior and command contracts.
4. Never commit credentials, queue data, article exports, or production Base identifiers.
5. Preserve the prompt-injection boundary and explicit authorization requirement for external writes.
6. Update the root README and Skill references when public behavior changes.

By participating, you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
