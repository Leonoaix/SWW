# SWW Frontend — Design & Motion System

Companion to the pipeline description in [`../README.md`](../README.md). This
document covers how the one page looks, how it moves, and how it defends itself
against the local service's JSON.

No runtime dependencies. No webfonts, no animation library. That isn't
minimalism for its own sake — the page's central claim is that nothing leaves
your machine, and a page that opens a connection to Google Fonts on load
contradicts it.

Two stylesheets, and the split is the whole organising idea:

| File | Holds |
|---|---|
| `src/style.css` | The base layer: tokens, element defaults, easing, buttons, form fields. Anything a second page would also want. |
| `src/waterlooworks.css` | Everything specific to the matcher page. |

---

## 1. Static design

**Swiss / International Typographic Style** (Müller-Brockmann) supplies the
structure: a fixed spacing and type scale, hierarchy built from size and weight
rather than ornament, generous negative space, and hairline rules where most UI
kits would reach for a bordered box. The setup column is the clearest case — the
numbered steps and the rules between them already group the panel, so a border
around each step would say it twice.

**Dieter Rams** supplies the restraint. The rule the palette follows: colour
beyond the single accent must *encode* something. There are exactly two
non-accent hues left, and each has one job.

### Tokens (`src/style.css`, `:root`)

| Group | Notes |
|---|---|
| Colour | Warm paper (`--paper: #f7f6f3`) against cool ink. Warm stock is a Swiss printing convention and keeps large white areas from reading clinical. |
| Accent | `--accent` and its `-hover` / `-tint` / `-ring` variants. The only decorative colour. |
| Semantic | `--ok` marks a completed step and the connected-service dot; `--warn` colours warning text; `--danger` is reserved for destructive affordances. Nothing else is coloured. |
| Space | 4px base: `--s-1` … `--s-10`. |
| Type | Major third (1.25) on a 16px base, `--text-micro` … `--text-display`. |
| Radius | `--radius-sm` … `--radius-xl`. |
| Elevation | `--shadow-1` … `--shadow-4`, **two layers each** — a tight contact shadow plus a soft ambient one. Single-layer shadows are the main reason UI depth reads as fake. |

Numerals that change in place (scores, counts) get `.tabular` so they don't
reflow as they tick.

> **Known inconsistency.** `waterlooworks.css` was written with raw values
> (`clamp(30px, 3.5vw, 48px)`, `padding: 58px 0 48px`) rather than the type and
> space tokens. The scale above is therefore the system's stated vocabulary, not
> a description of every rule on the page. New rules should use the tokens; the
> existing ones are worth converting when they are next touched.

---

## 2. Motion

Derived from **Disney's twelve principles** (Thomas & Johnston) by way of
**Material's** motion spec, which is itself a restatement of them for screens.
What survives here is the vocabulary and two rules, not a choreography system:
the scroll-reveal, stagger and FLIP machinery belonged to the marketing and
dashboard pages and went with them.

1. **Asymmetric easing.** Entering elements use `--ease-out`
   (`cubic-bezier(0.22, 1, 0.36, 1)`, a quint-out): they cover most of the
   distance immediately, then settle. The eye reads the *start* of a motion, so
   a fast start feels responsive and a slow one feels broken. `--ease-spring`
   adds a slight overshoot for affordances that should feel physical.
2. **Duration scales with distance.** `--dur-1` (120ms) for a hover tint
   through `--dur-5` (760ms). A button that eases over 500ms feels mushy; a
   panel that snaps over 120ms feels violent.
3. **Only `transform` and `opacity`.** Both are composited, so neither triggers
   layout or paint. Everything else is a frame-rate problem waiting to happen.

One keyframe animation remains, `spin`, for in-flight indicators.

### Reduced motion

`prefers-reduced-motion: reduce` collapses transitions and animations to their
end state. Elements still *appear*, they just don't animate in. Because the page
no longer drives any animation from JavaScript, this is now entirely a CSS
concern and cannot desynchronise from what the scripts do.

### Not animating is also a decision

The results list is the page's one expensive surface, and it is deliberately
static: rows do not animate in, and filtering toggles `hidden` rather than
transitioning. Animating a hundred rows on every keystroke is how a search box
starts dropping frames.

---

## 3. The API boundary

`src/matcher/wire.ts` carries the wire types and, more importantly, the
coercions. The local service is trusted to be *ours*; it is not trusted to be
*correct*. A version mismatch, a partial result, or a field the server stopped
sending must degrade the page, never throw inside a render loop.

Every value crosses the boundary through `object()`, `string()`, `number()`,
`maybeNumber()` or `strings()`. None of them can throw:

```ts
strings(job.warnings)      // [] if absent, null, a string, or a list of objects
maybeNumber(job.score)     // null rather than NaN
object(data.rerank)        // {} if the server predates the field
```

That is why adding `rerank` and `requirement_chunks_used` to the status and
ranking payloads needed no version negotiation: an older service simply reports
them as absent and the page renders the stages it can confirm.

`normalizeRanking()` also precomputes each job's lowercase `search` haystack, so
filtering never rebuilds strings per keystroke.

### Two rules the page holds to

- **Never `innerHTML`.** Job text and model explanations are untrusted and reach
  the DOM only through `textContent` (`matcher/dom.ts`). The frontend
  integration test asserts an `<img onerror>` payload in a model explanation
  renders as text.
- **Say which signals actually ran.** Embeddings and reranking are optional. The
  page reports whether each stage happened rather than implying a full cascade,
  so a weaker ranking is never presented as a stronger one.

### A note on reproducibility

Scores are deterministic within a process. Across processes the local
cross-encoder can differ by about 0.1 on a 0–100 score, from ONNX runtime thread
scheduling. Ordering is unaffected in practice, but the numbers are not
bit-reproducible and should not be compared across runs at that precision.
