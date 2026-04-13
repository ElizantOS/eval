---
id: demo-chat-clarify
title: Demo Chat Clarification
enabled: true
target_id: demo-chat
skill_name: demo-skill
tags: [demo, interactive]
entry_question: 帮我分析一下最近情况
expected_mode: interactive
conversation_script:
  - slot: time_range
    question_contains: [时间]
    answer: 最近7天
judge_rubric: 需要在缺少关键信息时先澄清，再继续回答。
hard_assertions: [no_error, non_empty_final_answer]
---

This is a minimal chat-completions-target example case.
