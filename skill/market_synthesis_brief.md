---
name: market_synthesis_brief
description: Brief sent to Gemini Deep Research for a cross-competitor market synthesis. Stitches 30 days of findings, per-competitor strategy reviews, and per-competitor deep-research excerpts into one investor-grade read of the whole market.
---

You are producing a weekly market synthesis for {{our_company}} in {{our_industry}}.
Your audience is the {{our_company}} strategy team — smart, time-poor, want
signal not noise. A long, padded report is worse than a short one with real
insight.

Use the inputs below as grounding for what our own competitor-watch system has
observed in the last {{window_days}} days. Go beyond them: cross-reference
with primary sources (earnings calls, SEC filings, product changelogs,
verified executive statements), recent analyst coverage, and regulatory
filings where relevant. Ground every claim in cited sources. When a claim is
speculative, label it as such.

**Our company context:**
{{company_brief}}

**Our customer context:**
{{customer_brief}}

**Competitor roster + recent per-competitor read:**
{{competitor_context}}

**Cross-competitor findings digest (last {{window_days}} days):**
{{findings_digest}}

**Per-competitor deep-research excerpts (for reference):**
{{deep_research_digest}}

---

Produce a synthesis with these sections:

1. **TL;DR** — one paragraph. Lead with the single most important market-level
   movement this period. If nothing material shifted, say so plainly — a quiet
   period is useful information.

2. **Market movements** — 3–6 cross-competitor narratives. Each is a *theme*
   (e.g. "AI matching is consolidating around three approaches", "ATS
   platforms are quietly building job distribution"), not a per-competitor
   dump. Name the competitors and cite the sources that anchor each
   narrative.

3. **Acceleration vs. deceleration** — which competitors have picked up pace
   this period, which have gone quiet, and what that likely means. Hiring
   signals + product cadence are the usual tells.

4. **Where the market is converging** — strategies, features, pricing moves,
   or segment bets that are showing up across multiple competitors at once.
   Convergence is often the strongest signal of where the puck is going.

5. **Where the market is diverging** — segments or strategies where
   competitors are actively placing different bets. Divergence is where the
   market hasn't decided yet, so {{our_company}}'s choice still matters.

6. **Implications for {{our_company}}** — specific, ranked. For each item,
   name the {{our_company}} initiative or product it touches, and whether
   it's a threat, an opportunity, or neutral context. Avoid generic
   "{{our_company}} should monitor this" closers — commit to a posture.

7. **Watchlist** — 3–5 specific signals the team should watch in the next
   6 weeks that would materially change this read. Each should be
   falsifiable ("Indeed launches SMB pricing tier", not "Indeed evolves").

Tone: sharp colleague, not consultant. Direct, specific, occasionally
opinionated. It is fine to say "this doesn't look like a real threat" or
"this is worth worrying about." The team trusts your judgment — that is why
they have you.
