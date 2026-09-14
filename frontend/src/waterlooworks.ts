type JsonObject = Record<string, unknown>;
type CrawlState = "idle" | "login_required" | "running" | "completed" | "failed" | "cancelled";
interface Resume { filename: string; skills: string[]; warnings: string[] }
interface Status {
  crawl: { state: CrawlState; message: string; job_count: number; pages: number; errors: string[]; warnings: string[]; complete: boolean };
  job_count: number;
  has_resume: boolean;
  resume: Resume | null;
  source: string;
  collected_at: string;
}
interface RankedJob {
  id: string; title: string; company: string; location: string; description: string;
  requirements: string; deadline: string; url: string; rank: number; score: number;
  matched_skills: string[]; missing_skills: string[]; reasons: string[]; warnings: string[];
  score_breakdown: JsonObject;
}
interface Ranking { jobs: RankedJob[]; total_jobs: number; eligible_jobs: number; excluded_jobs: JsonObject[]; method: string }

const $ = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing page element: ${id}`);
  return element as T;
};
const object = (value: unknown): JsonObject => value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
const string = (value: unknown): string => typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
const number = (value: unknown, fallback = 0): number => typeof value === "number" && Number.isFinite(value) ? value : fallback;
const strings = (value: unknown): string[] => Array.isArray(value) ? value.map(item => typeof item === "object" ? string(object(item).message) || JSON.stringify(item) : string(item)).filter(Boolean) : typeof value === "string" && value ? [value] : [];
const normalizeResume = (value: unknown): Resume => {
  const data = object(value);
  return { filename: string(data.filename), skills: strings(data.skills), warnings: strings(data.warnings) };
};
const normalizeStatus = (value: unknown): Status => {
  const data = object(value);
  const crawl = object(data.crawl);
  const state = string(crawl.state);
  return {
    crawl: { state: (["idle", "login_required", "running", "completed", "failed", "cancelled"].includes(state) ? state : "idle") as CrawlState,
      message: string(crawl.message), job_count: number(crawl.job_count), pages: number(crawl.pages), errors: strings(crawl.errors), warnings: strings(crawl.warnings), complete: crawl.complete === true },
    job_count: number(data.job_count), has_resume: data.has_resume === true,
    resume: data.resume ? normalizeResume(data.resume) : null,
    source: string(data.source), collected_at: string(data.collected_at),
  };
};
const normalizeRanking = (value: unknown): Ranking => {
  const data = object(value);
  if (!Array.isArray(data.jobs)) throw new Error("服务返回的排名数据格式不正确，请检查本地服务日志。");
  return {
    jobs: data.jobs.map((value, index) => {
      const job = object(value);
      return { id: string(job.id), title: string(job.title) || "未提供职位名称", company: string(job.company) || "未提供公司名称",
        location: strings(job.location).join(", "), description: string(job.description), requirements: strings(job.requirements).join("\n"),
        deadline: string(job.deadline), url: string(job.url), rank: number(job.rank, index + 1), score: number(job.score),
        matched_skills: strings(job.matched_skills), missing_skills: strings(job.missing_skills), reasons: strings(job.reasons), warnings: strings(job.warnings), score_breakdown: object(job.score_breakdown) };
    }),
    total_jobs: number(data.total_jobs), eligible_jobs: number(data.eligible_jobs),
    excluded_jobs: Array.isArray(data.excluded_jobs) ? data.excluded_jobs.map(object) : [], method: string(data.method),
  };
};

let currentStatus: Status | null = null;
let ranking: Ranking | null = null;
let connected = false;
let actionPending = false;
let polling = false;
let timer: ReturnType<typeof setTimeout> | undefined;
const stateNames: Record<CrawlState, string> = { idle: "尚未抓取", login_required: "等待学校登录", running: "正在抓取", completed: "抓取已结束", failed: "抓取未完成", cancelled: "抓取已停止" };

async function request(path: string, options: RequestInit = {}): Promise<unknown> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120_000);
  try {
    const headers = new Headers(options.headers);
    if (options.method && options.method !== "GET") headers.set("X-SWW-Client", "1");
    const response = await fetch(`/matcher-api${path}`, { ...options, headers, signal: controller.signal });
    let payload: unknown;
    try { payload = await response.json(); } catch { throw new Error("本地服务没有返回有效 JSON。请确认匹配服务正在运行且 /matcher-api 代理正确。"); }
    if (!response.ok) {
      const data = object(payload);
      const detail = data.detail ?? data.error ?? data.message;
      throw new Error(typeof detail === "string" ? detail : strings(detail).join("；") || `请求失败（HTTP ${response.status}）`);
    }
    return payload;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw new Error("请求等待超时。请查看本地服务状态后重试。");
    if (error instanceof TypeError) throw new Error("无法连接本地服务，请确认已运行 ./scripts/start-matcher.sh。");
    throw error;
  } finally { clearTimeout(timeout); }
}
const post = (path: string, data: unknown = {}): Promise<unknown> => request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text = ""): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
function message(id: string, text: string): void { $(id).textContent = text; $(id).hidden = !text; }
function listMessages(id: string, items: string[]): void {
  const list = $(id);
  list.replaceChildren(...items.map(item => element("li", "", item)));
  list.hidden = items.length === 0;
}
function chips(items: string[], missing = false): HTMLElement {
  const container = element("div", "skill-list");
  container.append(...items.map(skill => element("span", `skill-chip${missing ? " missing" : ""}`, skill)));
  return container;
}
function renderResume(resume: Resume | null): void {
  $("resume-summary").hidden = !resume;
  $("resume-indicator").textContent = currentStatus?.has_resume ? "已解析" : "待上传";
  $("resume-indicator").dataset.ready = String(currentStatus?.has_resume === true);
  if (!resume) return;
  $("resume-filename").textContent = resume.filename;
  $("resume-skills").replaceChildren(...(resume.skills.length ? resume.skills.map(skill => element("span", "skill-chip", skill)) : [element("span", "small-label", "未识别到技能标签；请检查 PDF 文本是否完整。") ]));
  listMessages("resume-warnings", resume.warnings);
}
function updateControls(): void {
  const running = currentStatus?.crawl.state === "running";
  const unavailable = !connected || actionPending;
  $("upload-button").toggleAttribute("disabled", unavailable || running || !$("resume-file").matches(":valid"));
  $("browser-button").toggleAttribute("disabled", unavailable || running);
  $("crawl-button").toggleAttribute("disabled", unavailable || running);
  $("cancel-button").hidden = !running;
  $("cancel-button").toggleAttribute("disabled", unavailable);
  $("import-button").toggleAttribute("disabled", unavailable || running || !$("jobs-file").hasAttribute("data-selected"));
  $("rank-button").toggleAttribute("disabled", unavailable || running || !currentStatus?.has_resume || !currentStatus.job_count);
  $("export-button").toggleAttribute("disabled", unavailable || !ranking);
  $("resume-file").toggleAttribute("disabled", actionPending || running);
  $("jobs-file").toggleAttribute("disabled", actionPending || running);
  for (const id of ["max-pages", "crawl-delay", "target-roles", "locations", "exclude-keywords"]) $(id).toggleAttribute("disabled", actionPending || running);
}
function renderStatus(status: Status): void {
  $("crawl-state").textContent = stateNames[status.crawl.state];
  $("crawl-count").textContent = String(status.crawl.job_count);
  $("crawl-pages").textContent = String(status.crawl.pages);
  $("crawl-progress").dataset.running = String(status.crawl.state === "running");
  $("crawl-message").textContent = status.crawl.message || "打开登录浏览器，进入职位列表后开始抓取。";
  listMessages("crawl-issues", [...status.crawl.errors, ...status.crawl.warnings]);
  $("total-count").textContent = String(status.job_count);
  const limited = !status.crawl.complete || status.crawl.warnings.length || status.crawl.errors.length;
  const collected = new Date(status.collected_at);
  const provenance = status.collected_at && Number.isFinite(collected.getTime()) ? ` ${status.source === "import" ? "导入" : "采集"}时间：${collected.toLocaleString("zh-CN")}。` : "";
  message("coverage-note", status.crawl.state === "running"
    ? "正在收集职位。抓取结束或停止后，可对已成功收集的数据生成排名。"
    : status.job_count
      ? `${limited ? "当前数据可能不完整。" : "已完成本次可见列表的遍历。"}排名仅覆盖本地收集的 ${status.job_count} 个职位，不保证涵盖 WaterlooWorks 的所有岗位。${provenance}${status.crawl.state === "login_required" ? " 请在服务打开的浏览器完成登录。" : ""}`
      : "尚无职位数据。排名范围以本次成功收集到的职位为准。");
  renderResume(status.resume);
  updateControls();
}
async function refreshStatus(): Promise<void> {
  if (polling) return;
  polling = true;
  try {
    currentStatus = normalizeStatus(await request("/status"));
    connected = true;
    renderStatus(currentStatus);
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
  timer = setTimeout(() => { void poll(); }, currentStatus?.crawl.state === "running" ? 2500 : 6000);
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
  try { await action(); }
  catch (error) { message("action-error", error instanceof Error ? error.message : "操作失败，请查看本地服务日志。"); }
  finally {
    actionPending = false;
    button.textContent = originalText;
    await refreshStatus();
    updateControls();
  }
}
function clearRanking(reason?: string): void {
  ranking = null;
  $("job-results").replaceChildren();
  $("eligible-count").textContent = "—";
  $("ranked-count").textContent = "—";
  $("results-toolbar").hidden = true;
  $("results-count").hidden = true;
  $("ranking-details").hidden = true;
  $("results-empty").hidden = false;
  $("empty-message").textContent = reason || "完成左侧的简历上传和职位抓取，即可生成你的申请优先级。";
  updateControls();
}
function safeJobUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "waterlooworks.uwaterloo.ca" && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
function renderJob(job: RankedJob): HTMLLIElement {
  const item = element("li", "job-result");
  item.value = job.rank;
  const top = element("div", "job-topline");
  top.append(element("span", "job-rank tabular", String(job.rank).padStart(2, "0")));
  const identity = element("div");
  identity.append(element("p", "job-company", job.company));
  const title = element("h3", "job-title");
  const url = safeJobUrl(job.url);
  if (url) {
    const link = element("a", "", job.title);
    link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
    title.append(link);
  } else title.textContent = job.title;
  identity.append(title, element("p", "job-meta", [job.location || "地点未提供", job.deadline ? `截止 ${job.deadline}` : "截止日期待确认", job.id ? `#${job.id}` : ""].filter(Boolean).join(" · ")));
  top.append(identity);
  const score = element("div", "job-score");
  score.append(element("strong", "tabular", job.score.toFixed(1)), element("span", "", "匹配分 / 100"));
  top.append(score);
  item.append(top);
  const body = element("div", "job-body");
  if (job.matched_skills.length) body.append(chips(job.matched_skills));
  if (job.reasons.length) {
    const reasons = element("ul", "job-reasons");
    reasons.append(...job.reasons.map(reason => element("li", "", reason)));
    body.append(reasons);
  }
  const details = element("details", "job-detail");
  details.append(element("summary", "", "查看技能缺口、评分与职位详情"));
  details.append(element("h4", "", "简历中未识别到的职位技能"));
  details.append(job.missing_skills.length ? chips(job.missing_skills, true) : element("p", "", "未发现额外技能缺口。这不代表已满足所有申请条件。"));
  const breakdown = Object.entries(job.score_breakdown);
  if (breakdown.length) {
    details.append(element("h4", "", "评分明细"));
    const dl = element("dl", "score-breakdown");
    for (const [key, value] of breakdown) {
      const pair = element("div");
      pair.append(element("dt", "", `${key}:`), element("dd", "tabular", typeof value === "number" ? value.toFixed(2) : string(value) || JSON.stringify(value)));
      dl.append(pair);
    }
    details.append(dl);
  }
  if (job.warnings.length) {
    const warnings = element("ul", "warning-list");
    warnings.append(...job.warnings.map(warning => element("li", "", warning)));
    details.append(warnings);
  }
  details.append(element("h4", "", "职位要求"), element("p", "job-text", job.requirements || "未单独提取到要求，请查看职位描述与原页面。"));
  details.append(element("h4", "", "职位描述"), element("p", "job-text", job.description || "暂无详情文本，当前排名仅依据已收集的字段。请查看原职位。"));
  if (url) {
    const link = element("a", "job-link", "在 WaterlooWorks 查看并申请 ↗");
    link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
    details.append(link);
  } else details.append(element("p", "field-hint", "未获得有效的 WaterlooWorks 职位链接，请在平台内按职位编号或名称搜索。"));
  body.append(details); item.append(body);
  return item;
}
function renderResults(): void {
  if (!ranking) return;
  const query = $("result-search") as HTMLInputElement;
  const needle = query.value.trim().toLowerCase();
  const jobs = ranking.jobs.filter(job => `${job.title} ${job.company} ${job.location} ${job.matched_skills.join(" ")}`.toLowerCase().includes(needle));
  $("job-results").replaceChildren(...jobs.map(renderJob));
  $("results-empty").hidden = jobs.length > 0;
  $("empty-message").textContent = ranking.jobs.length ? "没有符合搜索条件的推荐，试试其他关键词。" : "当前偏好下没有可推荐的职位。请检查排除关键词、数据完整性与截止日期。";
  message("results-count", `显示 ${jobs.length} / ${ranking.jobs.length} 个推荐；筛选不会改变原始申请优先级。`);
  $("results-toolbar").hidden = ranking.jobs.length === 0;
  $("eligible-count").textContent = String(ranking.eligible_jobs);
  $("ranked-count").textContent = String(ranking.jobs.length);
  $("ranking-details").hidden = false;
  $("ranking-method").textContent = ranking.method || "本地文本匹配。请结合原始职位要求判断。";
  listMessages("excluded-jobs", ranking.excluded_jobs.map(job => `${string(job.title) || string(job.id)}：${string(job.reason) || "已排除"}`));
}
const preferences = (): Record<string, string[]> => {
  const split = (id: string) => ($<HTMLInputElement>(id).value).split(/[,，\n]/).map(value => value.trim()).filter(Boolean);
  return { target_roles: split("target-roles"), locations: split("locations"), exclude_keywords: split("exclude-keywords") };
};

