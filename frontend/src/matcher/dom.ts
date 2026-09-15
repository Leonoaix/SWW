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
