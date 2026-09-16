/** Page entry point: state, polling and event wiring.
 *
 * Rendering lives in ./matcher/*; this file decides *when* it happens.
 */
import { $, element, message, listMessages } from "./matcher/dom";
import { post, request } from "./matcher/client";
import { ResultsView, setLoginBrowserOpen } from "./matcher/results";
import { renderResume } from "./matcher/resume-panel";
import {
  type CrawlState, type Ranking, type Status,
  normalizeRanking, normalizeResume, normalizeStatus, number, object, string,
} from "./matcher/wire";

const STATE_NAMES: Record<CrawlState, string> = {
  idle: "尚未抓取", login_required: "等待学校登录", running: "正在抓取",
  completed: "抓取已结束", failed: "抓取未完成", cancelled: "抓取已停止",
};
const ACTIVE_POLL_MS = 2500;
const IDLE_POLL_MS = 6000;
/** One frame's grace, so a burst of keystrokes filters once rather than per key. */
const SEARCH_DEBOUNCE_MS = 80;
const PREFERENCE_INPUTS = ["target-roles", "locations", "exclude-keywords", "max-term-months",
                           "exclude-restricted-eligibility", "ranking-engine", "ai-shortlist"];
const LOCKED_WHILE_BUSY = ["max-pages", "crawl-delay", ...PREFERENCE_INPUTS];

const results = new ResultsView();
let currentStatus: Status | null = null;
let connected = false;
let actionPending = false;
let polling = false;
let timer: ReturnType<typeof setTimeout> | undefined;
let searchTimer: ReturnType<typeof setTimeout> | undefined;
let loadedAIRun = -1;
/** A ranking that outlived a service restart is loaded once, not every poll. */
let restoredRanking = false;

const aiRunning = (): boolean => currentStatus?.ai.state === "running";

function updateControls(): void {
  const running = currentStatus?.crawl.state === "running" || aiRunning();
  const unavailable = !connected || actionPending;
  const usesAI = $<HTMLSelectElement>("ranking-engine").value === "deepseek";
  $("upload-button").toggleAttribute("disabled", unavailable || running || !$("resume-file").matches(":valid"));
  $("browser-button").toggleAttribute("disabled", unavailable || running);
  $("crawl-button").toggleAttribute("disabled", unavailable || running);
  $("cancel-button").hidden = currentStatus?.crawl.state !== "running";
  $("cancel-button").toggleAttribute("disabled", unavailable);
  $("import-button").toggleAttribute("disabled", unavailable || running || !$("jobs-file").hasAttribute("data-selected"));
  $("rank-button").toggleAttribute("disabled", unavailable || running || !currentStatus?.has_resume
    || !currentStatus.job_count || (usesAI && currentStatus?.ai_config.configured !== true));
  $("ai-cancel").hidden = !aiRunning();
  $("ai-cancel").toggleAttribute("disabled", unavailable);
  $("export-button").toggleAttribute("disabled", unavailable || !results.current);
  $("resume-file").toggleAttribute("disabled", actionPending || running);
  $("jobs-file").toggleAttribute("disabled", actionPending || running);
  for (const id of LOCKED_WHILE_BUSY) $(id).toggleAttribute("disabled", actionPending || running);
}

