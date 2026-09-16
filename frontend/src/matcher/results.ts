/** The results list.
 *
 * Two things make this cheap enough to type into. A Top-100 ranking with two
 * dozen assessed requirements each is roughly 19,000 DOM nodes if every
 * collapsed `<details>` is built up front — and the previous version rebuilt
 * all of them on every keystroke in the search box, which cost ~34 ms per
 * character, well past a 16 ms frame.
 *
 *   * Detail bodies are built the first time their `<details>` is opened.
 *   * Filtering toggles `hidden` on rows that already exist instead of
 *     recreating them, so typing is a few attribute writes.
 *
 * Row numbering comes from `<li value>`, not document order, so hiding a row
 * never renumbers the ones around it.
 */
import { $, chips, element, externalLink, listMessages, message, safeJobUrl } from "./dom";
import { hasOpened, markOpened } from "./applied";
import { post } from "./client";
import { type JsonObject, type Ranking, type RankedJob, string } from "./wire";

/** Set by the page when the service reports the login browser is up. */
let loginBrowserOpen = false;
export const setLoginBrowserOpen = (value: boolean): void => { loginBrowserOpen = value; };

const STATUS_LABELS: Record<string, string> = {
  direct: "直接经验", transferable: "可迁移经验", missing: "未体现",
  unknown: "待确认", conflict: "资格冲突",
};

const ELIGIBILITY_LABELS: Record<string, string> = {
  conflict: "发现冲突", needs_review: "需要核实", no_known_conflict: "未发现明确冲突",
};

function criterionBlock(criterion: JsonObject): HTMLElement {
  const block = element("div", "evidence-item");
  const status = string(criterion.status);
  block.append(element("h4", "", `${STATUS_LABELS[status] || status} · `
    + `${criterion.importance === "must" ? "必需" : "优先"} · ${string(criterion.requirement)}`));
  block.append(element("p", "", string(criterion.explanation)));
  block.append(element("p", "small-label", "职位原文"),
               element("blockquote", "evidence-quote", string(criterion.job_quote)));
  if (criterion.resume_quote) {
    block.append(element("p", "small-label", "简历证据"),
                 element("blockquote", "evidence-quote", string(criterion.resume_quote)));
  }
  return block;
}

/** Everything behind the disclosure triangle, built only when it is opened. */
function detailContent(job: RankedJob): Node[] {
  const parts: Node[] = [];
  if (job.criteria.length) {
    parts.push(element("h4", "", `资格核对：${ELIGIBILITY_LABELS[job.eligibility] || "需要核实"}`));
    for (const criterion of job.criteria) parts.push(criterionBlock(criterion));
    for (const review of job.pairwise_reviews) {
      parts.push(element("p", "field-hint",
        `与 #${string(review.other_id)} 双向比较：${review.preferred ? "本岗位更匹配" : "对方更匹配"}。`));
    }
  }
  parts.push(element("h4", "", "简历中未识别到的职位技能"));
  parts.push(job.missing_skills.length
    ? chips(job.missing_skills, true)
    : element("p", "", "未发现额外技能缺口。这不代表已满足所有申请条件。"));

  const breakdown = Object.entries(job.score_breakdown);
  if (breakdown.length) {
    parts.push(element("h4", "", "评分明细"));
    const list = element("dl", "score-breakdown");
    for (const [key, value] of breakdown) {
      const pair = element("div");
      pair.append(element("dt", "", `${key}:`),
                  element("dd", "tabular", typeof value === "number" ? value.toFixed(2)
                    : string(value) || JSON.stringify(value)));
      list.append(pair);
    }
    parts.push(list);
  }
  if (job.retrieval_score !== null) {
    parts.push(element("p", "field-hint",
      `本地检索相关度 ${(job.retrieval_score * 100).toFixed(1)}%（用于候选筛选，与最终分数分开计算）。`));
  }
  if (job.recency_alignment !== null) {
    parts.push(element("p", "field-hint",
      `经历新近度 ${(job.recency_alignment * 100).toFixed(0)}%：支撑这个岗位的是你`
      + `${job.recency_alignment > 0.85 ? "最近" : job.recency_alignment > 0.6 ? "较近" : "较早"}`
      + "的经历。越近权重越高（两年半衰，下限 40%），只影响排序，不影响证据是否成立。"));
  }
  if (job.warnings.length) {
    const warnings = element("ul", "warning-list");
    warnings.append(...job.warnings.map(warning => element("li", "", warning)));
    parts.push(warnings);
  }
  parts.push(element("h4", "", "职位要求"),
             element("p", "job-text", job.requirements || "未单独提取到要求，请查看职位描述与原页面。"));
  parts.push(element("h4", "", "职位描述"),
             element("p", "job-text", job.description || "暂无详情文本，当前排名仅依据已收集的字段。请查看原职位。"));
  // No second link here: the row carries the apply action. Two controls with
  // the same label and different behaviour — one routing through the login
  // browser and recording the visit, one not — is worse than one.
  return parts;
}

