"""Content sanitization — shared across fetchers and search adapters.

One home for every "clean the noise out of an extracted page" rule:
boilerplate stripping, noise/chrome/bot-wall classification, and the
domain exclude list that search providers use to skip hopeless sources.

Prior to this module the logic lived in scanner.py and was imported back
into app/fetcher.py, app/customer_watch.py, and competitor_manager.py.
"""

import re


# ---------------------------------------------------------------------------
# Domain policy

EXCLUDE_DOMAINS = [
    # Public job aggregators — they advertise every employer's jobs, so a
    # competitor-name query against them is dominated by unrelated listings.
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "totaljobs.com", "stepstone.de", "stepstone.com", "seek.com.au",
    "monster.com", "careerbuilder.com", "simplyhired.com",
    "salary.com", "payscale.com",
    # Startup / tech-specific boards — same problem: the "competitor" name
    # shows up as one tenant among thousands, which doesn't reflect their
    # strategy, only the board's coverage.
    "wellfound.com", "angel.co", "builtin.com", "builtinnyc.com",
    "builtinla.com", "builtinchicago.org", "builtinboston.com",
    "builtinaustin.com", "builtinsf.com", "builtincolorado.com",
    "dice.com", "hired.com", "otta.com", "welcometothejungle.com",
    "lensa.com", "flexjobs.com", "remoteok.com", "remote.co",
    "weworkremotely.com",
    # Recruitment agencies — they post client jobs, not their own hiring
    # signals. Keeping them out protects the `new_hire` channel from
    # "Robert Half is hiring a software engineer for <our competitor>"
    # style noise, which reads as a competitor hire but isn't.
    "roberthalf.com", "roberthalf.com.au", "roberthalf.co.uk",
    "michaelpage.com", "pagepersonnel.com", "hays.com",
    "randstad.com", "randstad.com.au", "randstad.co.uk",
    "manpowergroup.com", "manpower.com",
    "kellyservices.com", "kellyservices.com.au",
    # Generic ATS root hosts — we now scope ATS queries to per-competitor
    # tenant prefixes, so a result landing on the bare ATS root (which
    # hosts every customer) is by definition noise for this competitor.
    # Keep these last; override by adding the competitor's tenant to
    # ats_tenants so the scoped sweep picks it up.
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "smartrecruiters.com", "jobvite.com", "icims.com", "taleo.net",
    "bamboohr.com", "rippling-ats.com",
]


# ---------------------------------------------------------------------------
# Size bound

# Safety bound only — prevents a misbehaving site from dumping megabytes of
# content into one Finding row. Set very high so normal articles (incl. long
# podcast transcripts and analyst deep-dives) pass through untouched.
# Downstream consumers (analyzer, competitor_reports) clip independently for
# LLM context, so changing this does NOT affect digest prompt size.
RAW_CONTENT_MAX = 500_000


# ---------------------------------------------------------------------------
# Pattern banks

NOISE_PATTERNS = [
    "jobs available on", "jobs hiring now", "apply to ", "apply now",
    "browse ", " jobs ($", "openings posted daily", "1-click apply",
    "find job postings near", "job openings from", "hiring now",
    "salary range", "per hour", "/hr)", "/yr)", "entry-level to senior",
    "jobs in your area", "sign up for job alerts",
]

# Patterns that indicate the extractor hit a login wall / social media chrome
# instead of real content. X/Twitter and (less often) LinkedIn return
# navigation shells when fetched without JS/auth.
CHROME_PATTERNS = [
    "don't miss what's happening",
    "people on x are the first to know",
    "sign up for x",
    "joined twitter",
    "joined october", "joined january", "joined february", "joined march",
    "joined april", "joined may", "joined june", "joined july",
    "joined august", "joined september", "joined november", "joined december",
    "posts see new posts",
    "click to follow",
    "log in to twitter",
    "opens profile photo",
    "verified_followers",
    # Reddit login overlay — if clean_extracted's truncate didn't leave any
    # real post content above it, these phrases are what remains. Two of them
    # together = the page extracted as pure chrome, reject it.
    "new to reddit?",
    "create your account and connect with a world of communities",
    "continue with google",
    "continue with email",
    "continue with phone number",
]

# Bot-challenge / anti-scraping interstitial patterns. Stricter than CHROME —
# one match is enough to reject (these phrases don't appear in real articles).
# Cloudflare protects indeed.com, glassdoor.com, ziprecruiter.com, and many
# others — urllib fetches get the challenge page instead of content.
BOT_WALL_PATTERNS = [
    "just a moment",                       # Cloudflare challenge page title
    "ray id",                              # Cloudflare fingerprint
    "additional verification required",    # Cloudflare / CF-Turnstile
    "verification successful. waiting",    # Cloudflare post-challenge
    "checking your browser",               # Cloudflare IUAM
    "enable javascript and cookies to continue",
    "please turn javascript on",
    "attention required! | cloudflare",
    "access denied",
    "you are being rate limited",
    "this site can't be reached",
]


# ---------------------------------------------------------------------------
# Functions

def clean_extracted(text: str) -> str:
    """Strip boilerplate that otherwise eats the char budget before real content:
      - markdown image tags ![alt](url) and bare image lines
      - empty-anchor [](url)
      - navigation shells ('Skip to content', 'Login', repeated menu items)
      - Reddit / social login overlays that append *after* the real post body
      - excessive blank lines
    """
    if not text:
        return ""
    t = text
    # Markdown images — never useful, often long data:image URLs
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    # Empty-text anchors
    t = re.sub(r"\[\]\([^)]*\)", "", t)
    # Known nav phrases (line starts)
    nav = r"^(Skip to content|Log in|Sign up|Search|Menu|Subscribe|Follow|Share)\s*$"
    t = re.sub(nav, "", t, flags=re.MULTILINE | re.IGNORECASE)
    # Reddit login overlay always lands at the *end* of an extracted post page.
    # Truncate at the earliest overlay marker so the OP + visible comments
    # survive and the sign-up chrome doesn't.
    overlay_markers = [
        "New to Reddit?",
        "Create your account and connect with a world of communities",
        "Continue with Google Continue with Google",
        "Continue With Phone Number",
        "By continuing, you agree to our",
    ]
    cut = len(t)
    for m in overlay_markers:
        idx = t.find(m)
        if idx != -1 and idx < cut:
            cut = idx
    if cut < len(t):
        t = t[:cut]
    # Collapse 3+ blank lines to 2
    t = re.sub(r"\n{3,}", "\n\n", t)
    # Strip leading/trailing whitespace on each line but keep structure
    t = "\n".join(line.rstrip() for line in t.splitlines())
    return t.strip()


def is_noise(text: str) -> bool:
    """Return True if the result looks like a job listing rather than intelligence."""
    if not isinstance(text, str):
        return False
    lower = text.lower()
    matches = sum(1 for p in NOISE_PATTERNS if p in lower)
    return matches >= 2  # needs 2+ noise signals to be filtered


def is_chrome(text: str) -> bool:
    """Return True if the result is social-media site chrome (login wall,
    profile header, sign-up prompt) rather than actual content."""
    if not isinstance(text, str):
        return False
    lower = text.lower()
    matches = sum(1 for p in CHROME_PATTERNS if p in lower)
    return matches >= 2


def is_bot_wall(text: str) -> bool:
    """Return True if the text looks like a bot-challenge / anti-scrape
    interstitial (Cloudflare, Akamai, rate-limit pages). One match is enough
    since these phrases don't appear in legitimate article bodies."""
    if not isinstance(text, str):
        return False
    lower = text.lower()
    return any(p in lower for p in BOT_WALL_PATTERNS)
