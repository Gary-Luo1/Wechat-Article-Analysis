# Automation boundary

This Skill has no discovery or subscription scheduler. An Agent may process a
link only after the user supplies that exact URL in the current task. Reading,
scoring, and external Feishu writes remain Agent actions because article content
is untrusted and each external write must be explicitly requested.

`process --format json digest-plan` and queue exports are local-only helpers;
they never fetch pages, score content, or write Feishu.