/** The apply affordance: one click from the ranked row to the posting.
 *
 * It opens the application page. It never submits one — choosing a resume
 * package and answering an employer's questions is not something to automate
 * on someone's behalf, and it cannot be undone.
 *
 * When the service's login browser is up the posting opens there, already
 * authenticated, in a new tab so a running board page keeps its filters.
 * Otherwise this is an ordinary link, so middle-click and ctrl-click behave
 * the way links do.
 */
function applyAction(job: RankedJob): HTMLElement {
  const row = element("div", "job-actions");
  const url = safeJobUrl(job.url);
  const state = element("span", "job-applied-state");
  const refresh = () => {
    state.textContent = hasOpened(job.id) ? "已打开过申请页" : "";
    state.hidden = !hasOpened(job.id);
  };

  if (!url) {
    row.append(element("span", "field-hint",
      `未获得有效职位链接，请在 WaterlooWorks 内按编号 ${job.id || "（未知）"} 搜索。`));
    return row;
  }

  const link = externalLink(url, "去申请 ↗", "btn btn-primary btn-sm");
  link.addEventListener("click", event => {
    markOpened(job.id);
    refresh();
    if (!loginBrowserOpen) return;          // plain link: let the browser navigate
    event.preventDefault();
    link.setAttribute("aria-busy", "true");
    void post(`/jobs/${encodeURIComponent(job.id)}/open`)
      .catch(() => { window.open(url, "_blank", "noopener"); })
      .finally(() => link.removeAttribute("aria-busy"));
  });
  row.append(link, state);
  refresh();
  return row;
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
  if (url) title.append(externalLink(url, job.title));
  else title.textContent = job.title;
  identity.append(title, element("p", "job-meta", [
    job.location || "地点未提供",
    job.deadline ? `截止 ${job.deadline}` : "截止日期待确认",
    job.id ? `#${job.id}` : "",
  ].filter(Boolean).join(" · ")));
  top.append(identity);

  const score = element("div", "job-score");
  score.append(element("strong", "tabular", job.score.toFixed(1)), element("span", "", "匹配分 / 100"));
  if (job.must_have_coverage !== null && job.must_have_count) {
    score.append(element("span", "small-label",
      `必需条件覆盖 ${Math.round(job.must_have_coverage * 100)}%（共 ${job.must_have_count} 项）`));
  }
  top.append(score);
  item.append(top);

  const body = element("div", "job-body");
  body.append(applyAction(job));
  if (job.matched_skills.length) body.append(chips(job.matched_skills));
  if (job.reasons.length) {
    const reasons = element("ul", "job-reasons");
    reasons.append(...job.reasons.map(reason => element("li", "", reason)));
    body.append(reasons);
  }

  const details = element("details", "job-detail");
  details.append(element("summary", "", "查看技能缺口、评分与职位详情"));
  let built = false;
  details.addEventListener("toggle", () => {
    if (!details.open || built) return;
    built = true;
    details.append(...detailContent(job));
  });
  body.append(details);
  item.append(body);
  return item;
}

