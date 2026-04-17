const ACTIVE_RUN_ID = "__active__";

const state = {
  view: "targets",
  targets: [],
  selectedTargetId: "",
  cases: [],
  caseSearch: "",
  caseTag: "",
  caseMode: "",
  selectedCaseId: "",
  runs: [],
  selectedRunId: "",
  runDetail: null,
  selectedResultCaseId: "",
  caseResult: null,
  backendTrace: null,
  selectedBackendSessionId: "",
  runState: null,
  liveProgress: null,
  activeRun: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return await response.json();
}

function formatTime(value) {
  if (!value) return "-";
  const normalized =
    typeof value === "number" ? (value > 1e12 ? value : value * 1000) : value;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatMs(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  if (number >= 1000) return `${(number / 1000).toFixed(1)}s`;
  return `${Math.round(number)}ms`;
}

function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${Math.round(number * 100)}%`;
}

function formatJudge(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(1)}/10`;
}

function tailText(value, maxChars = 12000) {
  const text = String(value || "");
  if (text.length <= maxChars) return text;
  return text.slice(-maxChars);
}

function stripAnsi(value) {
  return String(value || "").replace(/\u001b\[[0-9;]*m/g, "");
}

function cleanedRunnerLog(value) {
  const lines = stripAnsi(value)
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter(Boolean)
    .filter((line) => !line.includes("The current version of promptfoo"))
    .filter((line) => !line.includes("Please run npx promptfoo@latest"))
    .filter((line) => !line.includes("npm install -g"))
    .filter((line) => !line.includes("Do you want to share this with your team"))
    .filter((line) => !line.includes("This project needs your feedback"))
    .filter((line) => !/^=+$/.test(line))
    .filter((line) => !/^[┌┬┐└┴┘├┼┤│─\s]+$/.test(line));
  return lines.join("\n");
}

function tailLines(value, maxLines = 40) {
  const lines = String(value || "").split(/\r?\n/);
  if (lines.length <= maxLines) return lines.join("\n");
  return lines.slice(-maxLines).join("\n");
}

function resolveJudge(result) {
  return result?.judge || result?.judge_score || {};
}

function resolveFinalScore(result) {
  const value = result?.final_score ?? result?.final_eval_score;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function resolveCaseStatus(result) {
  if (result?.status) return result.status;
  if (result?.success === true) return "passed";
  if (result?.success === false) return "failed";
  return "unknown";
}

function statusClass(status) {
  if (status === "passed" || status === "completed") return "good";
  if (status === "failed" || status === "completed_with_failures") return "warn";
  if (status === "error" || status === "completed_with_errors") return "bad";
  return "warn";
}

function verdictClass(verdict) {
  if (verdict === "pass") return "good";
  if (verdict === "fail") return "bad";
  return "warn";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderInlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function renderMarkdown(markdown) {
  const source = String(markdown || "").replace(/\r\n/g, "\n").trim();
  if (!source) return '<p class="muted">-</p>';

  const lines = source.split("\n");
  const chunks = [];
  let paragraph = [];
  let listItems = [];
  let orderedItems = [];
  let inCode = false;
  let codeLines = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    chunks.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };

  const flushList = () => {
    if (!listItems.length) return;
    chunks.push(`<ul>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
    listItems = [];
  };

  const flushOrderedList = () => {
    if (!orderedItems.length) return;
    chunks.push(`<ol>${orderedItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`);
    orderedItems = [];
  };

  const flushCode = () => {
    if (!inCode) return;
    chunks.push(`<pre>${escapeHtml(codeLines.join("\n"))}</pre>`);
    codeLines = [];
    inCode = false;
  };

  for (const line of lines) {
    if (line.startsWith("```")) {
      flushParagraph();
      flushList();
      flushOrderedList();
      if (inCode) {
        flushCode();
      } else {
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }

    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      flushOrderedList();
      continue;
    }

    if (/^#{1,4}\s+/.test(trimmed)) {
      flushParagraph();
      flushList();
      flushOrderedList();
      const level = Math.min(4, trimmed.match(/^#+/)[0].length);
      const text = trimmed.replace(/^#{1,4}\s+/, "");
      chunks.push(`<h${level + 1}>${renderInlineMarkdown(text)}</h${level + 1}>`);
      continue;
    }

    if (trimmed === "---") {
      flushParagraph();
      flushList();
      flushOrderedList();
      chunks.push("<hr />");
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      flushParagraph();
      flushList();
      flushOrderedList();
      chunks.push(`<blockquote>${renderInlineMarkdown(trimmed.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }

    if (/^- /.test(trimmed)) {
      flushParagraph();
      flushOrderedList();
      listItems.push(trimmed.replace(/^- /, ""));
      continue;
    }

    if (/^\d+\.\s+/.test(trimmed)) {
      flushParagraph();
      flushList();
      orderedItems.push(trimmed.replace(/^\d+\.\s+/, ""));
      continue;
    }

    paragraph.push(trimmed);
  }

  flushParagraph();
  flushList();
  flushOrderedList();
  flushCode();
  return chunks.join("");
}

function selectedTarget() {
  return state.targets.find((target) => target.id === state.selectedTargetId) || null;
}

function selectedCase() {
  return state.cases.find((item) => item.id === state.selectedCaseId) || null;
}

function selectedRun() {
  return state.runs.find((item) => item.run_id === state.selectedRunId) || null;
}

function activeRunForTarget() {
  if (!state.activeRun?.running) return null;
  if (state.activeRun.target_id !== state.selectedTargetId) return null;
  return state.activeRun;
}

function runRows() {
  const active = activeRunForTarget();
  return active ? [active, ...state.runs] : state.runs;
}

function isActiveRunSelected() {
  return state.selectedRunId === ACTIVE_RUN_ID && !!activeRunForTarget();
}

function latestRun() {
  return state.runs[0] || null;
}

function isFullTargetRun(run) {
  const filters = run?.filters || {};
  return !filters.case && !filters.tag && !filters.skill;
}

function latestFullRun() {
  return state.runs.find((item) => isFullTargetRun(item)) || null;
}

function resultCases() {
  return Array.isArray(state.runDetail?.cases) ? state.runDetail.cases : [];
}

function enabledCaseCount() {
  return state.cases.filter((item) => item.enabled !== false).length;
}

function plannedCaseLabel(live) {
  const total = Number(live?.planned_case_count);
  if (Number.isFinite(total) && total > 0) {
    return `${total} case`;
  }
  return "-";
}

function selectedResultSummary() {
  return resultCases().find((item) => item.case_id === state.selectedResultCaseId) || null;
}

function activeResultCases() {
  return Array.isArray(state.activeRun?.cases) ? state.activeRun.cases : [];
}

function selectedActiveResult(active) {
  const cases = Array.isArray(active?.cases) ? active.cases : [];
  if (!cases.length) return null;
  return (
    cases.find((item) => item.case_id === state.selectedResultCaseId) ||
    cases.find((item) => item.case_id === active?.current_case?.case_id) ||
    cases[0]
  );
}

function caseTagOptions() {
  return [...new Set(state.cases.flatMap((item) => item.tags || []))].sort();
}

function runScopeLabel(run) {
  const filters = run?.filters || {};
  if (filters.case) return `单 Case 调试 · ${filters.case}`;
  if (filters.tag) return `批次 · tag=${filters.tag}`;
  if (filters.skill) return `批次 · skill=${filters.skill}`;
  return "批次 · 当前 Target 全量";
}

function runPromptfooLabel(run) {
  return run?.promptfoo_eval_id ? `Promptfoo Eval: ${run.promptfoo_eval_id}` : "Promptfoo Eval: -";
}

function activeRunScopeLabel(active) {
  return active?.scope?.label || "批次 · 当前 Target 全量";
}

function activeRunDurationMs(active) {
  const startedAt = Number(active?.started_at);
  if (!Number.isFinite(startedAt)) return null;
  return Math.max(0, Date.now() - startedAt * 1000);
}

function filteredCases() {
  const query = state.caseSearch.trim().toLowerCase();
  return state.cases.filter((item) => {
    const haystack = [item.id, item.title, item.skill_name, item.entry_question, item.summary]
      .join(" ")
      .toLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (state.caseTag && !(item.tags || []).includes(state.caseTag)) return false;
    if (state.caseMode && item.expected_mode !== state.caseMode) return false;
    return true;
  });
}

function ensureSelectedCaseVisible() {
  const visibleCases = filteredCases();
  if (!visibleCases.find((item) => item.id === state.selectedCaseId)) {
    state.selectedCaseId = visibleCases[0]?.id || "";
  }
}

function traceSummary(trace) {
  const turns = Array.isArray(trace?.turns) ? trace.turns : [];
  const toolCalls = [];
  const chartCalls = [];
  const sqlCalls = [];
  const cardCalls = [];
  const exportCalls = [];

  for (const turn of turns) {
    for (const item of turn.response_output || []) {
      if (item.type !== "function_call") continue;
      toolCalls.push(item);
      if (item.name === "smartbot_chart") chartCalls.push(item);
      if (item.name === "run_duckdb_sql") sqlCalls.push(item);
      if (item.name === "get_card" || item.name === "preview_card") cardCalls.push(item);
      if (item.name === "export_card_to_duckdb") exportCalls.push(item);
    }
  }

  const kind = toolCalls.length ? "真实查数型" : "规划型";
  return { turns, toolCalls, chartCalls, sqlCalls, cardCalls, exportCalls, kind };
}

async function refreshRunState() {
  const [runState, liveProgress, activeRun] = await Promise.all([
    api("/api/run"),
    api("/api/live"),
    api(`/api/active-run?target=${encodeURIComponent(state.selectedTargetId || "")}`),
  ]);
  state.runState = runState;
  state.liveProgress = liveProgress;
  state.activeRun = activeRun;
  renderStatusBar();
}

async function loadTargets() {
  const payload = await api("/api/targets");
  state.targets = payload.targets || [];
  if (!state.targets.find((target) => target.id === state.selectedTargetId)) {
    state.selectedTargetId = payload.default_target_id || state.targets[0]?.id || "";
  }
  renderTargetPicker();
}

async function loadTargetData() {
  if (!state.selectedTargetId) return;
  const [casesPayload, runsPayload, activeRunPayload] = await Promise.all([
    api(`/api/targets/${encodeURIComponent(state.selectedTargetId)}/cases`),
    api(`/api/targets/${encodeURIComponent(state.selectedTargetId)}/runs`),
    api(`/api/active-run?target=${encodeURIComponent(state.selectedTargetId)}`),
  ]);
  state.cases = casesPayload.cases || [];
  state.runs = runsPayload.runs || [];
  state.activeRun = activeRunPayload;

  if (!state.cases.find((item) => item.id === state.selectedCaseId)) {
    state.selectedCaseId = state.cases[0]?.id || "";
  }

  const availableRunIds = new Set(runRows().map((item) => item.run_id));
  if (!availableRunIds.has(state.selectedRunId)) {
    state.selectedRunId = activeRunForTarget() ? ACTIVE_RUN_ID : state.runs[0]?.run_id || "";
  }

  if (state.selectedRunId === ACTIVE_RUN_ID) {
    state.runDetail = null;
    state.selectedResultCaseId = "";
    state.caseResult = null;
    state.backendTrace = null;
    state.selectedBackendSessionId = "";
  } else if (state.selectedRunId) {
    await loadRunDetail(state.selectedRunId);
  } else {
    state.runDetail = null;
    state.selectedResultCaseId = "";
    state.caseResult = null;
    state.backendTrace = null;
    state.selectedBackendSessionId = "";
  }
}

async function loadRunDetail(runId) {
  state.selectedRunId = runId;
  state.runDetail = await api(`/api/runs/${encodeURIComponent(runId)}`);
  const cases = resultCases();
  if (!cases.find((item) => item.case_id === state.selectedResultCaseId)) {
    state.selectedResultCaseId = cases[0]?.case_id || "";
  }
  if (state.selectedResultCaseId) {
    await loadCaseResult(state.selectedResultCaseId);
  } else {
    state.caseResult = null;
    state.backendTrace = null;
    state.selectedBackendSessionId = "";
  }
}

async function loadCaseResult(caseId) {
  if (!state.selectedRunId) return;
  state.selectedResultCaseId = caseId;
  state.caseResult = await api(
    `/api/runs/${encodeURIComponent(state.selectedRunId)}/results/${encodeURIComponent(caseId)}`
  );
  const sessionIds = state.caseResult.backend_session_ids || [];
  state.selectedBackendSessionId = sessionIds[0] || "";
  await loadBackendTrace();
}

async function loadBackendTrace() {
  if (!state.selectedBackendSessionId) {
    state.backendTrace = null;
    return;
  }
  const archived = await api(
    `/api/runs/${encodeURIComponent(state.selectedRunId)}/backend/${encodeURIComponent(state.selectedBackendSessionId)}`
  );
  if (archived && Object.keys(archived).length) {
    state.backendTrace = archived;
    return;
  }
  state.backendTrace = await api(
    `/api/backend/session/${encodeURIComponent(state.selectedBackendSessionId)}/turns?target=${encodeURIComponent(state.selectedTargetId)}`
  );
}

async function refreshAll() {
  await loadTargets();
  await refreshRunState();
  await loadTargetData();
  renderApp();
}

async function startRun(payload) {
  await api("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.selectedRunId = ACTIVE_RUN_ID;
  state.view = "runs";
  state.runDetail = null;
  state.selectedResultCaseId = "";
  state.caseResult = null;
  state.backendTrace = null;
  state.selectedBackendSessionId = "";
  await refreshRunState();
  renderApp();
  pollRunState();
}

async function pollRunState() {
  const [runState, liveProgress, activeRun] = await Promise.all([
    api("/api/run"),
    api("/api/live"),
    api(`/api/active-run?target=${encodeURIComponent(state.selectedTargetId || "")}`),
  ]);
  state.runState = runState;
  state.liveProgress = liveProgress;
  state.activeRun = activeRun;
  renderStatusBar();
  renderApp();
  if (runState.running) {
    setTimeout(pollRunState, 1500);
    return;
  }
  await loadTargetData();
  renderApp();
}

function renderTargetPicker() {
  $("targetPicker").innerHTML = state.targets
    .map((target) => `<option value="${escapeHtml(target.id)}">${escapeHtml(target.name)}</option>`)
    .join("");
  $("targetPicker").value = state.selectedTargetId;
}

function renderStatusBar() {
  const runState = state.runState;
  $("runStatus").textContent = !runState
    ? "空闲"
    : runState.running
    ? "运行中"
    : runState.exit_code === null
    ? "空闲"
    : `已完成 · exit ${runState.exit_code}`;
  $("runCommand").textContent = runState?.command?.length ? runState.command.join(" ") : "-";
}

function renderNav() {
  document.querySelectorAll(".navtab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === state.view);
  });
}

function renderTargetsView() {
  const cards = state.targets
    .map(
      (target) => `
        <article class="card">
          <h3>${escapeHtml(target.name)}</h3>
          <p>${escapeHtml(target.base_url)}</p>
          <div class="tags">
            <span class="tag">${escapeHtml(target.id)}</span>
            <span class="tag">${escapeHtml(target.protocol)}</span>
            <span class="tag">${escapeHtml(target.conversation_mode)}</span>
            <span class="tag">${escapeHtml(target.clarification_mode)}</span>
            <span class="tag">${target.case_count} cases</span>
            <span class="tag">${target.run_count} runs</span>
          </div>
          <div class="card-actions">
            <button data-target-action="cases" data-target-id="${escapeHtml(target.id)}">查看 Cases</button>
            <button class="ghost" data-target-action="runs" data-target-id="${escapeHtml(target.id)}">查看 Runs</button>
          </div>
          <div class="detail-block">
            <h4>能力声明</h4>
            <p>会话模式：${escapeHtml(target.conversation_mode)} · history：${escapeHtml(target.history_strategy)} · tool 调用：${escapeHtml(target.tool_call_shape)}</p>
          </div>
        </article>
      `
    )
    .join("");

  return `
    <section class="stack">
      <div class="panel-header">
        <div>
          <h2 class="panel-title">Targets</h2>
          <p class="panel-subtitle">Target 是评测的一等入口。先选目标，再进入它自己的 Cases 和 Runs。</p>
        </div>
      </div>
      <div class="grid-3">${cards || '<div class="empty">暂无 target 配置。</div>'}</div>
    </section>
  `;
}

function renderCasesView() {
  ensureSelectedCaseVisible();
  const cases = filteredCases();
  const active = selectedCase();
  const tagOptions = caseTagOptions()
    .map((tag) => `<option value="${escapeHtml(tag)}">${escapeHtml(tag)}</option>`)
    .join("");

  return `
    <section class="stack">
      <div class="panel">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">Cases</h2>
            <p class="panel-subtitle">这里只展示当前 target 的 case 资产，不混入历史结果。</p>
          </div>
          <div class="card-actions">
            <button id="debugSelectedCase" ${active ? "" : "disabled"}>调试当前 Case</button>
            <button id="runFilteredCases" class="secondary">运行当前 Tag 批次</button>
            <button id="runAllCases" class="ghost">运行当前 Target 全量</button>
          </div>
        </div>
        <div class="toolbar">
          <input id="caseSearchInput" placeholder="搜索 case / title / skill / question" value="${escapeHtml(state.caseSearch)}" />
          <select id="caseTagFilter">
            <option value="">全部标签</option>
            ${tagOptions}
          </select>
          <select id="caseModeFilter">
            <option value="">全部模式</option>
            <option value="single_turn">single_turn</option>
            <option value="interactive">interactive</option>
          </select>
        </div>
        <p class="muted">这里的 Run 指“一轮批量评测”。<code>调试当前 Case</code> 只用于单条调试，不等同于批次 Run。搜索和 mode 仅用于浏览，批量 Run 当前只支持按 tag 或全量。</p>
      </div>
      <div class="grid-2">
        <section class="panel">
          <div class="panel-header">
            <div>
              <h3 class="panel-title">Case Catalog</h3>
              <p class="panel-subtitle">${cases.length} 条匹配结果</p>
            </div>
          </div>
          <div class="card-list">
            ${
              cases.length
                ? cases
                    .map(
                      (item) => `
                        <article class="card ${item.id === state.selectedCaseId ? "active" : ""}" data-case-id="${escapeHtml(item.id)}">
                          <h3>${escapeHtml(item.title)}</h3>
                          <p>${escapeHtml(item.entry_question)}</p>
                          <div class="tags">
                            <span class="tag">${escapeHtml(item.source)}</span>
                            <span class="tag">${escapeHtml(item.expected_mode)}</span>
                            ${(item.tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
                          </div>
                        </article>
                      `
                    )
                    .join("")
                : '<div class="empty">当前筛选下没有 case。</div>'
            }
          </div>
        </section>
        <section class="panel">
          ${
            active
              ? `
                <div class="panel-header">
                  <div>
                    <h3 class="panel-title">${escapeHtml(active.title)}</h3>
                    <p class="panel-subtitle">${escapeHtml(active.id)} · ${escapeHtml(active.skill_name || "-")} · ${escapeHtml(active.expected_mode)}</p>
                  </div>
                </div>
                <div class="detail-block">
                  <h4>Entry Question</h4>
                  ${renderMarkdown(active.entry_question)}
                </div>
                <div class="detail-block">
                  <h4>Markdown Body</h4>
                  <div class="markdown">${renderMarkdown(active.body || active.summary || "")}</div>
                </div>
                <div class="detail-block">
                  <h4>Judge Rubric</h4>
                  <div class="markdown">${renderMarkdown(active.judge_rubric || "-")}</div>
                </div>
                <div class="detail-block">
                  <h4>Hard Assertions</h4>
                  <div class="tags">${(active.hard_assertions || []).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("") || '<span class="muted">-</span>'}</div>
                </div>
                <div class="detail-block">
                  <h4>Conversation Script</h4>
                  ${
                    active.conversation_script?.length
                      ? active.conversation_script
                          .map(
                            (step, index) => `
                              <details open>
                                <summary>Step ${index + 1} · ${escapeHtml(step.slot || "freeform")}</summary>
                                <pre>${escapeHtml(JSON.stringify(step, null, 2))}</pre>
                              </details>
                            `
                          )
                          .join("")
                      : '<div class="empty">这个 case 没有 conversation_script。</div>'
                  }
                </div>
              `
              : '<div class="empty">请选择一个 case 查看详情。</div>'
          }
        </section>
      </div>
    </section>
  `;
}

function renderActiveRunHistoryRow(active) {
  const currentCase = active?.current_case || {};
  const planned = Number(active?.planned_case_count);
  const currentIndex = Number(active?.current_index);
  const progressLabel =
    Number.isFinite(currentIndex) && currentIndex > 0 && Number.isFinite(planned) && planned > 0
      ? `当前 ${currentIndex}/${planned}`
      : "当前初始化中";

  return `
    <article class="table-row ${isActiveRunSelected() ? "active" : ""}" data-run-id="${ACTIVE_RUN_ID}">
      <div class="table-row-top">
        <div>
          <h3>运行中 · ${escapeHtml(activeRunScopeLabel(active))}</h3>
          <p>${escapeHtml(formatTime(active.started_at))} · running · ${escapeHtml(activeRunScopeLabel(active))}</p>
          <p class="muted">当前 case：${escapeHtml(currentCase.title || currentCase.case_id || "正在初始化...")}</p>
        </div>
        <button data-open-run-detail="${ACTIVE_RUN_ID}">查看详情</button>
      </div>
      <div class="table-meta">
        <span class="tag good">running</span>
        <span class="tag">${Number.isFinite(planned) && planned > 0 ? `${planned} cases / round` : "cases -"}</span>
        <span class="tag">${escapeHtml(progressLabel)}</span>
        <span class="tag">${escapeHtml(currentCase.status || "-")}</span>
        <span class="tag">turn ${escapeHtml(currentCase.turn_index ?? "-")}</span>
        <span class="tag">ask ${escapeHtml(currentCase.ask_count ?? "-")}</span>
        <span class="tag">串行执行 1/1</span>
        <span class="tag">${formatMs(activeRunDurationMs(active))}</span>
      </div>
    </article>
  `;
}

function renderArchivedRunHistoryRow(run) {
  return `
    <article class="table-row ${run.run_id === state.selectedRunId ? "active" : ""}" data-run-id="${escapeHtml(run.run_id)}">
      <div class="table-row-top">
        <div>
          <h3>${escapeHtml(runScopeLabel(run))}</h3>
          <p>${escapeHtml(formatTime(run.completed_at || run.generated_at || run.started_at))} · ${escapeHtml(run.status || "-")} · ${escapeHtml(runScopeLabel(run))}</p>
          <p class="muted">${escapeHtml(runPromptfooLabel(run))}</p>
        </div>
        <button data-open-run-detail="${escapeHtml(run.run_id)}">查看详情</button>
      </div>
      <div class="table-meta">
        <span class="tag">${run.case_count} cases / round</span>
        <span class="tag good">passed ${Number(run.status_counts?.passed || 0)}</span>
        <span class="tag warn">failed ${Number(run.status_counts?.failed || 0)}</span>
        <span class="tag bad">error ${Number(run.status_counts?.error || 0)}</span>
        <span class="tag">Judge ${run.judge_avg ?? "-"}</span>
        <span class="tag">Final ${run.final_avg ?? "-"}</span>
        <span class="tag">${formatMs(run.duration_ms)}</span>
      </div>
    </article>
  `;
}

function renderRunsView() {
  const target = selectedTarget();
  const active = activeRunForTarget();
  const rows = runRows();
  const judgeAvg = state.runs
    .map((item) => Number(item.judge_avg))
    .filter(Number.isFinite);
  const finalAvg = state.runs
    .map((item) => Number(item.final_avg))
    .filter(Number.isFinite);

  return `
    <section class="stack">
      <div class="grid-4">
        <div class="metric"><div class="metric-label">Target</div><div class="metric-value">${escapeHtml(target?.name || "-")}</div></div>
        <div class="metric"><div class="metric-label">Runs</div><div class="metric-value">${state.runs.length}</div></div>
        <div class="metric"><div class="metric-label">Judge Avg</div><div class="metric-value">${judgeAvg.length ? formatJudge(judgeAvg.reduce((a, b) => a + b, 0) / judgeAvg.length) : "-"}</div></div>
        <div class="metric"><div class="metric-label">Final Avg</div><div class="metric-value">${finalAvg.length ? formatPercent(finalAvg.reduce((a, b) => a + b, 0) / finalAvg.length) : "-"}</div></div>
      </div>
      <section class="panel">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">Run History</h2>
            <p class="panel-subtitle">第一条如果是 running，就是当前正在执行的这一轮；后面都是已归档历史。</p>
          </div>
        </div>
        <div class="table">
          ${
            rows.length
              ? rows
                  .map((run) => (run.run_id === ACTIVE_RUN_ID ? renderActiveRunHistoryRow(run) : renderArchivedRunHistoryRow(run)))
                  .join("")
              : '<div class="empty">当前 target 还没有历史 run。</div>'
          }
        </div>
      </section>
    </section>
  `;
}

function renderActiveRunDetailView(active) {
  const currentCase = active?.current_case || {};
  const selectedCase = selectedActiveResult(active) || currentCase;
  const streamEvents = Array.isArray(selectedCase?.stream_events) ? selectedCase.stream_events : [];
  const stdout = tailText(active?.stdout_tail || "", 10000);
  const stdoutClean = tailLines(cleanedRunnerLog(stdout), 40);
  const streamText = tailText(selectedCase?.stream_text || "", 10000);
  const activeCases = activeResultCases();
  const resultList = activeCases
    .map(
      (item) => `
        <article class="table-row ${item.case_id === selectedCase?.case_id ? "active" : ""}" data-active-case-id="${escapeHtml(item.case_id)}">
          <div class="table-row-top">
            <div>
              <h3>${escapeHtml(item.title || item.case_id || "-")}</h3>
              <p>${escapeHtml(item.case_id || "-")} · ${escapeHtml(item.skill_name || "-")}</p>
            </div>
            <div class="tags">
              <span class="tag ${statusClass(item.status === "completed" ? "passed" : item.status)}">${escapeHtml(item.status || "pending")}</span>
              ${item.case_id === currentCase.case_id ? '<span class="tag good">current</span>' : ""}
            </div>
          </div>
          <div class="table-meta">
            <span class="tag">序号 ${escapeHtml(item.index ?? "-")}/${escapeHtml(active.planned_case_count ?? "-")}</span>
            <span class="tag">ask ${escapeHtml(item.ask_count ?? "-")}</span>
            ${Number.isFinite(Number(item.hard_assert_score)) ? `<span class="tag">Hard ${Number(item.hard_assert_score).toFixed(2)}</span>` : ""}
            ${Number.isFinite(Number(item?.judge_score?.score)) ? `<span class="tag">Judge ${Number(item.judge_score.score)}/10</span>` : ""}
            ${Number.isFinite(Number(item.final_score)) ? `<span class="tag">Final ${formatPercent(item.final_score)}</span>` : ""}
            ${
              item.status === "pending"
                ? '<span class="tag">尚未运行</span>'
                : item.status === "running"
                ? '<span class="tag good">运行中</span>'
                : item.error
                ? '<span class="tag bad">有错误</span>'
                : '<span class="tag">已完成</span>'
            }
          </div>
        </article>
      `
    )
    .join("");

  return `
    <section class="stack">
      <div class="grid-4">
        <div class="metric"><div class="metric-label">开始时间</div><div class="metric-value">${escapeHtml(formatTime(active.started_at))}</div></div>
        <div class="metric"><div class="metric-label">范围</div><div class="metric-value">${escapeHtml(activeRunScopeLabel(active))}</div></div>
        <div class="metric"><div class="metric-label">Cases</div><div class="metric-value">${escapeHtml(active.planned_case_count ?? "-")}</div></div>
        <div class="metric"><div class="metric-label">当前进度</div><div class="metric-value">${escapeHtml(active.current_index ?? "-")}/${escapeHtml(active.planned_case_count ?? "-")}</div></div>
      </div>
      <section class="panel">
        <div class="panel-header">
          <div>
            <h2 class="panel-title">当前正在执行的批次</h2>
            <p class="panel-subtitle">这是一轮正在执行的 run，不是历史归档结果。当前执行为串行（concurrency = 1），这里展示的是本轮自己的 case 列表，不混入旧 run 的结果。</p>
          </div>
        </div>
        <div class="tags">
          <span class="tag good">running</span>
          <span class="tag">${escapeHtml(active.target_id || "-")}</span>
          <span class="tag">本轮 ${escapeHtml(plannedCaseLabel(active))}</span>
          <span class="tag">当前 ${escapeHtml(active.current_index ?? "-")}/${escapeHtml(active.planned_case_count ?? "-")}</span>
          <span class="tag">${escapeHtml(currentCase.status || "-")}</span>
          <span class="tag">turn ${escapeHtml(currentCase.turn_index ?? "-")}</span>
          <span class="tag">ask ${escapeHtml(currentCase.ask_count ?? "-")}</span>
        </div>
      </section>
      <div class="split">
        <section class="panel">
          <div class="panel-header">
            <div>
              <h2 class="panel-title">本轮 Case 列表</h2>
              <p class="panel-subtitle">同一轮里：未开始显示空内容，当前运行显示实时输出，已完成直接展示本轮结果。</p>
            </div>
          </div>
          <div class="table">${resultList || '<div class="empty">本轮还没有 case。</div>'}</div>
        </section>
        <section class="stack">
          <section class="panel">
            <div class="panel-header">
              <div>
                <h2 class="panel-title">${escapeHtml(selectedCase?.title || selectedCase?.case_id || "正在初始化当前 case")}</h2>
                <p class="panel-subtitle">${escapeHtml(selectedCase?.case_id || "-")} · ${escapeHtml(activeRunScopeLabel(active))}</p>
              </div>
            </div>
          <div class="detail-block">
            <h4>Entry Question</h4>
            <div class="markdown">${renderMarkdown(selectedCase?.entry_question || "-")}</div>
          </div>
          ${
            Number.isFinite(Number(selectedCase?.hard_assert_score)) || Number.isFinite(Number(selectedCase?.judge_score?.score)) || Number.isFinite(Number(selectedCase?.final_score))
              ? `
                <div class="grid-3">
                  <div class="mini-card panel"><div class="metric-label">HardAssert</div><div class="metric-value">${Number.isFinite(Number(selectedCase?.hard_assert_score)) ? Number(selectedCase.hard_assert_score).toFixed(2) : "-"}</div></div>
                  <div class="mini-card panel"><div class="metric-label">Judge</div><div class="metric-value">${Number.isFinite(Number(selectedCase?.judge_score?.score)) ? formatJudge(selectedCase.judge_score.score) : "-"}</div></div>
                  <div class="mini-card panel"><div class="metric-label">FinalEval</div><div class="metric-value">${Number.isFinite(Number(selectedCase?.final_score)) ? formatPercent(selectedCase.final_score) : "-"}</div></div>
                </div>
              `
              : ""
          }
          ${
            selectedCase?.judge_score?.summary
              ? `
                <div class="detail-block">
                  <h4>Judge Summary</h4>
                  <p>${escapeHtml(selectedCase.judge_score.summary)}</p>
                </div>
              `
              : ""
          }
          ${
            selectedCase?.last_user_reply
              ? `
                <div class="detail-block">
                  <h4>最近一次自动回复</h4>
                  <div class="markdown">${renderMarkdown(selectedCase.last_user_reply)}</div>
                </div>
              `
              : ""
          }
          <div class="detail-block">
            <h4>${selectedCase?.status === "running" ? "当前流式输出" : "本轮即时结果"}</h4>
            <div class="markdown answer">${renderMarkdown(streamText || selectedCase?.final_answer || "-")}</div>
          </div>
          ${
            selectedCase?.error
              ? `
                <div class="detail-block">
                  <h4>错误</h4>
                  <pre>${escapeHtml(JSON.stringify(selectedCase.error, null, 2))}</pre>
                </div>
              `
              : ""
          }
          <div class="detail-block">
            <h4>当前说明</h4>
            <p class="muted">${
              selectedCase?.status === "pending"
                ? "这条 case 还没有运行到，所以这里只显示空内容。"
                : selectedCase?.status === "running"
                ? "这条 case 正在执行中，这里显示当前 live 输出。"
                : "这条 case 已经在本轮里跑完，这里显示本轮即时结果和即时评分；归档完成后会再落成正式历史结果。"
            }</p>
          </div>
          ${
            selectedCase?.transcript?.length
              ? `
                <div class="detail-block">
                  <h4>Transcript</h4>
                  ${selectedCase.transcript
                    .map(
                      (turn) => `
                        <details>
                          <summary>Turn ${escapeHtml(turn.turn ?? "-")} · ${escapeHtml(turn.status || "-")} · ${escapeHtml(turn.response_id || "-")}</summary>
                          <pre>${escapeHtml(JSON.stringify(turn, null, 2))}</pre>
                        </details>
                      `
                    )
                    .join("")}
                </div>
              `
              : ""
          }
          </section>
          <section class="panel">
            <div class="panel-header">
              <div>
                <h2 class="panel-title">Runner Log</h2>
                <p class="panel-subtitle">当前这轮 run 的实时标准输出。</p>
              </div>
            </div>
            <pre>${escapeHtml(stdoutClean || "暂无可读 stdout")}</pre>
            <details>
              <summary>查看原始 Runner Log</summary>
              <pre>${escapeHtml(stdout || "暂无 stdout")}</pre>
            </details>
          </section>
          <section class="panel">
            <div class="panel-header">
              <div>
                <h2 class="panel-title">Agent 实时流事件</h2>
                <p class="panel-subtitle">只显示当前 active case 的流式事件。</p>
              </div>
            </div>
            <div class="tags">
              <span class="tag">${streamEvents.length} events</span>
              <span class="tag">${escapeHtml(selectedCase?.session_id || "-")}</span>
            </div>
            ${
              streamEvents.length
                ? streamEvents
                    .slice(-30)
                    .map(
                      (event) => `
                        <details>
                          <summary>${escapeHtml(event.type || "-")}</summary>
                          <pre>${escapeHtml(JSON.stringify(event, null, 2))}</pre>
                        </details>
                      `
                    )
                    .join("")
                : '<div class="empty">当前还没有流式事件。</div>'
            }
          </section>
        </section>
      </div>
    </section>
  `;
}

function renderArchivedRunDetailView(run) {
  const caseResult = state.caseResult;
  const trace = traceSummary(state.backendTrace);
  const caseJudge = resolveJudge(caseResult);
  const caseFinalScore = resolveFinalScore(caseResult);
  const newestRun = latestRun();
  const newestFullRun = latestFullRun();
  const isViewingLatest = run && newestRun && run.run_id === newestRun.run_id;
  const isViewingLatestFull = run && newestFullRun && run.run_id === newestFullRun.run_id;
  const cleanedLog = tailLines(cleanedRunnerLog(run?.log || ""), 40);

  const resultList = resultCases()
    .map(
      (item) => `
        <article class="table-row ${item.case_id === state.selectedResultCaseId ? "active" : ""}" data-result-case-id="${escapeHtml(item.case_id)}">
          <div class="table-row-top">
            <div>
              <h3>${escapeHtml(item.title || item.case_id)}</h3>
              <p>${escapeHtml(item.case_id)} · ${escapeHtml(item.skill_name || "-")}</p>
            </div>
            <div class="tags">
              <span class="tag ${statusClass(resolveCaseStatus(item))}">${escapeHtml(resolveCaseStatus(item))}</span>
              ${item.error_type ? `<span class="tag">${escapeHtml(item.error_type)}</span>` : ""}
            </div>
          </div>
          <div class="table-meta">
            <span class="tag">Hard ${Number(item.hard_assert_score ?? 0).toFixed(2)}</span>
            <span class="tag">Judge ${resolveJudge(item).score ?? "-"}</span>
            <span class="tag">Final ${resolveFinalScore(item) ?? "-"}</span>
            <span class="tag">${formatMs(item.latency_ms)}</span>
            <span class="tag">${item.ask_count || 0} asks</span>
          </div>
        </article>
      `
    )
    .join("");

  return `
    <section class="stack">
      ${
        run
          ? `
            ${
              !isViewingLatest || !isViewingLatestFull
                ? `
                  <section class="panel">
                    <div class="panel-header">
                      <div>
                        <h2 class="panel-title">当前查看的不是最新结果</h2>
                        <p class="panel-subtitle">你现在看的 run 是 ${escapeHtml(run.run_id)}。最新 run 和最新全量 run 可能已经更新。</p>
                      </div>
                      <div class="card-actions">
                        ${
                          newestRun && !isViewingLatest
                            ? `<button id="jumpToNewestRun">切到最新 Run</button>`
                            : ""
                        }
                        ${
                          newestFullRun && !isViewingLatestFull
                            ? `<button class="secondary" id="jumpToNewestFullRun">切到最新全量 Run</button>`
                            : ""
                        }
                      </div>
                    </div>
                    <div class="tags">
                      ${
                        newestRun
                          ? `<span class="tag">最新 Run: ${escapeHtml(newestRun.run_id)}</span>`
                          : ""
                      }
                      ${
                        newestFullRun
                          ? `<span class="tag">最新全量 Run: ${escapeHtml(newestFullRun.run_id)}</span>`
                          : ""
                      }
                    </div>
                  </section>
                `
                : ""
            }
            <div class="grid-4">
              <div class="metric"><div class="metric-label">完成时间</div><div class="metric-value">${escapeHtml(formatTime(run.completed_at || run.generated_at || run.started_at))}</div></div>
              <div class="metric"><div class="metric-label">Status</div><div class="metric-value">${escapeHtml(run.status || "-")}</div></div>
              <div class="metric"><div class="metric-label">Cases</div><div class="metric-value">${escapeHtml(run.case_count ?? "-")}</div></div>
              <div class="metric"><div class="metric-label">Final Avg</div><div class="metric-value">${formatPercent(run.final_avg)}</div></div>
            </div>
            <div class="split">
              <section class="panel">
                <div class="panel-header">
                  <div>
                    <h2 class="panel-title">Case Results</h2>
                    <p class="panel-subtitle">${run.case_count} 个 case，当前 Run 范围：${escapeHtml(runScopeLabel(run))}。点击任意一条查看详细评估与后端 trace。</p>
                    <p class="panel-subtitle">Run ID: ${escapeHtml(run.run_id)}</p>
                    <p class="panel-subtitle">${escapeHtml(runPromptfooLabel(run))}</p>
                  </div>
                </div>
                <div class="table">${resultList || '<div class="empty">当前 run 没有 case 结果。</div>'}</div>
                <div class="detail-block">
                  <h4>Runner Log</h4>
                  <pre>${escapeHtml(cleanedLog || "暂无可读 runner log")}</pre>
                  <details>
                    <summary>查看原始 Runner Log</summary>
                    <pre>${escapeHtml(run.log || "暂无 runner.log")}</pre>
                  </details>
                </div>
              </section>
              <section class="stack">
                <section class="panel">
                  ${
                    caseResult
                      ? `
                        <div class="panel-header">
                          <div>
                            <h2 class="panel-title">${escapeHtml(caseResult.title || caseResult.case_id)}</h2>
                            <p class="panel-subtitle">${escapeHtml(caseResult.case_id)} · ${escapeHtml(caseResult.skill_name || "-")}</p>
                          </div>
                        </div>
                        <div class="grid-3">
                          <div class="mini-card panel"><div class="metric-label">HardAssert</div><div class="metric-value">${Number(caseResult.hard_assert_score ?? 0).toFixed(2)}</div></div>
                          <div class="mini-card panel"><div class="metric-label">Judge</div><div class="metric-value">${formatJudge(caseJudge.score)}</div></div>
                          <div class="mini-card panel"><div class="metric-label">FinalEval</div><div class="metric-value">${formatPercent(caseFinalScore)}</div></div>
                        </div>
                        <div class="detail-block">
                          <h4>Evaluation Status</h4>
                          <div class="tags">
                            <span class="tag ${statusClass(resolveCaseStatus(caseResult))}">${escapeHtml(resolveCaseStatus(caseResult))}</span>
                            ${caseResult.error_type ? `<span class="tag">${escapeHtml(caseResult.error_type)}</span>` : ""}
                            <span class="tag ${verdictClass(caseJudge.verdict)}">${escapeHtml(caseJudge.verdict || "-")}</span>
                            <span class="tag">${escapeHtml(trace.kind)}</span>
                          </div>
                        </div>
                        <div class="detail-block">
                          <h4>Judge Summary</h4>
                          <p>${escapeHtml(caseJudge.summary || "-")}</p>
                        </div>
                        <div class="detail-block">
                          <h4>优点</h4>
                          ${
                            caseJudge?.strengths?.length
                              ? `<ul>${caseJudge.strengths.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
                              : '<p class="muted">无。</p>'
                          }
                        </div>
                        <div class="detail-block">
                          <h4>问题</h4>
                          ${
                            caseJudge?.issues?.length
                              ? `<ul>${caseJudge.issues.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
                              : '<p class="muted">无。</p>'
                          }
                        </div>
                        ${
                          caseResult.error
                            ? `
                              <div class="detail-block">
                                <h4>Error</h4>
                                <pre>${escapeHtml(JSON.stringify(caseResult.error, null, 2))}</pre>
                              </div>
                            `
                            : ""
                        }
                        <div class="detail-block">
                          <h4>Final Answer</h4>
                          <div class="markdown answer">${renderMarkdown(caseResult.final_answer || "")}</div>
                        </div>
                        <div class="detail-block">
                          <h4>Case Original</h4>
                          <div class="markdown">${renderMarkdown(caseResult.case_snapshot?.entry_question || "")}</div>
                          <div class="markdown">${renderMarkdown(caseResult.case_snapshot?.body || caseResult.case_snapshot?.summary || "")}</div>
                        </div>
                        <div class="detail-block">
                          <h4>Judge Rubric</h4>
                          <div class="markdown">${renderMarkdown(caseResult.case_snapshot?.judge_rubric || "-")}</div>
                        </div>
                        <div class="detail-block">
                          <h4>Driver Signals</h4>
                          ${
                            caseResult.runner_warnings?.length || caseResult.unexpected_asks?.length || caseResult.unused_script_steps?.length
                              ? `
                                ${caseResult.runner_warnings?.length ? `<details open><summary>Runner Warnings</summary><pre>${escapeHtml(JSON.stringify(caseResult.runner_warnings, null, 2))}</pre></details>` : ""}
                                ${caseResult.unexpected_asks?.length ? `<details><summary>Unexpected Asks</summary><pre>${escapeHtml(JSON.stringify(caseResult.unexpected_asks, null, 2))}</pre></details>` : ""}
                                ${caseResult.unused_script_steps?.length ? `<details><summary>Unused Script Steps</summary><pre>${escapeHtml(JSON.stringify(caseResult.unused_script_steps, null, 2))}</pre></details>` : ""}
                              `
                              : '<p class="muted">无额外 driver 信号。</p>'
                          }
                        </div>
                        <div class="detail-block">
                          <h4>Simulated User Trace</h4>
                          ${
                            caseResult.simulated_user_trace?.length
                              ? caseResult.simulated_user_trace
                                  .map(
                                    (item, index) => `
                                      <details ${index === 0 ? "open" : ""}>
                                        <summary>Turn ${item.turn} · ${escapeHtml(item.source || "simulated_user")} · ${escapeHtml(item.slot || "-")}</summary>
                                        <pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre>
                                      </details>
                                    `
                                  )
                                  .join("")
                              : '<p class="muted">该 case 没有 simulated user 交互。</p>'
                          }
                        </div>
                        <div class="detail-block">
                          <h4>Case Snapshot</h4>
                          <pre>${escapeHtml(JSON.stringify(caseResult.case_snapshot || {}, null, 2))}</pre>
                        </div>
                        <div class="detail-block">
                          <h4>Request / Response Payload</h4>
                          <details>
                            <summary>Request Payload</summary>
                            <pre>${escapeHtml(JSON.stringify(caseResult.request_payload || caseResult.request_payloads || {}, null, 2))}</pre>
                          </details>
                          <details>
                            <summary>Response Payload</summary>
                            <pre>${escapeHtml(JSON.stringify(caseResult.response_payload || caseResult.response_payloads || {}, null, 2))}</pre>
                          </details>
                        </div>
                        <div class="detail-block">
                          <h4>Transcript</h4>
                          ${
                            caseResult.transcript?.length
                              ? caseResult.transcript
                                  .map(
                                    (turn) => `
                                      <details>
                                        <summary>Turn ${turn.turn} · ${escapeHtml(turn.status || "-")} · ${escapeHtml(turn.response_id || "-")}</summary>
                                        <pre>${escapeHtml(JSON.stringify(turn, null, 2))}</pre>
                                      </details>
                                    `
                                  )
                                  .join("")
                              : '<div class="empty">没有 transcript。</div>'
                          }
                        </div>
                      `
                      : '<div class="empty">请选择一个 CaseResult 查看详情。</div>'
                  }
                </section>
                <section class="panel">
                  <div class="panel-header">
                    <div>
                      <h2 class="panel-title">Backend Trace</h2>
                      <p class="panel-subtitle">展示后端 session / turns / tool calls / chart / SQL 痕迹。</p>
                    </div>
                  </div>
                  ${
                    caseResult?.backend_session_ids?.length
                      ? `
                        <div class="tags">
                          ${caseResult.backend_session_ids
                            .map(
                              (sessionId) => `
                                <button class="${sessionId === state.selectedBackendSessionId ? "" : "ghost"}" data-session-id="${escapeHtml(sessionId)}">
                                  ${escapeHtml(sessionId)}
                                </button>
                              `
                            )
                            .join("")}
                        </div>
                      `
                      : '<p class="muted">该 case 未产生可追踪后端会话。</p>'
                  }
                  ${
                    state.backendTrace
                      ? `
                        <div class="grid-4" style="margin-top:16px;">
                          <div class="mini-card panel"><div class="metric-label">Turns</div><div class="metric-value">${trace.turns.length}</div></div>
                          <div class="mini-card panel"><div class="metric-label">Tool Calls</div><div class="metric-value">${trace.toolCalls.length}</div></div>
                          <div class="mini-card panel"><div class="metric-label">Charts</div><div class="metric-value">${trace.chartCalls.length}</div></div>
                          <div class="mini-card panel"><div class="metric-label">SQL</div><div class="metric-value">${trace.sqlCalls.length}</div></div>
                        </div>
                        <div class="detail-block">
                          <h4>Trace Summary</h4>
                          <div class="tags">
                            <span class="tag">${escapeHtml(trace.kind)}</span>
                            <span class="tag">${trace.cardCalls.length} card ops</span>
                            <span class="tag">${trace.exportCalls.length} exports</span>
                          </div>
                        </div>
                        <div class="detail-block">
                          <h4>Tool Calls</h4>
                          ${
                            trace.toolCalls.length
                              ? trace.toolCalls
                                  .map(
                                    (item, index) => `
                                      <details>
                                        <summary>${index + 1}. ${escapeHtml(item.name || "-")}</summary>
                                        <pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre>
                                      </details>
                                    `
                                  )
                                  .join("")
                              : '<div class="empty">没有 tool call 痕迹。</div>'
                          }
                        </div>
                        <div class="detail-block">
                          <h4>Response Turns</h4>
                          ${
                            trace.turns.length
                              ? trace.turns
                                  .map(
                                    (turn) => `
                                      <details>
                                        <summary>${escapeHtml(turn.response_id || "-")} · ${escapeHtml(turn.status || "-")}</summary>
                                        <pre>${escapeHtml(JSON.stringify(turn, null, 2))}</pre>
                                      </details>
                                    `
                                  )
                                  .join("")
                              : '<div class="empty">暂无 turns 数据。</div>'
                          }
                        </div>
                      `
                      : '<div class="empty">选择一个 backend session 后会在这里显示 trace。</div>'
                  }
                </section>
              </section>
            </div>
          `
          : '<div class="empty">当前 target 还没有可查看的 run。先去 Cases 运行一个 case，或者进入 Run History 选择历史 run。</div>'
      }
    </section>
  `;
}

function renderRunDetailView() {
  const active = activeRunForTarget();
  if (isActiveRunSelected() && active) {
    return renderActiveRunDetailView(active);
  }
  return renderArchivedRunDetailView(state.runDetail);
}

function bindGlobalEvents() {
  $("refreshAll").onclick = refreshAll;
  $("targetPicker").onchange = async (event) => {
    state.selectedTargetId = event.target.value;
    state.selectedRunId = "";
    state.selectedResultCaseId = "";
    await loadTargetData();
    renderApp();
  };

  document.querySelectorAll(".navtab").forEach((tab) => {
    tab.onclick = () => {
      state.view = tab.dataset.view;
      renderApp();
    };
  });
}

function bindViewEvents() {
  document.querySelectorAll("[data-target-action]").forEach((button) => {
    button.onclick = async () => {
      state.selectedTargetId = button.dataset.targetId;
      await loadTargetData();
      state.view = button.dataset.targetAction === "cases" ? "cases" : "runs";
      renderApp();
    };
  });

  const caseSearchInput = $("caseSearchInput");
  if (caseSearchInput) {
    caseSearchInput.oninput = (event) => {
      state.caseSearch = event.target.value;
      renderApp();
    };
  }

  const caseTagFilter = $("caseTagFilter");
  if (caseTagFilter) {
    caseTagFilter.value = state.caseTag;
    caseTagFilter.onchange = (event) => {
      state.caseTag = event.target.value;
      renderApp();
    };
  }

  const caseModeFilter = $("caseModeFilter");
  if (caseModeFilter) {
    caseModeFilter.value = state.caseMode;
    caseModeFilter.onchange = (event) => {
      state.caseMode = event.target.value;
      renderApp();
    };
  }

  document.querySelectorAll("[data-case-id]").forEach((card) => {
    card.onclick = () => {
      state.selectedCaseId = card.dataset.caseId;
      renderApp();
    };
  });

  const debugSelectedCaseButton = $("debugSelectedCase");
  if (debugSelectedCaseButton) {
    debugSelectedCaseButton.onclick = async () => {
      if (!state.selectedCaseId) return;
      await startRun({ target: state.selectedTargetId, case: state.selectedCaseId });
    };
  }

  const runFilteredCasesButton = $("runFilteredCases");
  if (runFilteredCasesButton) {
    runFilteredCasesButton.onclick = async () => {
      if (state.caseTag) {
        await startRun({ target: state.selectedTargetId, tag: state.caseTag });
        return;
      }
      window.alert("当前没有选择 tag。批量 Run 目前支持按 tag 或当前 Target 全量。");
    };
  }

  const runAllCasesButton = $("runAllCases");
  if (runAllCasesButton) {
    runAllCasesButton.onclick = async () => {
      await startRun({ target: state.selectedTargetId });
    };
  }

  const jumpToNewestRun = $("jumpToNewestRun");
  if (jumpToNewestRun) {
    jumpToNewestRun.onclick = async () => {
      const newest = latestRun();
      if (!newest) return;
      await loadRunDetail(newest.run_id);
      renderApp();
    };
  }

  const jumpToNewestFullRun = $("jumpToNewestFullRun");
  if (jumpToNewestFullRun) {
    jumpToNewestFullRun.onclick = async () => {
      const newest = latestFullRun();
      if (!newest) return;
      await loadRunDetail(newest.run_id);
      renderApp();
    };
  }

  document.querySelectorAll("[data-run-id]").forEach((row) => {
    row.onclick = async () => {
      const runId = row.dataset.runId;
      state.selectedRunId = runId;
      if (runId === ACTIVE_RUN_ID) {
        state.runDetail = null;
        state.selectedResultCaseId = "";
        state.caseResult = null;
        state.backendTrace = null;
        state.selectedBackendSessionId = "";
      } else {
        await loadRunDetail(runId);
      }
      renderApp();
    };
  });

  document.querySelectorAll("[data-open-run-detail]").forEach((button) => {
    button.onclick = async (event) => {
      event.stopPropagation();
      const runId = button.dataset.openRunDetail;
      state.selectedRunId = runId;
      if (runId === ACTIVE_RUN_ID) {
        state.runDetail = null;
        state.selectedResultCaseId = "";
        state.caseResult = null;
        state.backendTrace = null;
        state.selectedBackendSessionId = "";
      } else {
        await loadRunDetail(runId);
      }
      state.view = "run-detail";
      renderApp();
    };
  });

  document.querySelectorAll("[data-result-case-id]").forEach((row) => {
    row.onclick = async () => {
      await loadCaseResult(row.dataset.resultCaseId);
      renderApp();
    };
  });

  document.querySelectorAll("[data-active-case-id]").forEach((row) => {
    row.onclick = () => {
      state.selectedResultCaseId = row.dataset.activeCaseId;
      renderApp();
    };
  });

  document.querySelectorAll("[data-session-id]").forEach((button) => {
    button.onclick = async () => {
      state.selectedBackendSessionId = button.dataset.sessionId;
      await loadBackendTrace();
      renderApp();
    };
  });
}

function renderApp() {
  renderNav();
  renderTargetPicker();
  renderStatusBar();

  const viewRoot = $("viewRoot");
  if (state.view === "targets") {
    viewRoot.innerHTML = renderTargetsView();
  } else if (state.view === "cases") {
    viewRoot.innerHTML = renderCasesView();
  } else if (state.view === "runs") {
    viewRoot.innerHTML = renderRunsView();
  } else {
    viewRoot.innerHTML = renderRunDetailView();
  }

  bindGlobalEvents();
  bindViewEvents();
}

async function bootstrap() {
  await refreshAll();
  if (state.runState?.running) {
    pollRunState();
  }
}

bootstrap().catch((error) => {
  $("viewRoot").innerHTML = `<div class="empty">Dashboard 初始化失败：${escapeHtml(error.message || error)}</div>`;
});
