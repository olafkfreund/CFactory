// Correlation keys are "uuid:slug". The raw key wraps 4-5 lines on cards and
// buries the title, so every surface shows the human slug (one line, full key
// in a title= tooltip) and derives a readable title from the slug when the
// work item has none.

/** The human slug half of a correlation key ("uuid:slug" → "slug"). */
export function keySlug(key: string): string {
  const i = key.indexOf(":");
  const slug = i >= 0 ? key.slice(i + 1) : key;
  return slug || key;
}

/** Derive a human title from the slug: strip the leading number, replace
 *  dashes/underscores with spaces, title-case. "0042-fix-login-flow" →
 *  "Fix Login Flow". Returns null when nothing usable remains. */
export function titleFromSlug(key: string): string | null {
  const words = keySlug(key)
    .replace(/^\d+[-_]*/, "")
    .split(/[-_]+/)
    .filter(Boolean);
  if (words.length === 0) return null;
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

/** The display title for a work item: its real title, else one derived from
 *  the slug, else the generic fallback. */
export function displayTitle(title: string | null | undefined, key: string): string {
  const t = title?.trim();
  if (t) return t;
  return titleFromSlug(key) ?? "Untitled work item";
}