function resultNote(ranking: Ranking): string {
  const stages = [ranking.semantic_retrieval ? "BM25 + 本地语义检索" : "仅 BM25 词频检索"];
  if (ranking.reranked_jobs) stages.push(`本地重排 ${ranking.reranked_jobs} 个`);
  if (ranking.requirement_chunks_used) stages.push("检索已使用缓存的岗位要求");
  const retrieval = stages.join(" · ");
  if (ranking.engine !== "deepseek") {
    return `本地排序（无 API 调用）：${retrieval}。${ranking.warnings.join(" ")}`;
  }
  const parts = [
    `${retrieval}，从 ${ranking.eligible_jobs} 个合格职位中选出候选，`
    + `模型已评估 ${ranking.assessed_jobs} 个（复用缓存 ${ranking.cached_jobs} 个）。`,
  ];
  if (ranking.not_assessed_jobs) {
    parts.push(`另有 ${ranking.not_assessed_jobs} 个未评估，把候选数量设为 0 可全量评估。`);
  }
  if (ranking.failed_jobs.length) {
    parts.push(`${ranking.failed_jobs.length} 个失败，未混入本地分数；本次排名不完整。`);
  }
  return parts.join("") + " " + ranking.warnings.join(" ");
}

/** Owns the rendered rows so filtering never has to rebuild them. */
export class ResultsView {
  private ranking: Ranking | null = null;
  private rows: { job: RankedJob; node: HTMLLIElement }[] = [];

  clear(reason?: string): void {
    this.ranking = null;
    this.rows = [];
    message("ai-result-note", "");
    $("job-results").replaceChildren();
    $("eligible-count").textContent = "—";
    $("ranked-count").textContent = "—";
    $("results-toolbar").hidden = true;
    $("results-count").hidden = true;
    $("ranking-details").hidden = true;
    $("results-empty").hidden = false;
    $("empty-message").textContent = reason || "完成左侧的简历上传和职位抓取，即可生成你的申请优先级。";
  }

  get current(): Ranking | null {
    return this.ranking;
  }

  /** Full render. Called once per ranking, not once per keystroke. */
  show(ranking: Ranking): void {
    this.ranking = ranking;
    message("ai-result-note", resultNote(ranking));
    this.rows = ranking.jobs.map(job => ({ job, node: renderJob(job) }));
    $("job-results").replaceChildren(...this.rows.map(row => row.node));
    $("results-toolbar").hidden = ranking.jobs.length === 0;
    $("eligible-count").textContent = String(ranking.eligible_jobs);
    $("ranked-count").textContent = String(ranking.jobs.length);
    $("ranking-details").hidden = false;
    $("ranking-method").textContent = ranking.method || "本地文本匹配。请结合原始职位要求判断。";
    listMessages("excluded-jobs", [...ranking.excluded_jobs, ...ranking.failed_jobs]
      .map(job => `${string(job.title) || string(job.id)}：${string(job.reason) || "已排除"}`));
    this.filter();
  }

  /** Apply the search box to rows that already exist. */
  filter(): void {
    if (!this.ranking) return;
    const needle = $<HTMLInputElement>("result-search").value.trim().toLowerCase();
    let visible = 0;
    for (const { job, node } of this.rows) {
      const shown = !needle || job.search.includes(needle);
      if (node.hidden === shown) node.hidden = !shown;
      if (shown) visible += 1;
    }
    $("results-empty").hidden = visible > 0;
    $("empty-message").textContent = this.ranking.jobs.length
      ? "没有符合搜索条件的推荐，试试其他关键词。"
      : "当前偏好下没有可推荐的职位。请检查排除关键词、数据完整性与截止日期。";
    message("results-count", `显示 ${visible} / ${this.ranking.jobs.length} 个推荐；筛选不会改变原始申请优先级。`);
  }
}
