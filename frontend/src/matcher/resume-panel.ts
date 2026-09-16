/** The resume panel: show the split the matcher will actually use.
 *
 * This is the one place the user can catch a bad parse before it silently
 * decides a hundred rankings, so it shows the structure — roles, durations,
 * which skills are backed by work and which are only listed — rather than a
 * single cloud of keyword chips.
 */
import { $, element, listMessages } from "./dom";
import type { Resume } from "./wire";

const KIND_LABELS: Record<string, string> = {
  work: "工作", project: "项目", research: "研究", leadership: "社团/志愿", other: "其他",
};

function experienceRow(item: Resume["experiences"][number]): HTMLLIElement {
  const entry = element("li", "resume-experience");
  entry.append(element("span", "skill-chip", [
    KIND_LABELS[item.kind] || item.kind,
    item.months ? `${item.months} 个月` : "时长未标注",
    // Recency is a scoring dimension, so the date it is derived from is shown.
    item.months_ago === null ? "日期未识别"
      : item.months_ago === 0 ? "进行中"
      : item.months_ago < 12 ? `${item.months_ago} 个月前`
      : `${(item.months_ago / 12).toFixed(1)} 年前`,
  ].join(" · ")));
  entry.append(element("span", "resume-experience-name",
    [item.title, item.organization].filter(Boolean).join(" @ ") || "未能识别标题"));
  if (item.technologies.length) {
    entry.append(element("span", "small-label", item.technologies.join("、")));
  }
  return entry;
}

export function renderResume(resume: Resume | null, hasResume: boolean): void {
  $("resume-summary").hidden = !resume;
  $("resume-indicator").textContent = hasResume ? "已解析" : "待上传";
  $("resume-indicator").dataset.ready = String(hasResume);
  if (!resume) return;

  $("resume-filename").textContent = resume.filename;
  $("resume-headline").textContent = [
    resume.headline,
    resume.source === "model" ? "由模型拆分" : "由本地规则拆分",
    resume.total_experience_months ? `累计 ${resume.total_experience_months} 个月经历` : "",
  ].filter(Boolean).join(" · ");

  $("resume-experiences").replaceChildren(...(resume.experiences.length
    ? resume.experiences.map(experienceRow)
    : [element("li", "small-label",
        "未能从简历中拆出工作或项目段落；匹配将主要依赖技能清单，证据强度较弱。")]));

  $("resume-skills").replaceChildren(...(resume.skills.length
    ? resume.skills.map(skill => element("span", "skill-chip", skill))
    : [element("span", "small-label", "没有技能被任何经历佐证。")]));

  $("resume-listed-skills").replaceChildren(...(resume.listed_only_skills.length
    ? resume.listed_only_skills.map(skill => element("span", "skill-chip missing", skill))
    : [element("span", "small-label", "无。")]));

  const availability = [
    resume.availability.months.length ? `可接受工期：${resume.availability.months.join(" / ")} 个月` : "",
    resume.availability.terms.length ? `学期：${resume.availability.terms.join("、")}` : "",
  ].filter(Boolean);
  $("resume-availability").textContent = availability.length
    ? availability.join(" · ")
    : "简历未写明可用工期；工期冲突检查将无法自动进行。";

  listMessages("resume-warnings", resume.warnings);
}
