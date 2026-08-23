# Installation

Install the Skill using the repository installer, restart the Agent, and provide
a public `https://mp.weixin.qq.com/s/...` article URL. No WeChat account,
subscription, Cookie, token, or search window is configured by this Skill.

```text
bash install.sh --target agents
```

The runtime needs Python 3.10+, outbound network access, and the packages listed
in `requirements.txt`. Feishu is optional and is configured separately only when
the user asks to write reviews to an external Base.

The wrappers first use the Skill-owned isolated runtime created by the installer.
Without one, they select a Python 3.10+ interpreter that can import
`curl_cffi`, `requests`, and `beautifulsoup4`. The supported wrapper path
requires `curl_cffi` so live article requests use a browser-like TLS
fingerprint. Do not invoke the Python modules directly: their compatibility
fallback to plain `requests` is a degraded path and is not selected by the
wrappers.

An exact-host `http://mp.weixin.qq.com/s/...` input is canonicalized to HTTPS
before any request. Redirects and final requests remain restricted to HTTPS on
the exact public article host.

Optional environment overrides:

- `WECHAT_ARTICLE_HOME`: state directory for config, queue, and the isolated
  runtime.
- `WECHAT_ARTICLE_PYTHON`: exact Python executable selected by the wrappers.
- `WECHAT_SKILL_INSTALL_ROOT`: profile root used for target-specific installs.
- `WECHAT_LARK_CLI_PATH`: exact `lark-cli` executable when it is not on `PATH`.
