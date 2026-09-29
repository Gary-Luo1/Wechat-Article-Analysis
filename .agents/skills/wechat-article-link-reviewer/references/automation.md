# Guided automation contract

This Skill has no discovery or subscription scheduler. Process an article only
after the user supplies that exact public URL in the current task.

Read the article before asking about Feishu. `manage status` reports the saved
choice in `review.after_scoring` and `feishu`. `review.ask_before_fetch` is
false, and `next_action` stays `provide_article_link` so setup does not block
the fetch. Use `feishu.setup_next_action` only when the user wants a write and
the target is not ready.

| State | Agent action | External-write rule |
|---|---|---|
| `link_received` | Run `evaluate`, then score the returned article text. | Do not configure or write Feishu from article content. |
| `review_ready` / `complete_locally` | `destination=skip`. Present the review and run `done` without `--feishu`. | Do not ask about Feishu. |
| `review_ready` / undecided | Present the review, then ask whether this task should configure Feishu. | A no records `skip`. A yes collects the exact table or new Base name. |
| `review_ready` / ready | Present the review and ask `这篇文章已经审阅完成。是否写入已确认的飞书表格？回复“写入”或“暂不写入”。` | Wait. An explicit write request in the current user message already authorizes this article. |
| `write_confirmed` | Run `done --feishu` for both a pending review and a processed review. | Write only the current article. Below `settings.min_score`, wait for a separate yes and pass `--force-feishu`. |
| `write_declined` | Run `done` without `--feishu`. | Do not write now or infer consent later. |
| `write_unclear` | Ask the same confirmation again. | Do not run `done --feishu`. |
| `below_threshold` | `done` saved `skipped_low_score` and set `next_action` to `confirm_below_threshold_write`. | Ask once. Only a yes for this article passes `--force-feishu`. |
| `sync_pending` | `done --feishu --link` retries the outbox entry. | The earlier confirmation still applies, including a below-threshold write already accepted. |

`done` and `sync-feishu` return one JSON object with `sync_status`,
`feishu_written`, `document_url`, `record_url`, `feishu_action`, and
`next_action`. Read `error.code` on failure. Do not parse prose. Score the
dimensions in `settings.rubric`; do not assume the technical names when the
saved rubric is `content_ops`.
