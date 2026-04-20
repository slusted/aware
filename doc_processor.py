"""
Document Processor — reads strategy docs (PDFs, DOCX, TXT, MD) and builds
structured strategy profiles that the analyzer uses for context.

Docs go in:
  docs/seek/          — Seek's own strategy docs (annual reports, investor decks)
  docs/competitors/   — Competitor public docs (annual reports, filings)

On each run, all docs are read and summarised into a strategy profile JSON
stored at data/strategy_profiles.json. This profile is loaded by the analyzer
and injected into the system prompt so Claude analyses findings through the
lens of actual company strategy.
"""

import os
import json
import hashlib
import subprocess
from datetime import datetime

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
PROFILES_FILE = os.path.join(DATA_DIR, "strategy_profiles.json")

# ═══════════════════════════════════════════════════════════════
#  TEXT EXTRACTION
# ═══════════════════════════════════════════════════════════════

def extract_text_from_pdf(filepath: str) -> str:
    """Extract text from a PDF using pdfplumber (best quality) with pypdf fallback."""
    text = ""

    # Try pdfplumber first — best text extraction
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n\n"
        if text.strip():
            return text.strip()
    except ImportError:
        pass
    except Exception as e:
        print(f"  [docs] pdfplumber failed for {filepath}: {e}")

    # Fallback to pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n\n"
        if text.strip():
            return text.strip()
    except ImportError:
        pass
    except Exception as e:
        print(f"  [docs] pypdf failed for {filepath}: {e}")

    # Fallback to pdftotext CLI
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", filepath, "-"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print(f"  [docs] WARNING: Could not extract text from {filepath}")
    return ""