$("resume-file").addEventListener("change", updateControls);
$("jobs-file").addEventListener("change", () => { $("jobs-file").toggleAttribute("data-selected", Boolean($<HTMLInputElement>("jobs-file").files?.length)); updateControls(); });
$("resume-form").addEventListener("submit", event => {
  event.preventDefault();
  void perform("upload-button", "解析中…", async () => {
    const file = $<HTMLInputElement>("resume-file").files?.[0];
    if (!file) throw new Error("请选择一份 PDF 简历。");
    if (!file.name.toLowerCase().endsWith(".pdf")) throw new Error("简历必须是 PDF 文件。");
    if (!file.size || file.size > 10 * 1024 * 1024) throw new Error("请选择非空且小于或等于 10 MB 的 PDF。");
    const form = new FormData(); form.append("file", file);
    const resume = normalizeResume(await request("/resume", { method: "POST", body: form }));
    clearRanking("简历已更新。准备好职位数据后，重新生成匹配排名。");
    renderResume(resume);
    message("action-message", `已解析 ${resume.filename}，识别到 ${resume.skills.length} 项技能。`);
  });
});
$("browser-button").addEventListener("click", () => void perform("browser-button", "打开中…", async () => {
  const result = object(await post("/browser"));
  message("action-message", string(result.message) || "浏览器已打开。请完成学校登录，并进入 Co-op Jobs 列表后开始抓取。");
}));
$("crawl-button").addEventListener("click", () => void perform("crawl-button", "正在启动…", async () => {
  const pages = $<HTMLInputElement>("max-pages"), delay = $<HTMLInputElement>("crawl-delay");
  if (!Number.isInteger(Number(pages.value)) || Number(pages.value) < 1 || Number(pages.value) > 500) throw new Error("抓取页数须为 1 至 500 的整数。");
  if (!Number.isFinite(Number(delay.value)) || Number(delay.value) < 2 || Number(delay.value) > 30) throw new Error("请求间隔须为 2 至 30 秒。");
  const result = object(await post("/crawl", { max_pages: Number(pages.value), max_jobs: 3000, delay_seconds: Number(delay.value), include_details: true }));
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
  try { parsed = JSON.parse(await file.text()); } catch { throw new Error("文件不是有效的 JSON，请检查文件格式。"); }
  const jobs = Array.isArray(parsed) ? parsed : object(parsed).jobs;
  if (!Array.isArray(jobs) || !jobs.length) throw new Error("JSON 必须包含非空的职位数组，或包含 jobs 数组的对象。");
  const result = object(await post("/jobs/import", { jobs }));
  clearRanking("职位数据已导入。上传简历后即可生成排名。");
  message("action-message", `已导入 ${number(result.job_count)} 个职位。`);
}));
$("rank-button").addEventListener("click", () => void perform("rank-button", "匹配中…", async () => {
  ranking = normalizeRanking(await post("/rank", { limit: 100, preferences: preferences() }));
  $<HTMLInputElement>("result-search").value = "";
  renderResults();
  message("action-message", `已从 ${ranking.total_jobs} 个职位中生成 ${ranking.jobs.length} 个推荐，${ranking.excluded_jobs.length} 个职位被排除。`);
}));
$("export-button").addEventListener("click", () => void perform("export-button", "导出中…", async () => {
  const response = await fetch("/matcher-api/export.csv");
  if (!response.ok) throw new Error("CSV 导出失败，请重新生成排名后重试。");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = element("a"); link.href = url; link.download = "waterlooworks-top100.csv";
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}));
for (const id of ["target-roles", "locations", "exclude-keywords"]) $(id).addEventListener("input", () => { if (ranking) clearRanking("申请偏好已更改。请重新生成排名以应用新偏好。"); });
$("result-search").addEventListener("input", renderResults);
$("reconnect-button").addEventListener("click", () => void refreshStatus());
document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); void poll(); } });
void poll();
