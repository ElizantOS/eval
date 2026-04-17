async function judgeWithModel({ payload, metadata }) {
  if (payload && payload.evaluation && typeof payload.evaluation === "object") {
    return payload.evaluation;
  }
  if (process.env.SMARTBOT_EVAL_PROMPTFOO_SKIP_JUDGE === "1") {
    return {
      score: 0,
      verdict: "warn",
      summary: "judge deferred to reporting",
      strengths: [],
      issues: [],
    };
  }
  const apiKey = process.env.OPENAI_API_KEY || "";
  const baseUrl = (process.env.OPENAI_BASE_URL || process.env.OPENAI_API_BASE_URL || "https://api.openai.com/v1").replace(/\/$/, "");
  if (!apiKey) {
    return {
      score: 0,
      verdict: "fail",
      summary: "OPENAI_API_KEY missing for judge request",
      strengths: [],
      issues: ["judge provider is not configured"],
    };
  }

  const prompt = {
    case_id: metadata.caseId,
    title: metadata.title,
    skill_name: metadata.skillName,
    judge_rubric: metadata.judgeRubric,
    body_markdown: metadata.bodyMarkdown,
    provider_output: payload,
  };

  const res = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: "gpt-5.2",
      temperature: 0,
      response_format: { type: "json_object" },
      messages: [
        {
          role: "system",
          content:
            "你是智能问数 agent 的评测裁判。返回 JSON，字段固定为 score(0-10整数), verdict(pass|warn|fail), summary, strengths, issues。评分重点：是否贴题、是否遵守澄清策略、是否有幻觉、是否能继续原任务。",
        },
        {
          role: "user",
          content: JSON.stringify(prompt),
        },
      ],
    }),
  });

  if (!res.ok) {
    const raw = await res.text();
    return {
      score: 0,
      verdict: "fail",
      summary: `judge request failed: ${res.status}`,
      strengths: [],
      issues: [raw.slice(0, 500)],
    };
  }

  const json = await res.json();
  const text = json?.choices?.[0]?.message?.content || "{}";
  const parsed = JSON.parse(text);
  return {
    score: Number(parsed.score || 0),
    verdict: String(parsed.verdict || "fail"),
    summary: String(parsed.summary || "").trim(),
    strengths: Array.isArray(parsed.strengths) ? parsed.strengths.map(String).filter(Boolean) : [],
    issues: Array.isArray(parsed.issues) ? parsed.issues.map(String).filter(Boolean) : [],
  };
}

module.exports = async (output, context) => {
  const payload = JSON.parse(output);
  const vars = (context && context.vars) || {};
  const metadata = (context && context.test && context.test.metadata) || {};

  let hardAssertions = [];
  if (Array.isArray(vars.hard_assertions)) {
    hardAssertions = vars.hard_assertions;
  } else if (typeof vars.hard_assertions_json === "string" && vars.hard_assertions_json.trim()) {
    hardAssertions = JSON.parse(vars.hard_assertions_json);
  }

  const failures = [];
  const finalAnswer = String(payload.final_answer || "").trim();
  const askCount = Number(payload.ask_count || 0);
  const transcript = payload.transcript;
  const error = payload.error;

  for (const assertionName of hardAssertions) {
    if (assertionName === "no_error" && error) {
      failures.push(`expected no error but got: ${JSON.stringify(error)}`);
    } else if (assertionName === "non_empty_final_answer" && !finalAnswer) {
      failures.push("final_answer is empty");
    } else if (assertionName === "must_ask_clarification" && askCount <= 0) {
      failures.push("expected ask_clarification to occur at least once");
    } else if (assertionName === "must_not_require_clarification" && askCount !== 0) {
      failures.push("expected no ask_clarification calls");
    } else if (assertionName === "transcript_present" && !transcript) {
      failures.push("expected transcript to be present");
    }
  }

  const hardPass = failures.length === 0;
  const hardScore = hardPass ? 1 : 0;
  const judge = await judgeWithModel({ payload, metadata });
  const judgeDeferred = judge.summary === "judge deferred to reporting";
  const judgeNormalized = judgeDeferred ? 0 : Math.max(0, Math.min(1, Number(judge.score || 0) / 10));
  const finalScore = hardPass ? (judgeDeferred ? hardScore : 0.4 * hardScore + 0.6 * judgeNormalized) : 0;

  return {
    pass: hardPass && (judgeDeferred || judge.verdict !== "fail"),
    score: finalScore,
    reason: hardPass ? `hard assertions passed; judge=${judge.score}/10` : failures.join("; "),
    comment: `Judge ${judge.score}/10 [${judge.verdict}]${judge.summary ? ` - ${judge.summary}` : ""}`,
    namedScores: {
      HardAssert: Number(hardScore.toFixed(2)),
      Judge: Number(judgeNormalized.toFixed(2)),
      FinalEval: Number(finalScore.toFixed(2)),
    },
  };
};
