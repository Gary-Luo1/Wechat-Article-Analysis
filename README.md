# 微信公众号文章链接审阅器

一个通用 Agent Skill：只处理用户明确给出的公开微信公众号文章链接，读取正文、按五维评分、写入本地队列，并在用户逐篇确认后可选同步到飞书多维表格。

它不会监控公众号、发现文章，也不需要微信 Cookie 或 token。文章正文始终按不可信数据处理；本地队列只保存元数据和正文指纹，不保存正文。

## 安装

需要 Python 3.10+，以及能访问公开微信文章的网络。

macOS / Linux：

```bash
bash install.sh --target agents
```

Windows：

```powershell
pwsh -File .\install.ps1 -Target agents
```

安装器还支持 `codex`、`claude`、`copilot`、`openclaw`、`hermes` 和 `all`。完整安装会创建隔离 Python 环境；`--no-deps` / `-NoDeps` 仅适用于依赖已就绪的环境。

## 使用

安装并重启 Agent 后，直接发送公开文章链接：

```text
请审阅这篇文章：https://mp.weixin.qq.com/s/...
```

Skill 会先读取正文并完成五维评分。飞书写入是可选能力，必须同时满足：

1. 本任务已选定飞书目标。已保存的「跳过」不会反复追问；未配置时在评分之后再问。
2. 当前这篇文章得到明确确认。用户在同一条消息里要求写入这篇时，不再追问「写入」。低于 `settings.min_score`（默认 6.0）仍要单独确认。

Cursor 等独立环境只能用飞书**用户身份**。Bot 写入仅限受支持的飞书宿主（`openclaw`、`hermes`、`lark-channel`），且只能写已有表格。新建 Base 始终使用用户身份，不会做 Bot 管理员授权。成功创建或写入后，应回报可打开的表格链接（`document_url`），不要输出表格 token。

完整工作流、评分标准和飞书配置见：

- [SKILL.md](.agents/skills/wechat-article-link-reviewer/SKILL.md)
- [飞书配置](.agents/skills/wechat-article-link-reviewer/references/feishu.md)
- [安装与运行环境](.agents/skills/wechat-article-link-reviewer/references/setup.md)
- [队列与运维](.agents/skills/wechat-article-link-reviewer/references/operations.md)

评分后的确认规则以 `manage status` 的 `review.after_scoring` 为准。完成审阅统一用 `process done --link`；`--format json` 的结果里看 `feishu_written`、`document_url` 和 `record_url`。阈值、正文去重和评分标尺用 `manage settings`。默认标尺是 `technical`；公众号运营和合规文章用 `manage settings set --rubric content_ops`。

## 检查环境

```bash
bash .agents/skills/wechat-article-link-reviewer/scripts/run.sh manage doctor
```

本项目采用 MIT License，详见 [LICENSE](LICENSE)。
