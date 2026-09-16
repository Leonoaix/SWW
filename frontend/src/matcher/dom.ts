/** Small DOM helpers. Everything user- or model-supplied goes in as text. */

/** Look up a required element, failing loudly rather than silently no-op'ing. */
export const $ = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing page element: ${id}`);
  return node as T;
};

export function element<K extends keyof HTMLElementTagNameMap>(
  tag: K, className = "", text = "",
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  // textContent, never innerHTML: job and model text is untrusted.
  if (text) node.textContent = text;
  return node;
}

export function message(id: string, text: string): void {
  const node = $(id);
  node.textContent = text;
  node.hidden = !text;
}

export function listMessages(id: string, items: string[]): void {
  const list = $(id);
  list.replaceChildren(...items.map(item => element("li", "", item)));
  list.hidden = items.length === 0;
}

export function chips(items: string[], missing = false): HTMLElement {
  const container = element("div", "skill-list");
  container.append(...items.map(skill => element("span", `skill-chip${missing ? " missing" : ""}`, skill)));
  return container;
}

/** Whether a URL actually addresses a posting, or only stands in for one.
 *
 * Most board rows open their posting through an onclick handler and have no
 * href, so the crawler records `jobs.htm#job-<id>`. That is a perfectly valid
 * WaterlooWorks URL and passes every other check — it just lands on the board.
 * Presenting it as an apply link is how the button came to take people to the
 * wrong page.
 */
export const isPlaceholderUrl = (value: string): boolean =>
  /\/myAccount\/co-op\/full\/jobs\.htm#job-/.test(value);

/** Only an https WaterlooWorks posting URL may become a link. */
export function safeJobUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "waterlooworks.uwaterloo.ca"
      && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

export function externalLink(href: string, text: string, className = ""): HTMLAnchorElement {
  const link = element("a", className, text);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}
