/** Which postings you have already opened to apply to.
 *
 * Kept in this browser only. It records that you *opened* a posting, not that
 * you submitted anything — the service never applies on your behalf, so it has
 * no way to know whether you went through with it. The label says "已打开"
 * rather than "已申请" for that reason.
 *
 * A ranking is regenerated every run, so this is keyed by posting id and
 * survives re-ranking, re-crawling and restarts. localStorage can be
 * unavailable (private windows, blocked site data) and every access is
 * therefore guarded: losing the marks degrades the page, it does not break it.
 */
const KEY = "sww.opened-postings";

function read(): Set<string> {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter(id => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

let opened = read();

export const hasOpened = (id: string): boolean => Boolean(id) && opened.has(id);

export function markOpened(id: string): void {
  if (!id) return;
  opened.add(id);
  try {
    localStorage.setItem(KEY, JSON.stringify([...opened]));
  } catch {
    // Out of quota or storage disabled: the mark lives for this page view only.
  }
}

export function forgetOpened(): void {
  opened = new Set();
  try {
    localStorage.removeItem(KEY);
  } catch {
    // Nothing to clear if it was never writable.
  }
}
