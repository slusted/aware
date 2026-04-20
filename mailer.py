"""
Mailer — send digests to the team and check for replies.
"""

import os
import re
import email
import imaplib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime


def _strip_html(html: str) -> str:
    """Minimal HTML → text: drop script/style, convert <br>/<p> to newlines, strip tags."""
    if not html:
        return ""
    # Drop script and style blocks entirely
    html = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    # Preserve line breaks
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p\s*>", "\n\n", html, flags=re.I)
    html = re.sub(r"</div\s*>", "\n", html, flags=re.I)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", html)
    # Unescape common HTML entities
    import html as _html
    text = _html.unescape(text)
    # Collapse whitespace runs but keep newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SUBJECT_PREFIX = "Competitor Watch"


def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email via Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print(f"  [mail] Skipping email (no credentials) — would send to {to}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)
        print(f"  [mail] Sent to {to}")
        return True
    except Exception as e:
        print(f"  [mail] Error sending to {to}: {e}")
        return False


def send_digest_to_team(digest: str, config: dict, findings: list[dict]):
    """Send the digest to each team member, filtered by their competitor preferences."""
    today = datetime.now().strftime("%B %d, %Y")
    company = config["company"]

    for member in config["team"]:
        # Filter findings per member
        member_competitors = member.get("competitors", "all")
        if member_competitors == "all":
            member_findings = findings
        else:
            member_findings = [f for f in findings if f["competitor"] in member_competitors]

        # Build personalized digest
        subject = f"{SUBJECT_PREFIX} — {company} — {today}"

        if member_competitors != "all" and member_findings:
            # Custom filtered digest for this member
            body = f"# {subject}\n\n"
            body += f"Hi {member['name']}, here's your filtered digest "
            body += f"({', '.join(member_competitors)}):\n\n"
            body += digest  # still send full analysis, they can skim
        else:
            body = digest

        if member.get("can_reply"):
            body += "\n\n---\n_Reply to this email to ask follow-up questions._"

        send_email(member["email"], subject, body)


def check_for_replies(config: dict) -> list[dict]:
    """Check for team replies to digest emails."""
    if not GMAIL_USER or not GMAIL_PASSWORD:
        return []

    # Build list of allowed reply addresses
    allowed = {m["email"].lower() for m in config["team"] if m.get("can_reply")}

    replies = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        mail.select("inbox")

        status, data = mail.search(None, f'(UNSEEN SUBJECT "{SUBJECT_PREFIX}")')
        if status != "OK" or not data[0]:
            mail.logout()
            return []

        for msg_id in data[0].split():
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            subject = msg.get("Subject", "")

            # Only replies
            if not subject.lower().startswith("re:"):
                continue

            # Check sender is allowed
            from_addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            if from_addr not in allowed:
                print(f"  [mail] Ignoring reply from unauthorized address: {from_addr}")
                mail.store(msg_id, "+FLAGS", "\\Seen")
                continue

            # Extract reply body — prefer text/plain, fall back to text/html
            body = ""
            html_body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain" and not body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")
                    elif ctype == "text/html" and not html_body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_body = payload.decode("utf-8", errors="ignore")
            else:
                payload = msg.get_payload(decode=True) or b""
                decoded = payload.decode("utf-8", errors="ignore")
                if msg.get_content_type() == "text/html":
                    html_body = decoded
                else:
                    body = decoded

            if not body.strip() and html_body:
                body = _strip_html(html_body)

            lines = body.split("\n")
            clean = []
            for line in lines:
                if line.strip().startswith("On ") and "wrote:" in line:
                    break
                if line.strip().startswith(">"):
                    continue
                clean.append(line)

            reply_text = "\n".join(clean).strip()
            if reply_text:
                replies.append({
                    "from": from_addr,
                    "from_name": email.utils.parseaddr(msg.get("From", ""))[0],
                    "subject": subject,
                    "body": reply_text,
                })

            mail.store(msg_id, "+FLAGS", "\\Seen")

        mail.logout()
    except Exception as e:
        print(f"  [mail] IMAP error: {e}")

    return replies