function renderStatus(status: Status): void {
  $("ai-config").textContent = status.ai_config.configured
    ? `已配置模型：${string(status.ai_config.model)}`
    : string(status.ai_config.message) || "服务尚未配置 DeepSeek。";
  $("embedding-status").textContent = status.embeddings.available === true
    ? `语义检索已启用（本地模型 ${string(status.embeddings.model)}，不联网、不计费）。`
    : "未安装本地嵌入模型，检索将只用 BM25 词频；语义不同但相关的岗位可能被低估。"
      + "安装：pip install -e './matcher[embeddings]'";
  const rerank = object(status.rerank);
  $("rerank-status").textContent = rerank.available === true
    ? `本地重排已启用（${string(rerank.model)}，对检索靠前的 ${number(rerank.pool)} 个职位逐对精算，不联网、不计费）。`
    : "未安装本地重排模型，候选顺序仅来自检索分数。";

  $("ai-progress").hidden = !status.ai.state || status.ai.state === "idle";
  $("ai-message").textContent = string(status.ai.message);
  const usage = object(status.ai.usage);
  $("ai-usage").textContent = usage.requests
    ? `本次 API 请求 ${number(usage.requests)} 次；输入 ${number(usage.prompt_tokens)} / 输出 ${number(usage.completion_tokens)} tokens。`
    : "";

  $("crawl-state").textContent = STATE_NAMES[status.crawl.state];
  $("crawl-count").textContent = String(status.crawl.job_count);
  $("crawl-pages").textContent = String(status.crawl.pages);
  $("crawl-progress").dataset.running = String(status.crawl.state === "running");
  $("crawl-message").textContent = status.crawl.message || "打开登录浏览器，进入职位列表后开始抓取。";
  listMessages("crawl-issues", [...new Set([...status.crawl.errors, ...status.crawl.warnings])]
    .filter(issue => issue !== status.crawl.message));

  $("total-count").textContent = String(status.job_count);
  const limited = !status.crawl.complete || status.crawl.warnings.length || status.crawl.errors.length;
  const collected = new Date(status.collected_at);
  const provenance = status.collected_at && Number.isFinite(collected.getTime())
    ? ` ${status.source === "import" ? "导入" : "采集"}时间：${collected.toLocaleString("zh-CN")}。` : "";
  message("coverage-note", status.crawl.state === "running"
    ? "正在收集职位。抓取结束或停止后，可对已成功收集的数据生成排名。"
    : status.job_count
      ? `${limited ? "当前数据可能不完整。" : "已完成本次可见列表的遍历。"}`
        + `排名仅覆盖本地收集的 ${status.job_count} 个职位，不保证涵盖 WaterlooWorks 的所有岗位。${provenance}`
        + `${status.crawl.state === "login_required" ? " 请在服务打开的浏览器完成登录。" : ""}`
      : "尚无职位数据。排名范围以本次成功收集到的职位为准。");

  setLoginBrowserOpen(object(status.browser).open === true);
  renderResume(status.resume, status.has_resume);
  updateControls();
}

async function refreshStatus(): Promise<void> {
  if (polling) return;
  polling = true;
  try {
    currentStatus = normalizeStatus(await request("/status"));
    if (currentStatus.ai.state === "idle") loadedAIRun = -1;
    connected = true;
    renderStatus(currentStatus);
    // Load a finished AI ranking exactly once per run, not on every poll.
    if (currentStatus.ai.state === "completed" && number(currentStatus.ai.run_id) !== loadedAIRun) {
      try {
        const ranking: Ranking = normalizeRanking(await request("/ai/result"));
        loadedAIRun = number(currentStatus.ai.run_id);
        restoredRanking = true;
        message("action-error", "");
        results.show(ranking);
      } catch (error) {
        message("action-error", error instanceof Error ? error.message : "AI 结果尚未就绪。");
      }
    }
    // The service keeps the last ranking across restarts. Show it once, so a
    // reload does not present an empty results pane next to a loaded resume.
    if (!restoredRanking && currentStatus.ai.state !== "completed"
        && object(currentStatus.ranking).available === true) {
      restoredRanking = true;
      try {
        results.show(normalizeRanking(await request("/ranking")));
      } catch {
        // Nothing stored after all; the empty pane is already correct.
      }
    }
  } catch {
    connected = false;
  } finally {
    polling = false;
    $("service-status").dataset.connected = String(connected);
    $("service-label").textContent = connected ? "本地服务已连接" : "本地服务未连接";
    $("service-help").hidden = connected;
    updateControls();
  }
}

