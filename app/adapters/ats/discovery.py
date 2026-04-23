"""ATS tenant auto-discovery.

Given a competitor's careers page, scrape anchor links for canonical ATS
tenant URLs and return them as normalized `host[/slug]` prefixes. The hiring
sweep uses these prefixes to scope job searches to the competitor's own
board on the ATS rather than the ATS root domain (which hosts every
customer's jobs).

Patterns are intentionally conservative — we'd rather miss a tenant and
fall back to the (imperfect) name-scoped query than record a wrong slug
that permanently poisons the hiring signal. Operators can always edit
the list in the admin UI.

Supported ATS platforms (pattern bank below):
  - Greenhouse   (boards.greenhouse.io/<slug>, job-boards.greenhouse.io/<slug>)
  - Lever        (jobs.lever.co/<slug>)
  - Ashby        (jobs.ashbyhq.com/<slug>)
  - Workday      (<slug>.myworkdayjobs.com/<tenant>)
  - BambooHR     (<slug>.bamboohr.com/jobs|careers)
  - Rippling     (<slug>.rippling-ats.com, ats.rippling.com/<slug>)
  - Workable     (apply.workable.com/<slug>, <slug>.workable.com)
  - SmartRecruiters (jobs.smartrecruiters.com/<slug>, careers.smartrecruiters.com/<slug>)
  - Jobvite      (jobs.jobvite.com/<slug>, careers.jobvite.com/<slug>)
  - iCIMS        (careers-<slug>.icims.com, <slug>.icims.com)
  - Taleo        (<slug>.taleo.net)
  - SAP SuccessFactors (career.successfactors.com/career?company=<slug>)
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


# Each entry describes one ATS layout.
#   regex: captures the full canonical tenant prefix as group(1).
#          Match on the full URL so we can pull host + path in one shot.
#   min_slug: minimum slug length — reject matches where the slug is a
#             known-junk shortname (e.g. "jobs", "careers") or too short
#             to be a real company tenant. 2 is enough to catch "vw" but
#             reject stray single-letter matches.
_PATTERNS: list[tuple[re.Pattern, int]] = [
    # Greenhouse: boards.greenhouse.io/<slug> and job-boards.greenhouse.io/<slug>.
    # The embeddable API path /boards/<slug>/jobs is excluded — we want the
    # public board, not the API endpoint, even though both identify the slug.
    (re.compile(
        r"\bhttps?://(?:boards|job-boards)\.greenhouse\.io/([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
    # Lever: jobs.lever.co/<slug>
    (re.compile(
        r"\bhttps?://jobs\.lever\.co/([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
    # Ashby: jobs.ashbyhq.com/<slug>
    (re.compile(
        r"\bhttps?://jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_.-]+)",
        re.IGNORECASE,
    ), 2),
    # Workday: <slug>.myworkdayjobs.com/<tenant>. Tenant path is required — the
    # bare subdomain is a landing page that redirects through a client-side
    # router, so Tavily won't index it. Capture <subdomain>.myworkdayjobs.com/<path1>.
    (re.compile(
        r"\bhttps?://([a-z0-9][a-z0-9-]+)\.myworkdayjobs\.com/([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
    # BambooHR: <slug>.bamboohr.com/jobs or /careers (both live).
    (re.compile(
        r"\bhttps?://([a-z0-9][a-z0-9-]+)\.bamboohr\.com/(?:jobs|careers)",
        re.IGNORECASE,
    ), 2),
    # Rippling: <slug>.rippling-ats.com, plus the newer ats.rippling.com/<slug>.
    (re.compile(
        r"\bhttps?://([a-z0-9][a-z0-9-]+)\.rippling-ats\.com",
        re.IGNORECASE,
    ), 2),
    (re.compile(
        r"\bhttps?://ats\.rippling\.com/([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
    # Workable: apply.workable.com/<slug> OR <slug>.workable.com.
    (re.compile(
        r"\bhttps?://apply\.workable\.com/([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
    (re.compile(
        r"\bhttps?://([a-z0-9][a-z0-9-]+)\.workable\.com",
        re.IGNORECASE,
    ), 2),
    # SmartRecruiters: jobs.smartrecruiters.com/<slug>, careers.smartrecruiters.com/<slug>.
    (re.compile(
        r"\bhttps?://(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
    # Jobvite: jobs.jobvite.com/<slug>, careers.jobvite.com/<slug>.
    (re.compile(
        r"\bhttps?://(?:jobs|careers)\.jobvite\.com/([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
    # iCIMS: careers-<slug>.icims.com and <slug>.icims.com (rarer). The
    # hyphenated form is preferred so we only keep the canonical host.
    (re.compile(
        r"\bhttps?://careers-([a-z0-9][a-z0-9-]+)\.icims\.com",
        re.IGNORECASE,
    ), 2),
    (re.compile(
        r"\bhttps?://([a-z0-9][a-z0-9-]+)\.icims\.com",
        re.IGNORECASE,
    ), 2),
    # Taleo: <slug>.taleo.net — subdomain is the tenant.
    (re.compile(
        r"\bhttps?://([a-z0-9][a-z0-9-]+)\.taleo\.net",
        re.IGNORECASE,
    ), 2),
    # SuccessFactors: career.successfactors.com/career?company=<slug>. We
    # store the prefix with the query fragment because the slug lives there;
    # the scanner treats this as a URL-prefix match, not a path match, so it
    # still works.
    (re.compile(
        r"\bhttps?://career\.successfactors\.com/career\?company=([a-z0-9][a-z0-9_-]+)",
        re.IGNORECASE,
    ), 2),
]


# Slugs that masquerade as tenants but are generic board sections (e.g. a
# nav link back to the ATS root). These never belong in ats_tenants.
_GENERIC_SLUGS = {
    "jobs", "careers", "career", "roles", "openings", "apply", "search",
    "about", "home", "board", "boards", "customers", "pricing", "demo",
    "login", "signup", "contact", "support", "help", "docs", "blog",
    "products", "product", "features", "solutions", "company",
}

# Subdomain slugs that should be ignored when they appear as the tenant
# position in a wildcard-subdomain ATS (Workable, Rippling, BambooHR,
# iCIMS, Taleo). These are the ATS vendor's own marketing/app subdomains,
# not customer tenants.
_GENERIC_SUBDOMAINS = {
    "www", "app", "api", "admin", "hub", "support", "help", "status",
    "docs", "blog", "dev", "staging", "demo",
}


def _normalize_tenant(host: str, *parts: str) -> str:
    """Join a host with optional path parts into the canonical prefix form
    we store in ats_tenants: no scheme, no trailing slash, no query string
    (except for the SuccessFactors pattern which we pass through verbatim).
    Lowercase because ATS hosts are case-insensitive."""
    host = host.lower().strip("/")
    clean = [p.strip("/").lower() for p in parts if p]
    if clean:
        return host + "/" + "/".join(clean)
    return host


def extract_ats_tenants(html: str) -> list[str]:
    """Scan a careers-page HTML blob for ATS tenant URLs and return a
    deduplicated list of canonical prefixes (first-match order preserved).
    """
    if not html:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for pattern, _ in _PATTERNS:
        for m in pattern.finditer(html):
            tenant = _match_to_tenant(pattern, m)
            if not tenant:
                continue
            if tenant in seen:
                continue
            seen.add(tenant)
            out.append(tenant)
    return out


def _match_to_tenant(pattern: re.Pattern, m: re.Match) -> str | None:
    """Turn one regex match into a normalized tenant prefix, filtering
    generic/junk slugs that would falsely pass for a real tenant."""
    url = m.group(0)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    host = host.lower()
    if not host:
        return None

    # Slug validation differs per ATS — the regex already captured it, but
    # the rules for "is this slug a real tenant" depend on the host.
    if host.endswith(".greenhouse.io") or host.endswith(".lever.co") \
       or host.endswith(".ashbyhq.com") or host == "ats.rippling.com" \
       or host == "apply.workable.com" \
       or host.endswith(".smartrecruiters.com") \
       or host.endswith(".jobvite.com"):
        slug = m.group(1).lower()
        if slug in _GENERIC_SLUGS or len(slug) < 2:
            return None
        return _normalize_tenant(host, slug)

    if host == "career.successfactors.com":
        slug = m.group(1).lower()
        if slug in _GENERIC_SLUGS or len(slug) < 2:
            return None
        # SuccessFactors keys on a query param, not a path. Preserve the
        # query form verbatim so URL-prefix matching finds the right jobs.
        return f"{host}/career?company={slug}"

    # Workday: host includes the subdomain tenant; path is a second tenant
    # segment we keep so we don't accidentally match a sibling tenant on
    # the same parent company.
    if host.endswith(".myworkdayjobs.com"):
        subdomain = host.split(".", 1)[0]
        if subdomain in _GENERIC_SUBDOMAINS or len(subdomain) < 2:
            return None
        path_tenant = m.group(2).lower()
        if path_tenant in _GENERIC_SLUGS or len(path_tenant) < 2:
            return None
        return _normalize_tenant(host, path_tenant)

    # Wildcard-subdomain ATSes where the subdomain IS the tenant.
    if host.endswith(".bamboohr.com") or host.endswith(".rippling-ats.com") \
       or host.endswith(".workable.com") or host.endswith(".icims.com") \
       or host.endswith(".taleo.net"):
        subdomain = host.split(".", 1)[0]
        # iCIMS hyphenated form: "careers-<slug>" — strip the prefix
        # so the tenant slug compares cleanly against generic-subdomain
        # blocklist. The host already encodes the canonical form we store.
        slug_for_check = subdomain
        if host.endswith(".icims.com") and subdomain.startswith("careers-"):
            slug_for_check = subdomain[len("careers-"):]
        if slug_for_check in _GENERIC_SUBDOMAINS or len(slug_for_check) < 2:
            return None
        # BambooHR prefix includes /jobs or /careers — we drop it because
        # the jobs might also live under /jobs/view/<id>, and we want the
        # URL-prefix match to cover all of them.
        return _normalize_tenant(host)

    return None


def discover_from_urls(urls: list[str], fetch) -> list[str]:
    """Walk a list of candidate careers URLs, fetch each, and aggregate
    tenant prefixes across all of them. `fetch` is injected so the caller
    controls the HTTP path (real fetcher in prod, fixture in tests).

    `fetch(url) -> str | None` — return raw HTML or None on failure.
    """
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not url:
            continue
        html = fetch(url)
        if not html:
            continue
        for tenant in extract_ats_tenants(html):
            if tenant in seen:
                continue
            seen.add(tenant)
            out.append(tenant)
    return out


def _careers_domain_to_urls(d: str) -> list[str]:
    """Turn a stored careers_domains entry (e.g. 'adeccogroup.com/careers'
    or 'careers.example.com') into a small list of absolute URLs to try.
    The stored value is a domain fragment, not a URL, so we synthesize
    https:// and also probe a couple of common alternate paths when the
    entry already has a path component."""
    d = (d or "").strip().strip("/")
    if not d:
        return []
    if "//" in d:
        d = d.split("//", 1)[1]
    # Build candidate URLs. Keep it small — two or three calls max per
    # careers_domains entry so discovery stays cheap.
    if "/" in d:
        return [f"https://{d}"]
    # Bare host — try /careers and /jobs as common conventions, plus root.
    return [
        f"https://{d}/careers",
        f"https://{d}/jobs",
        f"https://{d}",
    ]


def discover_for_competitor(careers_domains: list[str], fetch) -> list[str]:
    """Top-level entry point: expand each careers_domains entry into a
    handful of URLs and aggregate tenant prefixes across all of them.
    Returns an empty list if careers_domains is empty or every fetch
    fails — the caller should leave ats_tenants untouched in that case
    rather than clearing it, so a one-off fetch outage doesn't wipe
    state."""
    urls: list[str] = []
    seen: set[str] = set()
    for d in careers_domains or []:
        for u in _careers_domain_to_urls(d):
            if u in seen:
                continue
            seen.add(u)
            urls.append(u)
    return discover_from_urls(urls, fetch)