def extract_text_from_docx(filepath: str) -> str:
    """Extract text from a DOCX file using pandoc."""
    try:
        result = subprocess.run(
            ["pandoc", filepath, "-t", "plain", "--wrap=none"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: read as zip and extract text from XML
    try:
        import zipfile
        import re
        with zipfile.ZipFile(filepath) as z:
            with z.open("word/document.xml") as f:
                xml = f.read().decode("utf-8")
            # Strip XML tags, keep text
            text = re.sub(r"<[^>]+>", " ", xml)
            text = re.sub(r"\s+", " ", text).strip()
            return text
    except Exception as e:
        print(f"  [docs] DOCX extraction failed for {filepath}: {e}")
        return ""


def extract_text(filepath: str) -> str:
    """Extract text from any supported document type."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        return extract_text_from_pdf(filepath)
    elif ext in (".docx", ".doc"):
        return extract_text_from_docx(filepath)
    elif ext in (".txt", ".md", ".markdown"):
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    else:
        print(f"  [docs] Unsupported file type: {ext}")
        return ""


# ═══════════════════════════════════════════════════════════════
#  FILE DISCOVERY
# ═══════════════════════════════════════════════════════════════

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown"}


def discover_docs() -> dict:
    """Find all docs in the docs/ folder, organized by category.
    Returns: {"seek": [filepath, ...], "competitors": {name: [filepath, ...]}}
    """
    result = {"seek": [], "competitors": {}}

    seek_dir = os.path.join(DOCS_DIR, "seek")
    comp_dir = os.path.join(DOCS_DIR, "competitors")

    # Seek docs
    if os.path.isdir(seek_dir):
        for f in sorted(os.listdir(seek_dir)):
            if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS:
                result["seek"].append(os.path.join(seek_dir, f))

    # Competitor docs — organized by subfolder (e.g., docs/competitors/linkedin/)
    if os.path.isdir(comp_dir):
        for dirname in sorted(os.listdir(comp_dir)):
            subdir = os.path.join(comp_dir, dirname)
            if os.path.isdir(subdir):
                files = []
                for f in sorted(os.listdir(subdir)):
                    if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS:
                        files.append(os.path.join(subdir, f))
                if files:
                    result["competitors"][dirname] = files
            elif os.path.splitext(dirname)[1].lower() in SUPPORTED_EXTENSIONS:
                # Also support flat files in competitors/ root
                name = os.path.splitext(dirname)[0].lower()
                if name not in result["competitors"]:
                    result["competitors"][name] = []
                result["competitors"][name].append(os.path.join(comp_dir, dirname))

    return result


def file_hash(filepath: str) -> str:
    """Hash file contents for change detection."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


# ═══════════════════════════════════════════════════════════════
#  SUMMARISATION — uses Claude to extract strategy profiles
# ═══════════════════════════════════════════════════════════════

def summarise_with_claude(text: str, doc_type: str, entity_name: str) -> dict:
    """Use Claude to summarise a document into a structured strategy profile.

    doc_type: "own_company" or "competitor"
    entity_name: e.g., "Seek" or "LinkedIn"
    """
    import anthropic

    if doc_type == "own_company":
        prompt = f"""Analyze this document from {entity_name} and extract a structured strategy profile.
Return ONLY valid JSON with these fields:

{{
    "company": "{entity_name}",
    "doc_source": "internal strategy document",
    "summary": "2-3 sentence overview of the document",
    "strategic_priorities": ["priority 1", "priority 2", ...],
    "key_metrics": {{"metric_name": "value or target", ...}},
    "investments": ["area 1", "area 2", ...],
    "markets": ["market 1", "market 2", ...],
    "competitive_positioning": "How they position themselves vs competitors",
    "risks_and_challenges": ["risk 1", "risk 2", ...],
    "growth_areas": ["area 1", "area 2", ...],
    "technology_focus": ["tech area 1", "tech area 2", ...],
    "notable_quotes": ["key quote or statement that reveals strategy"]
}}

Be specific — extract actual numbers, targets, and named initiatives, not generic summaries.
If a field has no relevant information in the document, use an empty list or empty string.

Document text (may be truncated):
{text[:15000]}"""
    else:
        prompt = f"""Analyze this public document from {entity_name} (a competitor) and extract a structured profile.
Return ONLY valid JSON with these fields:

{{
    "company": "{entity_name}",
    "doc_source": "public competitor document",
    "summary": "2-3 sentence overview",
    "stated_strategy": "What they say their strategy is",
    "product_focus": ["product area 1", "product area 2", ...],
    "revenue_model": "How they make money",
    "key_metrics": {{"metric_name": "value", ...}},
    "markets": ["market 1", "market 2", ...],
    "investments": ["investment area 1", ...],
    "strengths": ["strength 1", ...],
    "weaknesses_or_risks": ["weakness 1", ...],
    "threat_to_seek": "How this competitor threatens Seek specifically",
    "opportunities_for_seek": "Where Seek could exploit gaps"
}}

Be specific — extract actual numbers, names, and details, not generic summaries.
If a field has no relevant information, use an empty list or empty string.

Document text (may be truncated):
{text[:15000]}"""

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Fast + cheap for extraction
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    # Parse JSON from response
    response_text = response.content[0].text.strip()
    # Handle markdown code blocks
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0].strip()

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        print(f"  [docs] WARNING: Failed to parse Claude's response as JSON for {entity_name}")
        return {"company": entity_name, "summary": response_text[:500], "parse_error": True}


# ═══════════════════════════════════════════════════════════════
#  MAIN PROCESSING PIPELINE
# ═══════════════════════════════════════════════════════════════

def load_profiles() -> dict:
    """Load existing strategy profiles."""
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [docs] WARNING: could not load {PROFILES_FILE} ({e}); using empty profiles")
    return {"seek": {}, "competitors": {}, "file_hashes": {}, "last_processed": None}


def save_profiles(profiles: dict):
    """Save strategy profiles to disk."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=2)


def _cache_key(filepath: str) -> str:
    """Normalize a file path for use as a cache key.
    On Windows, filesystems are case-insensitive but dict lookups are case-sensitive,
    so 'C:\\...\\Documents\\...' and 'C:\\...\\documents\\...' are the same file but
    different dict keys. normcase lowercases + normalizes separators on Windows;
    on POSIX it's a no-op.
    """
    return os.path.normcase(os.path.abspath(filepath))


def _migrate_file_hashes(profiles: dict) -> bool:
    """One-time cleanup: rewrite file_hashes dict with normalized keys, dedup.
    Returns True if any changes were made (so caller can mark dirty and save).
    """
    raw = profiles.get("file_hashes", {})
    if not raw:
        return False
    normalized = {}
    for path, h in raw.items():
        normalized[_cache_key(path)] = h  # last write wins on collision
    if normalized == raw:
        return False
    profiles["file_hashes"] = normalized
    removed = len(raw) - len(normalized)
    print(f"  [docs] Migrated file_hashes: {len(raw)} entries → {len(normalized)} "
          f"({removed} duplicate{'s' if removed != 1 else ''} removed)")
    return True


def process_docs(force: bool = False) -> dict:
    """Process all docs in docs/ and build strategy profiles.

    Only re-processes files that have changed since last run (unless force=True).
    Returns the updated profiles dict.
    """
    docs = discover_docs()
    profiles = load_profiles()
    changed = _migrate_file_hashes(profiles)

    # Process Seek docs
    for filepath in docs["seek"]:
        fhash = file_hash(filepath)
        fname = os.path.basename(filepath)
        key_path = _cache_key(filepath)
        if not force and profiles.get("file_hashes", {}).get(key_path) == fhash:
            print(f"  [docs] Skipping {fname} (unchanged)")
            continue

        print(f"  [docs] Processing Seek doc: {fname}")
        text = extract_text(filepath)
        if not text:
            continue

        profile = summarise_with_claude(text, "own_company", "Seek")
        profile["_file"] = fname
        profile["_processed"] = datetime.now().isoformat()

        # Store under a key based on filename
        key = os.path.splitext(fname)[0].lower().replace(" ", "_")
        if "seek" not in profiles:
            profiles["seek"] = {}
        profiles["seek"][key] = profile
        profiles.setdefault("file_hashes", {})[key_path] = fhash
        changed = True

    # Process competitor docs
    for comp_name, filepaths in docs["competitors"].items():
        for filepath in filepaths:
            fhash = file_hash(filepath)
            fname = os.path.basename(filepath)
            key_path = _cache_key(filepath)
            if not force and profiles.get("file_hashes", {}).get(key_path) == fhash:
                print(f"  [docs] Skipping {fname} (unchanged)")
                continue

            print(f"  [docs] Processing competitor doc: {comp_name}/{fname}")
            text = extract_text(filepath)
            if not text:
                continue

            profile = summarise_with_claude(text, "competitor", comp_name.title())
            profile["_file"] = fname
            profile["_processed"] = datetime.now().isoformat()

            if "competitors" not in profiles:
                profiles["competitors"] = {}
            if comp_name not in profiles["competitors"]:
                profiles["competitors"][comp_name] = {}
            key = os.path.splitext(fname)[0].lower().replace(" ", "_")
            profiles["competitors"][comp_name][key] = profile
            profiles.setdefault("file_hashes", {})[key_path] = fhash
            changed = True

    if changed:
        profiles["last_processed"] = datetime.now().isoformat()
        save_profiles(profiles)
        print(f"  [docs] Strategy profiles updated")
    else:
        print(f"  [docs] No document changes detected")

    return profiles


def build_strategy_context(profiles: dict = None) -> str:
    """Build a text summary of all strategy profiles for injection into the analyzer prompt.
    Returns a formatted string ready to be included in a system prompt.
    """
    if profiles is None:
        profiles = load_profiles()

    sections = []

    # Seek's own strategy
    seek_profiles = profiles.get("seek", {})
    if seek_profiles:
        sections.append("## YOUR COMPANY — SEEK\n")
        sections.append("The following is extracted from Seek's own strategy documents. "
                       "Use this to evaluate competitor moves against Seek's actual priorities.\n")
        for key, profile in seek_profiles.items():
            if isinstance(profile, dict) and not profile.get("parse_error"):
                sections.append(f"### {profile.get('_file', key)}")
                if profile.get("summary"):
                    sections.append(f"**Summary:** {profile['summary']}")
                if profile.get("strategic_priorities"):
                    sections.append(f"**Strategic priorities:** {', '.join(profile['strategic_priorities'])}")
                if profile.get("investments"):
                    sections.append(f"**Investment areas:** {', '.join(profile['investments'])}")
                if profile.get("growth_areas"):
                    sections.append(f"**Growth areas:** {', '.join(profile['growth_areas'])}")
                if profile.get("technology_focus"):
                    sections.append(f"**Technology focus:** {', '.join(profile['technology_focus'])}")
                if profile.get("competitive_positioning"):
                    sections.append(f"**Competitive positioning:** {profile['competitive_positioning']}")
                if profile.get("risks_and_challenges"):
                    sections.append(f"**Risks:** {', '.join(profile['risks_and_challenges'])}")
                if profile.get("key_metrics"):
                    metrics = "; ".join(f"{k}: {v}" for k, v in profile["key_metrics"].items())
                    sections.append(f"**Key metrics:** {metrics}")
                sections.append("")

    # Competitor profiles from docs
    comp_profiles = profiles.get("competitors", {})
    if comp_profiles:
        sections.append("\n## COMPETITOR STRATEGY (from their public documents)\n")
        sections.append("Cross-reference what competitors SAY they're doing (below) with what "
                       "the scan ACTUALLY finds them doing. Gaps between stated strategy and "
                       "observed actions are particularly interesting.\n")
        for comp_name, docs in comp_profiles.items():
            sections.append(f"### {comp_name.title()}")
            for key, profile in docs.items():
                if isinstance(profile, dict) and not profile.get("parse_error"):
                    if profile.get("summary"):
                        sections.append(f"**Summary:** {profile['summary']}")
                    if profile.get("stated_strategy"):
                        sections.append(f"**Stated strategy:** {profile['stated_strategy']}")
                    if profile.get("product_focus"):
                        sections.append(f"**Product focus:** {', '.join(profile['product_focus'])}")
                    if profile.get("threat_to_seek"):
                        sections.append(f"**Threat to Seek:** {profile['threat_to_seek']}")
                    if profile.get("opportunities_for_seek"):
                        sections.append(f"**Opportunities for Seek:** {profile['opportunities_for_seek']}")
                    if profile.get("strengths"):
                        sections.append(f"**Strengths:** {', '.join(profile['strengths'])}")
                    if profile.get("weaknesses_or_risks"):
                        sections.append(f"**Weaknesses:** {', '.join(profile['weaknesses_or_risks'])}")
            sections.append("")

    return "\n".join(sections) if sections else ""


# ═══════════════════════════════════════════════════════════════
#  CLI — can be run standalone for testing
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # Create docs directory structure if it doesn't exist
    os.makedirs(os.path.join(DOCS_DIR, "seek"), exist_ok=True)
    os.makedirs(os.path.join(DOCS_DIR, "competitors"), exist_ok=True)

    force = "--force" in sys.argv
    if force:
        print("Force re-processing all documents...")

    docs = discover_docs()
    print(f"Found {len(docs['seek'])} Seek docs, "
          f"{sum(len(v) for v in docs['competitors'].values())} competitor docs")

    if not docs["seek"] and not docs["competitors"]:
        print(f"\nNo documents found. Add files to:")
        print(f"  {os.path.join(DOCS_DIR, 'seek/')}")
        print(f"  {os.path.join(DOCS_DIR, 'competitors', '<competitor_name>/')}")
        print(f"\nSupported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    else:
        profiles = process_docs(force=force)
        context = build_strategy_context(profiles)
        print(f"\n--- Strategy Context ({len(context)} chars) ---")
        print(context[:2000] if context else "(empty)")