async function poll(): Promise<void> {
  if (!document.hidden) await refreshStatus();
  clearTimeout(timer);
  const busy = currentStatus?.crawl.state === "running" || aiRunning();
  timer = setTimeout(() => { void poll(); }, busy ? ACTIVE_POLL_MS : IDLE_POLL_MS);
}

async function perform(buttonId: string, busyText: string, action: () => Promise<void>): Promise<void> {
  if (actionPending) return;
  actionPending = true;
  const button = $(buttonId);
  const originalText = button.textContent;
  button.textContent = busyText;
  message("action-error", "");
  message("action-message", "");
  updateControls();
  try {
    await action();
  } catch (error) {
    message("action-error", error instanceof Error ? error.message : "操作失败，请查看本地服务日志。");
  } finally {
    actionPending = false;
    button.textContent = originalText;
    await refreshStatus();
    updateControls();
  }
}

function clearRanking(reason?: string): void {
  results.clear(reason);
  updateControls();
}

const preferences = (): Record<string, string[] | boolean | number | null> => {
  const split = (id: string) => $<HTMLInputElement>(id).value
    .split(/[,，\n]/).map(value => value.trim()).filter(Boolean);
  const term = $<HTMLInputElement>("max-term-months").value.trim();
  return {
    target_roles: split("target-roles"),
    locations: split("locations"),
    exclude_keywords: split("exclude-keywords"),
    exclude_restricted_eligibility: $<HTMLInputElement>("exclude-restricted-eligibility").checked,
    // Blank means "follow the resume"; the server reads null that way.
    max_term_months: term === "" ? null : Number(term),
  };
};

$("resume-file").addEventListener("change", updateControls);
$("jobs-file").addEventListener("change", () => {
  $("jobs-file").toggleAttribute("data-selected", Boolean($<HTMLInputElement>("jobs-file").files?.length));
  updateControls();
});

$("resume-form").addEventListener("submit", event => {
  event.preventDefault();
  void perform("upload-button", "解析中…", async () => {
    const file = $<HTMLInputElement>("resume-file").files?.[0];
    if (!file) throw new Error("请选择一份 PDF、DOCX 或 MD 简历。");
    if (!/\.(pdf|docx|md|markdown)$/i.test(file.name)) {
      throw new Error("简历必须是 PDF、DOCX 或 MD；旧版 .doc 请另存为 .docx。");
    }
    if (!file.size || file.size > 10 * 1024 * 1024) {
      throw new Error("请选择非空且不超过 10 MiB 的 PDF、DOCX 或 MD 简历。");
    }
    const form = new FormData();
    form.append("file", file);
    const resume = normalizeResume(await request("/resume", { method: "POST", body: form }));
    clearRanking("简历已更新。准备好职位数据后，重新生成匹配排名。");
    renderResume(resume, true);
    message("action-message", `已解析 ${resume.filename}，拆出 ${resume.experiences.length} 段经历，`
      + `其中 ${resume.skills.length} 项技能有经历佐证。请核对拆分结果。`);
  });
});

$("browser-button").addEventListener("click", () => void perform("browser-button", "打开中…", async () => {
  const result = object(await post("/browser"));
  message("action-message", string(result.message)
    || "浏览器已打开。请完成学校登录，并进入 Co-op Jobs 列表后开始抓取。");
}));

$("crawl-button").addEventListener("click", () => void perform("crawl-button", "正在启动…", async () => {
  const pages = Number($<HTMLInputElement>("max-pages").value);
  const delay = Number($<HTMLInputElement>("crawl-delay").value);
  if (!Number.isInteger(pages) || pages < 1 || pages > 500) throw new Error("抓取页数须为 1 至 500 的整数。");
  if (!Number.isFinite(delay) || delay < 0.5 || delay > 30) throw new Error("请求间隔须为 0.5 至 30 秒。");
  const result = object(await post("/crawl",
    { max_pages: pages, max_jobs: 3000, delay_seconds: delay, include_details: true }));
  clearRanking("正在收集职位。抓取结束或停止后，可生成申请优先级。");
  message("action-message", string(result.message) || "抓取已启动，请保持本地服务与浏览器运行。");
}));

