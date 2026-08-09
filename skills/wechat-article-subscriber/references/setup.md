# Installation

Install the Skill using the repository installer, restart the Agent, and provide
a public `https://mp.weixin.qq.com/s/...` article URL. No WeChat account,
subscription, Cookie, token, or search window is configured by this Skill.

```text
bash install.sh --target agents
```

The runtime needs Python 3.9+, outbound network access, and the packages listed
in `requirements.txt`. Feishu is optional and is configured separately only when
the user asks to write reviews to an external Base.
