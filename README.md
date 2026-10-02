<h1 align="center">微信公众号文章链接审阅器</h1>
<p align="center">
  <strong>读取用户给出的公开微信文章，完成五维评分，并在确认后同步到飞书多维表格。</strong>
  <br />
  <em>链接审阅 · 五维评分 · 可选飞书同步</em>
</p>

<p align="center">
  <a href="#快速开始"><img src="https://img.shields.io/badge/Quick_Start-4CAF50?style=for-the-badge" alt="Quick Start" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="License" /></a>
</p>

<p align="center">
  <a href="https://cursor.com"><img src="https://img.shields.io/badge/Cursor-000000?style=flat&logo=cursor&logoColor=white" alt="Cursor" /></a>
  <a href="https://docs.anthropic.com/en/docs/claude-code"><img src="https://img.shields.io/badge/Claude_Code-D97757?style=flat&logo=claude&logoColor=white" alt="Claude Code" /></a>
  <a href="https://github.com/features/copilot"><img src="https://img.shields.io/badge/GitHub_Copilot-000000?style=flat&logo=github&logoColor=white" alt="GitHub Copilot" /></a>
  <a href="https://github.com/openai/codex"><img src="https://img.shields.io/badge/Codex-000000?style=flat&logo=openai&logoColor=white" alt="Codex" /></a>
</p>

<p align="center">
  <a href="https://github.com/Gary-Luo1/Wechat-Article-Analysis/actions"><img src="https://img.shields.io/github/actions/workflow/status/Gary-Luo1/Wechat-Article-Analysis/ci.yml?style=flat" alt="Build" /></a>
  <a href="https://github.com/Gary-Luo1/Wechat-Article-Analysis/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Gary-Luo1/Wechat-Article-Analysis?style=flat" alt="License" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python" /></a>
</p>

## 功能特性

本 Skill 只审阅用户明确给出的一篇公开文章，并把飞书写入留到评分之后。

| 功能 | 说明 |
| --- | --- |
| 指定链接 | 处理用户给出的 `mp.weixin.qq.com/s` 公开链接。不监控公众号，不使用微信 Cookie 或 token。 |
| 五维评分 | 默认标尺是 `technical`。公众号运营和合规文章使用 `content_ops`。 |
| 本地队列 | 保存元数据和正文指纹。正文只留在临时缓存里，完成或忽略后删除。 |
| 逐篇确认 | 评分结束后再决定是否写入飞书。低于 `settings.min_score`（默认 6.0）需要单独确认。 |
| 飞书身份 | Cursor 等独立环境使用飞书用户身份。Bot 写入只在 `openclaw`、`hermes`、`lark-channel` 上处理已有表格。 |
| 广告判断 | 命中广告启发式时先向用户确认。用户确认是广告后，以广告结束，不再评分。 |

## 快速开始

以下步骤把 Skill 安装到本机，并完成第一次审阅。

### 环境

- Python 3.10+
- 能访问公开微信文章的网络

### 安装

macOS / Linux：

```bash
bash install.sh --target agents
```

Windows：

```powershell
pwsh -File .\install.ps1 -Target agents
```

`--target` 还接受 `codex`、`claude`、`copilot`、`openclaw`、`hermes` 和 `all`。默认目标 `agents` 会安装到 `.agents/skills`。依赖已经存在时，使用 `--no-deps` 或 `-NoDeps`。

### 运行

安装并重启 Agent 后，发送公开文章链接：

```text
请审阅这篇文章：https://mp.weixin.qq.com/s/...
```

## 使用方法

仓库内的命令通过 `scripts/run.sh` 调用。Windows 使用 `scripts/run.ps1`，参数相同。

### 检查环境

```bash
bash .agents/skills/wechat-article-link-reviewer/scripts/run.sh manage doctor
```

### 切换评分标尺

```bash
bash .agents/skills/wechat-article-link-reviewer/scripts/run.sh manage settings set --rubric content_ops
```

### 完成审阅

```bash
bash .agents/skills/wechat-article-link-reviewer/scripts/run.sh process done --link "https://mp.weixin.qq.com/s/..."
```

确认写入时加上 `--feishu`。分数低于默认 6.0 且用户已经单独确认时，再加上 `--force-feishu`。JSON 结果读取 `feishu_written`、`feishu_action`、`document_url` 和 `record_url`。回报可打开的表格链接，不输出表格 token。

## 架构

审阅从公开链接进入本地队列。飞书同步发生在这篇文章得到确认之后。

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'fontSize': '14px'}}}%%
flowchart TD
    A[用户给出公开链接] --> B[读取微信正文]
    B --> C{是否为广告}
    C -->|是| D[按广告结束]
    C -->|否| E[五维评分]
    E --> F[(本地队列)]
    F --> G{是否写入飞书}
    G -->|是| H[飞书多维表格]
    G -->|否| I[仅保留本地结果]

    classDef start fill:#3B82F6,stroke:#2563EB,color:#fff,stroke-width:2px
    classDef process fill:#10B981,stroke:#059669,color:#fff,stroke-width:2px
    classDef decision fill:#F59E0B,stroke:#D97706,color:#fff,stroke-width:2px
    classDef data fill:#8B5CF6,stroke:#7C3AED,color:#fff,stroke-width:2px
    classDef external fill:#F43F5E,stroke:#E11D48,color:#fff,stroke-width:2px
    classDef end fill:#8B5CF6,stroke:#7C3AED,color:#fff,stroke-width:2px

    class A start
    class E process
    class C,G decision
    class F data
    class B,H external
    class D,I end
```

## 项目结构

```text
.agents/skills/wechat-article-link-reviewer/
├── references/          # 评分、飞书、安装和运维说明
├── scripts/             # 审阅、队列和飞书命令
├── requirements.txt     # Python 依赖
└── SKILL.md             # Skill 入口
.github/workflows/
└── ci.yml               # GitHub Actions
tests/
└── test_link_review.py  # 单元测试
install.ps1              # Windows 安装
install.sh               # macOS / Linux 安装
LICENSE
README.md
```

## 技术栈

### 运行时

| 技术 | 用途 |
| --- | --- |
| Python 3.10+ | Skill 运行时。CI 在 3.10 和 3.12 上执行。 |

### 库

| 技术 | 用途 |
| --- | --- |
| curl_cffi | 获取公开微信文章页面 |
| requests | HTTP 客户端 |
| Beautiful Soup | 解析文章 HTML |
| urllib3 | HTTP 连接库 |

### 测试

| 技术 | 用途 |
| --- | --- |
| unittest | `tests/test_link_review.py` |

### 基础设施

| 技术 | 用途 |
| --- | --- |
| GitHub Actions | Ubuntu 与 Windows 上的编译、测试和运行检查 |

## 部署

本仓库用 GitHub Actions 做持续集成，配置在 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。工作流安装 `requirements.txt`，编译 Skill 模块，运行单元测试，并在空目录里执行 `manage status`。

## 贡献

1. Fork 本仓库。
2. 创建分支（`git checkout -b feature/name`）。
3. 提交改动。现有提交使用 `feat:`、`fix:`、`docs:`、`refactor:` 前缀。
4. 推送分支（`git push origin feature/name`）。
5. 打开 Pull Request。

提交前在仓库根目录运行：

```bash
python -m pip install -r .agents/skills/wechat-article-link-reviewer/requirements.txt
PYTHONPATH=.agents/skills/wechat-article-link-reviewer/scripts python -m unittest discover -s tests -v
```

## 许可证

[MIT](LICENSE)
