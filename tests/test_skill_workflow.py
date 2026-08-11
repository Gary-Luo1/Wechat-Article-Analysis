from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "wechat-article-subscriber"


def test_skill_requires_post_review_feishu_confirmation():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    workflow = text.split("## Guided link-review workflow", 1)[1]

    review_ready = workflow.index("prepare the review result")
    confirmation = workflow.index("这篇文章已经审阅完成。是否写入已确认的飞书表格")
    wait = workflow.index("Wait for the user's answer")
    write = workflow.index("After an affirmative answer")
    assert review_ready < confirmation < wait < write
    assert "Do not run `done` with `--feishu` while waiting" in workflow.replace("\n   ", " ")
    assert "sync-feishu --link <URL>" in workflow
    assert "current-task affirmative answer" in workflow


def test_automation_reference_covers_all_confirmation_states():
    text = (SKILL / "references" / "automation.md").read_text(encoding="utf-8")

    for state in (
        "`link_received`",
        "`review_ready`",
        "`write_confirmed`",
        "`write_declined`",
        "`write_unclear`",
    ):
        assert state in text
    assert "do not write now or infer consent later" in text.casefold()


def test_skill_has_pre_review_configuration_gate():
    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    automation_text = (SKILL / "references" / "automation.md").read_text(encoding="utf-8")

    assert "Pre-review configuration gate" in skill_text
    assert "这次需要把审阅结果写入飞书吗？" in skill_text
    assert "如果需要，使用哪个飞书多维表格" in skill_text
    assert "是否需要为本人开通这个多维表格的管理权限？" in skill_text
    assert "Do not request a Base token, App secret, or Open ID in chat" in skill_text
    assert "configuration permission, per-article write permission" in automation_text
    assert "setup_pending" in automation_text
    setup = skill_text.split("## Pre-review configuration gate", 1)[1].split(
        "## Guided link-review workflow", 1
    )[0]
    assert setup.index("这次需要把审阅结果写入飞书吗？") < setup.index(
        "如果需要，使用哪个飞书多维表格"
    ) < setup.index("是否需要为本人开通这个多维表格的管理权限？")
    assert "manage feishu-target --url-stdin" in setup


def test_readme_uses_user_identity_and_disables_bot_manager_grants():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    identity = readme.index("feishu-identity --as user")
    approval = readme.index("feishu-manager-access --mode approve")
    create = readme.index("feishu-create-base", approval)
    assert identity < approval < create
    assert "portable Bot creation and manager grants are disabled" in readme
