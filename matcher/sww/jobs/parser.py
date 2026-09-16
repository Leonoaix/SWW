"""Parse only visible job data; never persist page HTML, forms, or session data.

Modern DOM references (not a substitute for testing the authenticated live site):
https://github.com/jerryzhang1011/waterlooworks-application-plugins/blob/main/
plugins/waterlooworks-jobs-codex/skills/ww-scrape-jobs/references/chrome-scrape-workflow.md
The official 2025 redesign allows rearranging columns, so fields use headers:
https://uwaterloo.ca/co-operative-education/news/waterlooworks-updates-improved-user-experience
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

ORIGIN = "https://waterlooworks.uwaterloo.ca"
JOBS_URL = ORIGIN + "/myAccount/co-op/full/jobs.htm"
DASHBOARD_URL = ORIGIN + "/myAccount/dashboard.htm"


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def header_text(node: Tag) -> str:
    """Ignore decorative sort/menu icons, which often contain visible names."""
    copied = BeautifulSoup(str(node), "html.parser")
    for icon in copied.select(".material-icons, .material-symbols-outlined, [aria-hidden='true'], svg"):
        icon.decompose()
    return clean(copied.get_text(" ", strip=True))


def label(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


FIELD_LABELS = {
    "id": {"id", "jobid", "postingid", "jobpostingid", "jobnumber"},
    "title": {"title", "jobtitle", "positiontitle", "jobpostingtitle", "position"},
    "company": {"company", "organization", "organisation", "employer", "organizationname", "employername"},
    "location": {"location", "city", "joblocation", "joblocationcity", "worklocation"},
    "description": {"jobsummary", "jobdescription", "description", "positiondescription", "responsibilities", "jobresponsibilities"},
    "requirements": {"requiredskills", "requirements", "qualifications", "requiredqualifications", "jobrequirements", "skillsrequired", "requiredknowledgeandskills"},
    "deadline": {"deadline", "applicationdeadline", "appdeadline", "applicationdeadlinedate"},
}

METADATA_LABELS = {
    "status", "jobstatus", "postingstatus", "division", "openings", "numberofjobopenings",
    "level", "jobtype", "workterm", "worktermduration", "targeteddegreesanddisciplines",
    "compensation", "salary", "joblocationprovincestate", "joblocationcountry",
    "specialjobrequirements", "region", "employmentlocationarrangement",
}


def field_for(value: str) -> Optional[str]:
    key = label(value)
    return next((name for name, aliases in FIELD_LABELS.items() if key in aliases), None)


def placeholder_url(job_id: str) -> str:
    """Stand-in for a posting that has no address of its own.

    Most board rows open their posting through an onclick handler, so there is
    no href to link to. This records which posting a row referred to without
    pretending to be a link to it — `is_placeholder` is how every consumer
    tells the difference, because the string is otherwise a perfectly valid
    WaterlooWorks URL and will silently land on the board.
    """
    return JOBS_URL + "#job-" + job_id


def is_placeholder(url: str) -> bool:
    """Whether this is a stand-in rather than a link to an actual posting."""
    return bool(url) and url.startswith(JOBS_URL + "#job-")


def safe_job_url(href: str, base: str = JOBS_URL) -> Optional[str]:
    """Allow only HTTPS read-only board/posting URLs, dropping fragments.

    Do not accept auth tokens or arbitrary actions from scraped links. This also
    makes the returned public source URL safe to include in a CSV export.
    """
    if not href or href.startswith(("#", "javascript:")):
        return None
    try:
        url = urlsplit(urljoin(base, href))
        valid_origin = (url.scheme == "https" and url.hostname == "waterlooworks.uwaterloo.ca"
                        and not url.username and not url.password and url.port in (None, 443))
    except ValueError:
        return None
    if not valid_origin:
        return None
    if url.path not in {"/myAccount/co-op/full/jobs.htm", "/myAccount/jobs.htm"}:
        return None
    query = parse_qs(url.query, keep_blank_values=True)
    allowed = {"action", "jobid", "postingid", "id", "page", "pagenumber", "sort", "direction"}
    actions = {"display", "displayjob", "displayposting", "view", "viewjob", "viewposting", "show", "showposting", "postingdetails", "getpostingoverview", "search", "list"}
    for key, values in query.items():
        if key.lower() not in allowed:
            return None
        if key.lower() == "action" and any(v.lower() not in actions for v in values):
            return None
        if key.lower() in {"jobid", "postingid", "id", "page", "pagenumber"} and any(not v.isdigit() for v in values):
            return None
    return urlunsplit(("https", "waterlooworks.uwaterloo.ca", url.path, urlencode(query, doseq=True), ""))


@dataclass
class Target:
    selector: str
    url: Optional[str] = None


@dataclass
class Listing:
    jobs: list[dict] = field(default_factory=list)
    targets: dict[str, Target] = field(default_factory=dict)
    next_page: Optional[Target] = None
    first_page: Optional[Target] = None
    previous_page: Optional[Target] = None
    start_confirmed: bool = False
    current_page: Optional[int] = None
    expected_total: Optional[int] = None
    end_confirmed: bool = False
    empty: bool = False
    recognized: bool = False
    warnings: list[str] = field(default_factory=list)


def css_path(node: Tag) -> str:
    """Structural selector without interpolating scraped attribute values."""
    parts = []
    while isinstance(node, Tag) and node.name != "[document]":
        siblings = [s for s in node.previous_siblings if isinstance(s, Tag) and s.name == node.name]
        parts.append(f"{node.name}:nth-of-type({len(siblings) + 1})")
        node = node.parent
    return " > ".join(reversed(parts))


def _disabled(node: Tag) -> bool:
    return any(
        x.has_attr("disabled") or x.get("aria-disabled") == "true"
        or bool(set(x.get("class", [])) & {"disabled", "is--disabled"})
        for x in (node, node.parent) if isinstance(x, Tag)
    )


def _hidden(node: Tag) -> bool:
    return any(
        parent.name in {"script", "style"} or parent.has_attr("hidden")
        or parent.get("aria-hidden") == "true" or "is--hidden" in parent.get("class", [])
        or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", str(parent.get("style", "")), re.I))
        for parent in [node, *node.parents] if isinstance(parent, Tag)
    )


def _safe_target(node: Tag, base: str, known_ui: bool = False) -> Optional[Target]:
    href = str(node.get("href", ""))
    url = safe_job_url(href, base)
    if url:
        return Target(css_path(node), url)
    # These selectors are the board's documented title/pagination controls.
    # Their event handlers run only through a normal browser click, never eval.
    inert_href = not href or href == "#" or bool(re.fullmatch(r"javascript:\s*(?:void\(0\)|;)\s*;?", href, re.I))
    if known_ui and inert_href:
        return Target(css_path(node))
    handler = clean(str(node.get("onclick", "")))
    if re.fullmatch(r"(?:return\s+)?(?:window\.)?(?:getPostingOverview|displayPosting|displayJob|viewPosting|showPosting)\(\s*['\"]?\d+['\"]?\s*\)\s*;?(?:\s*return false;?)?", handler):
        return Target(css_path(node))
    return None


def _job_id(node: Tag, fields: dict, link: Tag, base: str) -> str:
    selection = node.select_one('input[name="dataViewerSelection"]')
    candidates = [fields.get("id", ""), node.get("data-job-id", ""), node.get("data-posting-id", "")]
    if selection:
        candidates.insert(0, selection.get("value", ""))
    url = safe_job_url(str(link.get("href", "")), base)
    if url:
        for key, values in parse_qs(urlsplit(url).query).items():
            if key.lower() in {"jobid", "postingid", "id"}:
                candidates.extend(values)
    handler = clean(str(link.get("onclick", "")))
    if re.fullmatch(r"(?:return\s+)?(?:window\.)?(?:getPostingOverview|displayPosting|displayJob|viewPosting|showPosting)\(\s*['\"]?\d+['\"]?\s*\)\s*;?(?:\s*return false;?)?", handler):
        candidates.append(re.search(r"\d+", handler).group())
    for candidate in candidates:
        candidate = clean(str(candidate))
        if candidate.isdigit():
            return candidate
    return ""


def _fields_from_labels(node: Tag) -> tuple[dict, dict]:
    fields, metadata = {}, {}
    pairs = []
    for row in node.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            pairs.append((cells[0].get_text(" ", strip=True), cells[1].get_text("\n", strip=True)))
    for term in node.select("dt"):
        definition = term.find_next_sibling("dd")
        if definition:
            pairs.append((term.get_text(" ", strip=True), definition.get_text("\n", strip=True)))
    for item in node.select("[data-label]"):
        pairs.append((str(item["data-label"]), item.get_text("\n", strip=True)))
    # Overview also uses stacked labels, e.g. <div><strong>Work Term:</strong>
    # <div>2027 - Winter</div></div>, rather than two-column tables.
    for term in node.select("strong, b, label, h3, h4, h5, .field-label, .form-label"):
        key = term.get_text(" ", strip=True)
        if not field_for(key) and label(key) not in METADATA_LABELS:
            continue
        if term.find_parent(["th", "td", "dt"]):
            continue
        anchor = term
        if not any(clean(str(s)) for s in anchor.next_siblings) and isinstance(anchor.parent, Tag):
            anchor = anchor.parent
        values = []
        short_field = field_for(key) not in {None, "description", "requirements"}
        for sibling in anchor.next_siblings:
            if isinstance(sibling, Tag):
                # Scalar labels in free-form descriptions must not absorb all
                # following paragraphs/headings as a city/title/company name.
                if short_field and values and sibling.name in {"br", "p", "div", "section", "ul", "ol", "h2", "h3", "h4", "h5"}:
                    break
                sibling_key = sibling.get_text(" ", strip=True)
                if field_for(sibling_key) or label(sibling_key) in METADATA_LABELS:
                    break
                if any(field_for(t.get_text(" ", strip=True)) or label(t.get_text(" ", strip=True)) in METADATA_LABELS
                       for t in sibling.select("strong, b, label, h3, h4, h5, .field-label, .form-label")):
                    break
                values.append(sibling.get_text("\n", strip=True))
                if short_field and sibling.name in {"p", "div", "section", "ul", "ol"} and clean(values[-1]):
                    break
            else:
                if str(sibling).strip():
                    values.append(str(sibling))
        value = "\n".join(part.strip() for part in values if part.strip())
        if value:
            pairs.append((key, value))
    for key, value in pairs:
        canonical = field_for(key)
        if canonical:
            # Explicit table/dl/data-label values precede free-form labels.
            # A repeated Location heading in the job summary must not overwrite
            # the structured location from the posting's overview.
            if canonical in {"description", "requirements"} or canonical not in fields:
                fields[canonical] = value.strip()
        elif label(key) in {"status", "jobstatus", "postingstatus"}:
            metadata["posting_status"] = clean(value)
        elif label(key) in METADATA_LABELS:
            metadata[clean(key).rstrip(":")] = value.strip()
    return fields, metadata


def parse_listing(html: str, base: str = JOBS_URL) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    result = Listing()
    # Leave the DOM intact: removing hidden siblings would invalidate the
    # nth-of-type selectors that are later clicked in the real browser.
    text = clean(" ".join(str(text) for text in soup.find_all(string=True) if not _hidden(text.parent)))
    total_match = re.search(r"([\d,]+)\s+(?:results|job postings|jobs found)\b", text, re.I)
    range_match = re.search(r"(?:showing\s+)?([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)", text, re.I)
    if total_match:
        result.expected_total = int(total_match[1].replace(",", ""))
    elif range_match:
        result.expected_total = int(range_match[3].replace(",", ""))
    result.empty = result.expected_total == 0 or bool(re.search(r"\bno (?:matching )?(?:jobs|job postings|results|records)(?: found| available| to display)\b", text, re.I))

    candidates = []
    for table in soup.select("table"):
        if _hidden(table):
            continue
        header_row = table.select_one("thead tr") or table.find("tr")
        headers = [header_text(x) for x in header_row.find_all(["th", "td"], recursive=False)] if header_row else []
        has_title = any(field_for(h) == "title" for h in headers)
        for row in table.select("tr"):
            if row is header_row or _hidden(row):
                continue
            known = row.select_one('input[name="dataViewerSelection"]') is not None
            if not known and not has_title:
                continue
            result.recognized = True
            fields, metadata = {}, {}
            for i, cell in enumerate(row.find_all(["th", "td"], recursive=False)):
                heading = str(cell.get("data-label", "")) or (headers[i] if i < len(headers) else "")
                canonical = field_for(heading)
                if canonical:
                    fields[canonical] = clean(cell.get_text(" ", strip=True))
                elif label(heading) in {"status", "jobstatus", "postingstatus"}:
                    metadata["posting_status"] = clean(cell.get_text(" ", strip=True))
                elif heading and label(heading) in {"division", "openings", "level", "apps", "applications"}:
                    metadata[heading] = clean(cell.get_text(" ", strip=True))
            candidates.append((row, fields, metadata, known))
    for card in soup.select(".doc-viewer__card, [data-job-id], [data-posting-id]"):
        if card.name == "tr" or _hidden(card):
            continue
        result.recognized = True
        fields, metadata = _fields_from_labels(card)
        candidates.append((card, fields, metadata, bool(card.select_one('input[name="dataViewerSelection"]'))))

    seen = set()
    for node, fields, metadata, known in candidates:
        link = node.select_one("a.overflow--ellipsis")
        if not link:
            # The screenshot's action icons precede the actual title. Prefer
            # the title column so a print/favourite link cannot become a job.
            link = next((cell.select_one("a") for cell in node.find_all(["th", "td"], recursive=False)
                         if clean(cell.get_text(" ", strip=True)) == fields.get("title") and cell.select_one("a")), None)
        if not link:
            link = next((a for a in node.select("a") if _safe_target(a, base)), None)
        if not link or _hidden(link):
            continue
        job_id = _job_id(node, fields, link, base)
        title = clean(link.get_text(" ", strip=True)) or fields.get("title", "")
        target = _safe_target(link, base, known_ui=bool(fields.get("title")) or (known and "overflow--ellipsis" in link.get("class", [])))
        if not job_id or not title or job_id in seen:
            continue
        seen.add(job_id)
        if target:
            result.targets[job_id] = target
        metadata["detail_status"] = "pending" if target else "unavailable"
        if not target:
            result.warnings.append(f"Job {job_id}: no recognized read-only detail link.")
        job = {name: "" for name in FIELD_LABELS}
        job.update(fields)
        job.update(id=job_id, title=title, url=(target.url if target and target.url else placeholder_url(job_id)), metadata=metadata)
        result.jobs.append(job)

    # A stale/hidden empty notice must never override real rows.
    if result.jobs:
        result.empty = False

    pagination = [node for node in soup.select(".pagination__link, .pagination a, .pagination button, a[rel~=next], a[rel~=prev], [aria-label='Next page'], [aria-label='Previous page'], [aria-label='First page']") if not _hidden(node)]
    current = next((p for p in pagination if p.get("aria-current") == "page" or "active" in p.get("class", []) or (isinstance(p.parent, Tag) and "active" in p.parent.get("class", []))), None)
    if current and clean(current.get_text()).isdigit():
        result.current_page = int(clean(current.get_text()))
    result_range = re.search(r"\bresults\s+([\d,]+)\s*[-–]\s*([\d,]+)", text, re.I)
    result.start_confirmed = result.current_page == 1 or bool(result_range and result_range[1] == "1") or bool(range_match and range_match[1] == "1")
    next_candidates = []
    previous_candidates = []
    for node in pagination:
        title = clean(node.get_text(" ", strip=True))
        aria = clean(str(node.get("aria-label", "")))
        is_next = "next" in node.get("rel", []) or bool(re.fullmatch(r"next(?: page)?|[›»>]|chevron_right|navigate_next", title, re.I)) or aria.lower() == "next page"
        is_first = title == "1" or bool(re.fullmatch(r"first(?: page)?|first_page|keyboard_double_arrow_left|«", title, re.I)) or aria.lower() in {"first", "first page"}
        is_previous = "prev" in node.get("rel", []) or bool(re.fullmatch(r"prev(?:ious)?(?: page)?|[‹<]|chevron_left|navigate_before|keyboard_arrow_left", title, re.I)) or aria.lower() in {"previous", "previous page", "prev", "prev page"}
        if is_first:
            if _disabled(node):
                result.start_confirmed = True
            else:
                result.first_page = _safe_target(node, base, known_ui=True)
        if is_previous:
            if _disabled(node):
                result.start_confirmed = True
            else:
                previous_candidates.insert(0, node)
        elif result.current_page and title == str(result.current_page - 1) and not _disabled(node):
            previous_candidates.append(node)
        if is_next:
            if _disabled(node):
                result.end_confirmed = True
            else:
                next_candidates.insert(0, node)
        elif result.current_page and title == str(result.current_page + 1) and not _disabled(node):
            next_candidates.append(node)
    for node in next_candidates:
        result.next_page = _safe_target(node, base, known_ui=True)
        if result.next_page:
            result.end_confirmed = False
            break
    for node in previous_candidates:
        result.previous_page = _safe_target(node, base, known_ui=True)
        if result.previous_page:
            break
    if result.expected_total is not None and result.expected_total <= len(result.jobs):
        result.end_confirmed = True
    result.recognized = result.recognized or result.empty
    return result


def parse_detail(html: str, expected_id: str) -> Optional[dict]:
    """Only accept the expected job's detail; a stale modal is not a result."""
    soup = BeautifulSoup(html, "html.parser")
    for hidden in soup.select("[hidden], [aria-hidden='true'], .is--hidden, script, style"):
        hidden.decompose()
    container = soup.select_one(".modal.is--visible") or soup.select_one("[role='dialog'][aria-hidden='false']") or soup.select_one("#postingDiv, #jobPosting, .job-posting, .jobPosting")
    if container is None:
        # Standalone Overview pages show the ID in a badge, then structured
        # posting fields. Require both; never fall back to the whole body.
        for candidate in soup.select("main, #mainContent, .content"):
            fields, _ = _fields_from_labels(candidate)
            heading = candidate.get_text(" ", strip=True)
            overview = re.search(r"JOB POSTING INFORMATION", heading, re.I)
            has_id = re.search(r"(?<!\d)" + re.escape(expected_id) + r"(?!\d)", heading)
            if fields.get("id") == expected_id or (overview and has_id and fields.get("title")):
                container = candidate
                break
    if container is None:
        return None
    text = container.get_text("\n", strip=True)
    if not re.search(r"(?<!\d)" + re.escape(expected_id) + r"(?!\d)", text):
        return None
    fields, metadata = _fields_from_labels(container)
    if fields.get("id") and fields["id"] != expected_id:
        return None
    reader = container.select_one(".is--long-form-reading")
    if not fields.get("description") and reader:
        fields["description"] = reader.get_text("\n", strip=True)
    if not fields.get("description") and not fields.get("requirements"):
        return None
    # Keep full long-form content: qualifications can be free-form headings.
    if reader:
        metadata["detail_text"] = reader.get_text("\n", strip=True)
    else:
        # Feed structured standalone Overview information (including work term
        # and level) into the same ranking input as a modal's reading panel.
        metadata["detail_text"] = "\n\n".join(f"{key}: {value}" for key, value in {**fields, **metadata}.items())
    metadata["detail_status"] = "complete"
    fields.pop("id", None)
    fields["metadata"] = metadata
    return fields


def login_required(html: str, url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.hostname != "waterlooworks.uwaterloo.ca":
        return True
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("input[type='password']"):
        return True
    text = clean(soup.get_text(" ", strip=True))
    return (bool(re.search(r"not logged in|session (?:has )?expired|session (?:has )?timed out|please (?:log|sign) in|you (?:have been|are) logged out", text, re.I))
            or parsed.path.lower() == "/notloggedin.htm"
            or bool(re.search(r"/(?:login|sso|logout)(?:\.|/|$)", parsed.path, re.I))
            or (parsed.path.lower() in {"/", "/home.htm", "/index.htm"} and bool(re.search(r"\b(?:login|log in|sign in)\b", text, re.I))))
