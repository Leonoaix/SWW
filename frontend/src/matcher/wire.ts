/** Wire types and the coercions that turn any server response into them.
 *
 * The service is local and trusted to be *ours*, not trusted to be *correct*:
 * a version mismatch or a partial result must degrade the page, never throw
 * inside a render loop. Every field is coerced, never asserted.
 */

export type JsonObject = Record<string, unknown>;
export type CrawlState = "idle" | "login_required" | "running" | "completed" | "failed" | "cancelled";

const CRAWL_STATES: readonly string[] = ["idle", "login_required", "running", "completed", "failed", "cancelled"];

export const object = (value: unknown): JsonObject =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
export const string = (value: unknown): string =>
  typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
export const number = (value: unknown, fallback = 0): number =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;
export const maybeNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
export const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.map(item => typeof item === "object" ? string(object(item).message) || JSON.stringify(item) : string(item)).filter(Boolean)
    : typeof value === "string" && value ? [value] : [];

export interface Experience {
  kind: string; title: string; organization: string;
  start: string; end: string; months: number | null; months_ago: number | null;
  technologies: string[];
}

export interface Resume {
  filename: string; headline: string; source: string;
  skills: string[]; attributed_skills: string[]; listed_only_skills: string[]; warnings: string[];
  experiences: Experience[]; total_experience_months: number;
  availability: { months: number[]; terms: string[] };
}

export interface Status {
  ai: JsonObject; ai_config: JsonObject; embeddings: JsonObject; rerank: JsonObject;
  browser: JsonObject; ranking: JsonObject;
  crawl: {
    state: CrawlState; message: string; job_count: number; pages: number;
    errors: string[]; warnings: string[]; complete: boolean;
  };
  job_count: number; has_resume: boolean; resume: Resume | null;
  source: string; collected_at: string;
}

export interface RankedJob {
  criteria: JsonObject[]; eligibility: string; pairwise_reviews: JsonObject[];
  id: string; title: string; company: string; location: string;
  description: string; requirements: string; deadline: string; url: string;
  rank: number; score: number;
  matched_skills: string[]; missing_skills: string[];
  reasons: string[]; warnings: string[]; score_breakdown: JsonObject;
  must_have_coverage: number | null; must_have_count: number | null;
  retrieval_score: number | null; recency_alignment: number | null;
  /** Lower-cased haystack, computed once so filtering never re-builds it. */
  search: string;
}

export interface Ranking {
  jobs: RankedJob[]; total_jobs: number; eligible_jobs: number;
  excluded_jobs: JsonObject[]; method: string; engine: string;
  assessed_jobs: number; failed_jobs: JsonObject[]; usage: JsonObject;
  warnings: string[]; cached_jobs: number;
  shortlisted_jobs: number; not_assessed_jobs: number; semantic_retrieval: boolean;
  reranked_jobs: number; requirement_chunks_used: boolean;
}

export function normalizeResume(value: unknown): Resume {
  const data = object(value);
  const availability = object(data.availability);
  return {
    filename: string(data.filename), headline: string(data.headline), source: string(data.source),
    skills: strings(data.skills), attributed_skills: strings(data.attributed_skills),
    listed_only_skills: strings(data.listed_only_skills),
    warnings: strings(data.warnings),
    experiences: Array.isArray(data.experiences) ? data.experiences.map(item => {
      const entry = object(item);
      return {
        kind: string(entry.kind), title: string(entry.title), organization: string(entry.organization),
        start: string(entry.start), end: string(entry.end), months: maybeNumber(entry.months),
        months_ago: maybeNumber(entry.months_ago), technologies: strings(entry.technologies),
      };
    }) : [],
    total_experience_months: number(data.total_experience_months),
    availability: {
      months: Array.isArray(availability.months) ? availability.months.map(item => number(item)) : [],
      terms: strings(availability.terms),
    },
  };
}

export function normalizeStatus(value: unknown): Status {
  const data = object(value);
  const crawl = object(data.crawl);
  const state = string(crawl.state);
  return {
    ai: object(data.ai), ai_config: object(data.ai_config),
    embeddings: object(data.embeddings), rerank: object(data.rerank),
    browser: object(data.browser), ranking: object(data.ranking),
    crawl: {
      state: (CRAWL_STATES.includes(state) ? state : "idle") as CrawlState,
      message: string(crawl.message), job_count: number(crawl.job_count), pages: number(crawl.pages),
      errors: strings(crawl.errors), warnings: strings(crawl.warnings), complete: crawl.complete === true,
    },
    job_count: number(data.job_count), has_resume: data.has_resume === true,
    resume: data.resume ? normalizeResume(data.resume) : null,
    source: string(data.source), collected_at: string(data.collected_at),
  };
}

export function normalizeRanking(value: unknown): Ranking {
  const data = object(value);
  if (!Array.isArray(data.jobs)) throw new Error("服务返回的排名数据格式不正确，请检查本地服务日志。");
  return {
    jobs: data.jobs.map((entry, index) => {
      const job = object(entry);
      const title = string(job.title) || "未提供职位名称";
      const company = string(job.company) || "未提供公司名称";
      const location = strings(job.location).join(", ");
      const matched = strings(job.matched_skills);
      return {
        criteria: Array.isArray(job.criteria) ? job.criteria.map(object) : [],
        eligibility: string(job.eligibility),
        pairwise_reviews: Array.isArray(job.pairwise_reviews) ? job.pairwise_reviews.map(object) : [],
        id: string(job.id), title, company, location,
        description: string(job.description), requirements: strings(job.requirements).join("\n"),
        deadline: string(job.deadline), url: string(job.url),
        rank: number(job.rank, index + 1), score: number(job.score),
        matched_skills: matched, missing_skills: strings(job.missing_skills),
        reasons: strings(job.reasons), warnings: strings(job.warnings),
        score_breakdown: object(job.score_breakdown),
        must_have_coverage: maybeNumber(job.must_have_coverage),
        must_have_count: maybeNumber(job.must_have_count),
        retrieval_score: maybeNumber(job.retrieval_score),
        recency_alignment: maybeNumber(job.recency_alignment),
        search: `${title} ${company} ${location} ${matched.join(" ")}`.toLowerCase(),
      };
    }),
    total_jobs: number(data.total_jobs), eligible_jobs: number(data.eligible_jobs),
    excluded_jobs: Array.isArray(data.excluded_jobs) ? data.excluded_jobs.map(object) : [],
    method: string(data.method), engine: string(data.engine),
    assessed_jobs: number(data.assessed_jobs),
    failed_jobs: Array.isArray(data.failed_jobs) ? data.failed_jobs.map(object) : [],
    usage: object(data.usage), warnings: strings(data.warnings),
    cached_jobs: number(data.cached_jobs), shortlisted_jobs: number(data.shortlisted_jobs),
    not_assessed_jobs: number(data.not_assessed_jobs),
    semantic_retrieval: data.semantic_retrieval === true,
    reranked_jobs: number(data.reranked_jobs),
    requirement_chunks_used: data.requirement_chunks_used === true,
  };
}