$("cancel-button").addEventListener("click", () => void perform("cancel-button", "正在停止…", async () => {
  await post("/crawl/cancel");
  message("action-message", "已请求停止，当前请求结束后会保留已收集的职位。请等待状态更新。");
}));

$("import-button").addEventListener("click", () => void perform("import-button", "导入中…", async () => {
  const file = $<HTMLInputElement>("jobs-file").files?.[0];
  if (!file) throw new Error("请选择 JSON 文件。");
  if (!file.size || file.size > 10 * 1024 * 1024) throw new Error("请选择非空且不超过 10 MB 的 JSON 文件。");
  let parsed: unknown;
  try {
    parsed = JSON.parse(await file.text());
  } catch {
    throw new Error("文件不是有效的 JSON，请检查文件格式。");
  }
  const jobs = Array.isArray(parsed) ? parsed : object(parsed).jobs;
  if (!Array.isArray(jobs) || !jobs.length) {
    throw new Error("JSON 必须包含非空的职位数组，或包含 jobs 数组的对象。");
  }
  const result = object(await post("/jobs/import", { jobs }));
  clearRanking("职位数据已导入。上传简历后即可生成排名。");
  message("action-message", `已导入 ${number(result.job_count)} 个职位。`);
}));

$("rank-button").addEventListener("click", () => void perform("rank-button", "匹配中…", async () => {
  if ($<HTMLSelectElement>("ranking-engine").value === "deepseek") {
    const shortlist = Number($<HTMLInputElement>("ai-shortlist").value);
    if (!Number.isInteger(shortlist) || shortlist < 0 || shortlist > 10000) {
      throw new Error("候选数量必须为 0 至 10000 的整数。");
    }
    const term = $<HTMLInputElement>("max-term-months").value.trim();
    if (term !== "" && (!Number.isInteger(Number(term)) || Number(term) < 0 || Number(term) > 24)) {
      throw new Error("可接受的最长工期必须是 0 至 24 的整数，或留空跟随简历。");
    }
    const started = object(await post("/ai/rank", { limit: 100, preferences: preferences(), shortlist }));
    clearRanking(`本地检索已从 ${number(started.eligible)} 个合格职位中选出 `
      + `${number(started.shortlisted)} 个送入模型逐项核对，可在左侧查看进度或停止。`);
    return;
  }
  restoredRanking = true;
  const ranking = normalizeRanking(await post("/rank", { limit: 100, preferences: preferences() }));
  $<HTMLInputElement>("result-search").value = "";
  results.show(ranking);
  message("action-message", `已从 ${ranking.total_jobs} 个职位中生成 ${ranking.jobs.length} 个推荐，`
    + `${ranking.excluded_jobs.length} 个职位被排除。`);
}));

$("export-button").addEventListener("click", () => void perform("export-button", "导出中…", async () => {
  const response = await fetch("/matcher-api/export.csv");
  if (!response.ok) throw new Error("CSV 导出失败，请重新生成排名后重试。");
  const url = URL.createObjectURL(await response.blob());
  const link = element("a");
  link.href = url;
  link.download = "waterlooworks-top100.csv";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}));

$("ai-cancel").addEventListener("click", () => void perform("ai-cancel", "停止中…", async () => {
  await post("/ai/cancel");
}));

for (const id of PREFERENCE_INPUTS) {
  $(id).addEventListener("input", () => {
    if (results.current) clearRanking("申请偏好已更改。请重新生成排名以应用新偏好。");
    updateControls();
  });
}

$("result-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => results.filter(), SEARCH_DEBOUNCE_MS);
});

$("reconnect-button").addEventListener("click", () => void refreshStatus());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    clearTimeout(timer);
    void poll();
  }
});
void poll();
