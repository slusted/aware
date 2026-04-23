---
name: deep-research-brief
description: Prompt sent to Google's Gemini Deep Research agent for an investor-grade per-competitor dossier. The {{placeholders}} are substituted at call time by the deep-research job; Deep Research plans its own searches, reads up to ~160 sources, and returns a cited markdown report.
---

You are producing an investor-grade research dossier on a single competitor of **{{our_company}}**, which operates in **{{our_industry}}**.

**Competitor:** {{competitor_name}}
**Category:** {{category}}
**Our working view of their threat angle:** {{threat_angle}}
**Topics we watch about them:** {{watch_topics}}

Goal: a dossier that a strategy-minded CEO, product head, or board member could read in 10 minutes before a meeting where decisions are made about how to respond to this competitor. Every substantive claim must be grounded in a citation.

## Structure

Use these sections, in this order. Markdown headings (`##`) for each.

### Strategy today
What is {{competitor_name}} trying to become over the next 12–24 months? State the thesis plainly. Cover:
- Positioning and value proposition — how they describe themselves today and how that differs from 6 months ago.
- Product direction — what they are actively building, what they have recently shipped or sunsetted.
- Revenue model — pricing, packaging, monetisation, any disclosed financials.
- Geographies and segments — where they are winning, where they are investing, where they are retreating.
- Key partnerships and distribution channels.

### Momentum
Last 6 months of observable movement:
- Funding, valuation, investor commentary.
- Hiring patterns — headcount trajectory, functional mix shift, key senior hires.
- Product launches, pricing or packaging changes, notable customer wins.
- Traffic, install, or usage trajectories where public data exists.
- Regulatory or legal developments.
Call out *direction of change* explicitly — acceleration, plateau, drawdown.

### People
Key leaders and decision makers. Recent executive moves (in or out). Board changes. Named advisors who materially shape direction.

### How they compete with {{our_company}}
Honest, specific comparison:
- Where the two companies genuinely overlap — buyer, use case, pricing tier, geography.
- Where they don't overlap, and why.
- The one-to-three bets on which {{competitor_name}} is optimistic they can out-execute {{our_company}}.
- The bets where {{our_company}} has a structural advantage they are unlikely to close.

### Watchlist — signals to monitor
3–5 concrete, falsifiable signals that, if they flipped over the next 6 months, would materially change the strategic read. Each signal: what it is, what flipping it would mean, where to watch for it.

## Sourcing rules

- **Prefer primary sources:** earnings calls, SEC/ASIC/Companies House filings, the competitor's own blog / product changelog / careers page, verified executive statements, regulator decisions, signed analyst reports.
- **Treat secondary coverage as corroboration,** not the claim itself. News rewrites of press releases are weaker than the release.
- **Label speculation.** If an assertion is inferred rather than sourced — say so. Use phrases like "this suggests…", "unclear whether…", "no public confirmation of…".
- **Date every claim.** If a fact could be stale (pricing, headcount, partnership status), state when it was last observable.
- **Cite sources inline.** For every substantive claim, link to the source. Do not group citations in a bibliography and leave the body uncited.
- **Surface disagreement.** If two reputable sources contradict each other, name both and note the conflict rather than picking silently.
- **Skip fluff.** No preamble, no "in conclusion" summaries, no restating the brief. The sections above are the whole output.
