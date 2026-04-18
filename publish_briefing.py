#!/usr/bin/env python3
"""
Publish the daily AI briefing to the briefing.ambient-advantage.ai website.

This script:
  1. Reads the most recently sent newsletter from Gmail (Sent Mail)
  2. Extracts the headline, snippet, and full HTML body
  3. Generates a dated article page from the template
  4. Updates index.html to feature the new article (rotates cards)
  5. Commits and pushes to the GitHub repo → Cloudflare auto-deploys

Run manually:
    python publish_briefing.py

Environment variables required:
    GMAIL_OAUTH_TOKEN_JSON  — OAuth token JSON string (or path to file)
    BRIEFING_REPO_PATH      — Local path to the briefing-site git repo
    GIT_PUSH                — Set to "true" to actually push (default: dry run)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Toronto")

# ---------------------------------------------------------------------------
# Gmail: fetch most recently sent newsletter
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_source = os.environ.get("GMAIL_OAUTH_TOKEN_JSON", "")
    if os.path.isfile(token_source):
        with open(token_source) as f:
            token_json = f.read()
    else:
        token_json = token_source

    if not token_json:
        raise RuntimeError("Set GMAIL_OAUTH_TOKEN_JSON (JSON string or file path)")

    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, scopes=SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Gmail credentials invalid and cannot refresh")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_b64url(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _walk_parts(parts: list[dict]) -> tuple[str, str]:
    html, plain = "", ""
    for part in parts:
        if part.get("parts"):
            sub_html, sub_plain = _walk_parts(part["parts"])
            html = html or sub_html
            plain = plain or sub_plain
            continue
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data:
            continue
        decoded = _decode_b64url(data)
        if mime == "text/html" and not html:
            html = decoded
        elif mime == "text/plain" and not plain:
            plain = decoded
    return html, plain


def _extract_bodies(message: dict) -> tuple[str, str]:
    payload = message.get("payload", {})
    if payload.get("parts"):
        return _walk_parts(payload["parts"])
    data = payload.get("body", {}).get("data", "")
    if not data:
        return "", ""
    decoded = _decode_b64url(data)
    if payload.get("mimeType") == "text/html":
        return decoded, ""
    return "", decoded


@dataclass
class SentNewsletter:
    date: str           # YYYY-MM-DD
    subject: str
    html_body: str
    plain_body: str


def fetch_latest_sent_newsletter() -> SentNewsletter | None:
    """Find the most recently sent newsletter email in Gmail."""
    svc = _gmail_service()

    query = 'in:sent subject:("AI Briefing" OR "Ambient Advantage") newer_than:3d'
    resp = svc.users().messages().list(userId="me", q=query, maxResults=5).execute()
    messages = resp.get("messages", [])
    if not messages:
        log.warning("No sent newsletters found in the last 3 days")
        return None

    # Get the most recent one
    msg = svc.users().messages().get(userId="me", id=messages[0]["id"], format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("subject", "")
    date_str = headers.get("date", "")

    html, plain = _extract_bodies(msg)
    if not html and not plain:
        log.warning("Sent newsletter has empty body")
        return None

    # Parse date from email headers
    internal_date_ms = int(msg.get("internalDate", "0"))
    dt = datetime.fromtimestamp(internal_date_ms / 1000, tz=LOCAL_TZ)
    date_key = dt.strftime("%Y-%m-%d")

    log.info("Found sent newsletter: %s (%s)", subject, date_key)
    return SentNewsletter(date=date_key, subject=subject, html_body=html, plain_body=plain)


# ---------------------------------------------------------------------------
# HTML parsing: extract headline + snippet from newsletter email
# ---------------------------------------------------------------------------

class TextExtractor(HTMLParser):
    """Extract visible text from HTML, stripping tags."""
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)


def extract_headline_and_snippet(html: str, subject: str) -> tuple[str, str]:
    """Pull the main headline and a snippet from the newsletter HTML.

    Strategy:
      - Look for the first <h1>, <h2>, or <h3> tag content as the headline
      - Fallback to the email subject line (minus the emoji prefix)
      - Snippet = first ~200 chars of body text after the headline
    """
    # Try to find first heading
    heading_match = re.search(r'<h[123][^>]*>(.*?)</h[123]>', html, re.DOTALL | re.IGNORECASE)
    if heading_match:
        headline_html = heading_match.group(1)
        # Strip any nested tags
        headline = re.sub(r'<[^>]+>', '', headline_html).strip()
    else:
        # Fall back to subject line, strip emoji prefix
        headline = re.sub(r'^[^\w]*', '', subject).strip()
        # Remove common prefixes
        for prefix in ["Chiel's AI Briefing —", "Chiel's AI Briefing -", "AI Briefing —", "AI Briefing -"]:
            if headline.startswith(prefix):
                headline = headline[len(prefix):].strip()
                break

    # Extract body text for snippet
    extractor = TextExtractor()
    extractor.feed(html)
    all_text = " ".join(extractor.text_parts)

    # Skip past the headline in the text to get the snippet
    headline_pos = all_text.find(headline[:30]) if headline else 0
    if headline_pos > 0:
        snippet_text = all_text[headline_pos + len(headline):]
    else:
        snippet_text = all_text

    # Clean up and truncate
    snippet_text = re.sub(r'\s+', ' ', snippet_text).strip()
    # Try to cut at a sentence boundary around 200 chars
    if len(snippet_text) > 250:
        cut = snippet_text[:250].rfind('. ')
        if cut > 100:
            snippet_text = snippet_text[:cut + 1]
        else:
            snippet_text = snippet_text[:250].rsplit(' ', 1)[0] + "..."

    return headline, snippet_text


# ---------------------------------------------------------------------------
# Generate article HTML page
# ---------------------------------------------------------------------------

def _format_date_long(date_key: str) -> str:
    """'2026-04-10' → 'Friday · April 10, 2026'"""
    dt = datetime.strptime(date_key, "%Y-%m-%d")
    return dt.strftime("%A · %B %-d, %Y")


def _format_date_short(date_key: str) -> str:
    """'2026-04-10' → 'Fri · Apr 10, 2026'"""
    dt = datetime.strptime(date_key, "%Y-%m-%d")
    return dt.strftime("%a · %b %-d, %Y")


def _day_and_month(date_key: str) -> tuple[str, str]:
    """'2026-04-10' → ('10', 'Apr')"""
    dt = datetime.strptime(date_key, "%Y-%m-%d")
    return dt.strftime("%-d").zfill(2), dt.strftime("%b")


def _estimate_read_time(html: str) -> int:
    """Estimate reading time in minutes from HTML word count."""
    text = re.sub(r'<[^>]+>', ' ', html)
    words = len(text.split())
    return max(2, round(words / 220))


def generate_article_page(
    date_key: str,
    headline: str,
    snippet: str,
    newsletter_html: str,
    prev_date: str | None = None,
    prev_headline: str | None = None,
    next_date: str | None = None,
    next_headline: str | None = None,
) -> str:
    """Generate the full article HTML page for a single newsletter."""
    read_time = _estimate_read_time(newsletter_html)
    long_date = _format_date_long(date_key)

    # Clean the newsletter HTML for embedding:
    # Remove <html>, <head>, <body> wrappers if present — we just want the content
    content = newsletter_html
    content = re.sub(r'<!doctype[^>]*>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'<html[^>]*>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'</html>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'<head>.*?</head>', '', content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r'<body[^>]*>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'</body>', '', content, flags=re.IGNORECASE)
    # Strip inline styles that would clash with the site theme
    # but keep structural elements

    # Build prev/next nav
    nav_items = ""
    if prev_date and prev_headline:
        nav_items += f'''    <a href="/{prev_date}.html">
      <div class="label">← Previous briefing</div>
      <div class="title">{_escape_html(prev_headline)}</div>
    </a>\n'''
    if next_date and next_headline:
        nav_items += f'''    <a href="/{next_date}.html">
      <div class="label">Next briefing →</div>
      <div class="title">{_escape_html(next_headline)}</div>
    </a>\n'''

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{_escape_html(headline)} — Daily Briefing · Ambient Advantage</title>
<meta name="description" content="{_escape_html(snippet[:160])}" />

<meta property="og:type" content="article" />
<meta property="og:url" content="https://briefing.ambient-advantage.ai/{date_key}.html" />
<meta property="og:title" content="{_escape_html(headline)}" />
<meta property="og:description" content="{_escape_html(snippet[:160])}" />
<meta property="og:image" content="https://podcast.ambient-advantage.ai/og-image.png" />

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{{
    --bg-0:#05070d;--bg-1:#0a0f1c;--bg-2:#0f1628;
    --line:rgba(120,170,255,.14);--text:#e8eefc;--mute:#8a97b4;
    --accent:#4ea8ff;--accent-2:#7ad0ff;
    --glow:0 0 40px rgba(78,168,255,.35);
  }}
  *{{box-sizing:border-box}}
  html,body{{margin:0;padding:0;background:var(--bg-0);color:var(--text);font-family:'Inter',system-ui,sans-serif;-webkit-font-smoothing:antialiased}}
  a{{color:var(--accent);text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .container{{max-width:1180px;margin:0 auto;padding:0 28px}}

  nav.top{{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);background:rgba(5,7,13,.6);border-bottom:1px solid var(--line)}}
  nav.top .row{{display:flex;align-items:center;justify-content:space-between;padding:18px 0}}
  .logo{{font-family:'Space Grotesk';font-weight:700;letter-spacing:.02em;font-size:18px;display:flex;align-items:center;gap:10px;color:var(--text);text-decoration:none}}
  .logo .dot{{width:10px;height:10px;border-radius:50%;background:var(--accent);box-shadow:var(--glow)}}
  .nav-links{{display:flex;gap:32px;font-size:14px;color:var(--mute)}}
  .nav-links a{{color:var(--mute);text-decoration:none}}
  .nav-links a:hover{{color:var(--text);text-decoration:none}}
  .nav-links a.active{{color:var(--text);border-bottom:2px solid var(--accent);padding-bottom:2px}}
  .nav-cta{{border:1px solid var(--line);padding:9px 16px;border-radius:999px;font-size:13px;color:var(--text);background:linear-gradient(180deg,rgba(78,168,255,.12),rgba(78,168,255,.02));text-decoration:none;transition:border-color .2s}}
  .nav-cta:hover{{border-color:var(--accent);text-decoration:none}}

  .article-header{{padding:80px 0 40px;position:relative}}
  .article-header::before{{content:"";position:absolute;inset:0;background:radial-gradient(ellipse 80% 50% at 50% 0%,rgba(78,168,255,.1),transparent 60%);pointer-events:none}}
  .article-header .inner{{position:relative;z-index:2;max-width:780px;margin:0 auto}}
  .back-link{{display:inline-flex;align-items:center;gap:6px;font-family:'Space Grotesk';font-size:13px;color:var(--mute);letter-spacing:.05em;margin-bottom:28px;text-decoration:none;transition:color .2s}}
  .back-link:hover{{color:var(--accent);text-decoration:none}}
  .article-meta{{display:flex;align-items:center;gap:16px;font-size:13px;color:var(--mute);font-family:'Space Grotesk';margin-bottom:18px;flex-wrap:wrap}}
  .article-meta .tag{{padding:4px 10px;border-radius:999px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;background:rgba(78,168,255,.1);color:var(--accent-2);border:1px solid rgba(78,168,255,.2)}}
  .article-header h1{{font-family:'Space Grotesk';font-weight:700;font-size:clamp(30px,4.5vw,48px);line-height:1.12;margin:0;letter-spacing:-.02em}}

  .article-body{{max-width:780px;margin:0 auto;padding:40px 0 80px}}
  .article-body p{{font-size:17px;line-height:1.8;color:var(--text);margin:0 0 24px}}
  .article-body h1,.article-body h2{{font-family:'Space Grotesk';font-size:26px;margin:48px 0 16px;letter-spacing:-.01em;color:var(--text)}}
  .article-body h3{{font-family:'Space Grotesk';font-size:20px;margin:36px 0 12px;color:var(--text)}}
  .article-body a{{color:var(--accent)}}
  .article-body blockquote{{border-left:3px solid var(--accent);margin:28px 0;padding:16px 24px;background:rgba(78,168,255,.04);border-radius:0 12px 12px 0;font-style:italic;color:var(--mute)}}
  .article-body ul,.article-body ol{{padding-left:24px;margin:0 0 24px}}
  .article-body li{{font-size:17px;line-height:1.8;margin-bottom:8px}}
  .article-body img{{max-width:100%;border-radius:12px}}
  .article-body hr{{border:none;border-top:1px solid var(--line);margin:40px 0}}
  .article-body table{{width:100%;border-collapse:collapse;margin:24px 0}}
  .article-body th,.article-body td{{padding:10px 14px;border:1px solid var(--line);font-size:15px;text-align:left}}
  .article-body th{{background:var(--bg-1);font-family:'Space Grotesk'}}

  .article-nav{{max-width:780px;margin:0 auto;padding:0 0 60px;display:flex;gap:18px}}
  .article-nav a{{flex:1;display:block;background:linear-gradient(180deg,var(--bg-1),var(--bg-2));border:1px solid var(--line);border-radius:16px;padding:22px;transition:border-color .2s,transform .2s;text-decoration:none}}
  .article-nav a:hover{{border-color:rgba(78,168,255,.35);transform:translateY(-1px);text-decoration:none}}
  .article-nav .label{{font-size:11px;color:var(--mute);font-family:'Space Grotesk';letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}}
  .article-nav .title{{font-family:'Space Grotesk';font-size:15px;color:var(--text);line-height:1.3}}

  footer{{border-top:1px solid var(--line);padding:40px 0;color:var(--mute);font-size:13px}}
  footer .frow{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px}}

  @media (max-width:800px){{
    .nav-links{{display:none}}
    .article-header{{padding:60px 0 30px}}
    .article-nav{{flex-direction:column}}
  }}
</style>
</head>
<body>

<nav class="top">
  <div class="container row">
    <a href="https://ambient-advantage.ai" class="logo"><span class="dot"></span>Ambient Advantage</a>
    <div class="nav-links">
      <a href="https://podcast.ambient-advantage.ai">Podcast</a>
      <a href="/" class="active">Daily Briefing</a>
      <a href="https://podcast.ambient-advantage.ai#about">About</a>
    </div>
    <a class="nav-cta" href="/#subscribe">Subscribe</a>
  </div>
</nav>

<header class="article-header">
  <div class="container">
    <div class="inner">
      <a href="/" class="back-link">← Back to all briefings</a>
      <div class="article-meta">
        <span class="tag">Daily Briefing</span>
        <span>{long_date}</span>
        <span>·</span>
        <span>{read_time} min read</span>
      </div>
      <h1>{_escape_html(headline)}</h1>
    </div>
  </div>
</header>

<article class="container">
  <div class="article-body">
    {content}
  </div>
</article>

<div class="container">
  <div class="article-nav">
{nav_items}  </div>
</div>

<footer>
  <div class="container frow">
    <div>© 2026 Ambient Advantage · Curated by Chiel Hendriks</div>
    <div><a href="https://ambient-advantage.ai" style="color:var(--accent);text-decoration:none">ambient-advantage.ai</a></div>
  </div>
</footer>

</body>
</html>'''


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# Update index.html — rotate featured + compact cards
# ---------------------------------------------------------------------------

def update_index_html(
    repo_path: Path,
    new_date: str,
    new_headline: str,
    new_snippet: str,
    new_read_time: int,
) -> None:
    """Rewrite index.html so the new briefing becomes the featured hero card
    and the previous featured moves down to the compact grid (keeping only 3 total)."""

    index_path = repo_path / "index.html"
    html = index_path.read_text()

    day, month = _day_and_month(new_date)
    long_date = _format_date_long(new_date)
    short_date = _format_date_short(new_date)

    # Extract current featured article info to move it to compact grid
    featured_match = re.search(
        r'<article class="featured-briefing">.*?<h3><a href="/([\d-]+)\.html">(.*?)</a></h3>.*?<p class="snippet">(.*?)</p>.*?<span>(.*?)</span>',
        html, re.DOTALL
    )

    # Extract current compact articles
    compact_matches = list(re.finditer(
        r'<article class="nl-compact">.*?<div class="nl-date">(.*?)</div>.*?<h3><a href="/([\d-]+)\.html">(.*?)</a></h3>.*?<p class="snippet">(.*?)</p>',
        html, re.DOTALL
    ))

    # Build the new compact cards: old featured + first old compact (drop the oldest)
    compact_cards = ""
    if featured_match:
        old_feat_date = featured_match.group(1)
        old_feat_headline = featured_match.group(2)
        old_feat_snippet = featured_match.group(3)
        old_short_date = _format_date_short(old_feat_date)
        compact_cards += f'''      <article class="nl-compact">
        <div class="nl-date">{old_short_date}</div>
        <h3><a href="/{old_feat_date}.html">{old_feat_headline}</a></h3>
        <p class="snippet">{old_feat_snippet}</p>
        <a href="/{old_feat_date}.html" class="read-link">Read briefing →</a>
      </article>'''

        if compact_matches:
            first_compact = compact_matches[0]
            compact_cards += f'''

      <article class="nl-compact">
        <div class="nl-date">{first_compact.group(1)}</div>
        <h3><a href="/{first_compact.group(2)}.html">{first_compact.group(3)}</a></h3>
        <p class="snippet">{first_compact.group(4)}</p>
        <a href="/{first_compact.group(2)}.html" class="read-link">Read briefing →</a>
      </article>'''

    # Build new featured section
    new_featured = f'''    <!-- Featured / latest briefing -->
    <article class="featured-briefing">
      <div class="featured-inner">
        <div>
          <span class="featured-tag"><span class="pulse"></span> Latest Briefing</span>
          <h3><a href="/{new_date}.html">{_escape_html(new_headline)}</a></h3>
          <p class="snippet">{_escape_html(new_snippet)}</p>
          <div class="meta-row">
            <span>{long_date}</span>
            <span>·</span>
            <span>{new_read_time} min read</span>
          </div>
          <a href="/{new_date}.html" class="read-btn">Read the briefing →</a>
        </div>
        <div class="featured-date-badge">
          <span class="day">{day}</span>
          <span class="month">{month}</span>
        </div>
      </div>
    </article>

    <!-- Older briefings -->
    <div class="older-briefings">
{compact_cards}
    </div>'''

    # Replace the content between section-head closing and section closing
    new_html = re.sub(
        r'(<!-- Featured / latest briefing -->).*?(</div>\s*</section>)',
        new_featured + '\n  ',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Also update closing </div></section> to be clean
    index_path.write_text(new_html)
    log.info("Updated index.html — featured: %s", new_headline[:60])


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def git_commit_and_push(repo_path: Path, date_key: str, headline: str) -> None:
    """Stage all changes, commit, and push to origin main."""

    def _run(cmd: list[str]):
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("Git command failed: %s\n%s", " ".join(cmd), result.stderr)
            raise RuntimeError(f"git failed: {result.stderr}")
        return result.stdout.strip()

    _run(["git", "add", "-A"])

    # Check if there are actually changes to commit
    status = _run(["git", "status", "--porcelain"])
    if not status:
        log.info("No changes to commit")
        return

    commit_msg = f"Publish briefing {date_key}: {headline[:80]}"
    _run(["git", "commit", "-m", commit_msg])

    if os.environ.get("GIT_PUSH", "").lower() == "true":
        _run(["git", "push", "origin", "main"])
        log.info("Pushed to origin/main")
    else:
        log.info("Dry run — skipping push (set GIT_PUSH=true to push)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1. Determine repo path
    repo_path = Path(os.environ.get(
        "BRIEFING_REPO_PATH",
        os.path.dirname(os.path.abspath(__file__)),
    ))
    log.info("Repo path: %s", repo_path)

    # 2. Fetch the latest sent newsletter from Gmail
    newsletter = fetch_latest_sent_newsletter()
    if not newsletter:
        log.error("No recent newsletter found — nothing to publish")
        sys.exit(1)

    # 3. Check if article already exists
    article_path = repo_path / f"{newsletter.date}.html"
    if article_path.exists():
        log.info("Article %s already exists — skipping", article_path.name)
        sys.exit(0)

    # 4. Extract headline and snippet.
    #    The sent email now uses a branded HTML template, so we derive the
    #    website article content from the plain-text body (raw markdown)
    #    converted to simple HTML.  This keeps the site formatting
    #    unchanged while the email gets the branded design.
    headline, snippet = extract_headline_and_snippet(
        newsletter.html_body, newsletter.subject
    )
    log.info("Headline: %s", headline)
    log.info("Snippet: %s", snippet[:100])

    # Use the plain (markdown) body for the website when available,
    # falling back to html_body for older emails sent before the
    # branded template was introduced.
    if newsletter.plain_body and newsletter.plain_body.strip():
        try:
            import markdown as _md
            _site_html = _md.markdown(
                newsletter.plain_body,
                extensions=["extra", "sane_lists"],
            )
            site_content_html = (
                '<div style="font-family:inherit;line-height:inherit;color:inherit;">'
                f'{_site_html}'
                '</div>'
            )
        except ImportError:
            log.warning("markdown package not installed — falling back to html_body")
            site_content_html = newsletter.html_body
    else:
        site_content_html = newsletter.html_body

    read_time = _estimate_read_time(newsletter.plain_body or newsletter.html_body)

    # 5. Find previous article for nav links
    existing = sorted(repo_path.glob("20??-??-??.html"), reverse=True)
    prev_date = existing[0].stem if existing else None
    prev_headline = None
    if prev_date:
        # Try to extract headline from existing article
        prev_html = existing[0].read_text()
        h1_match = re.search(r'<header.*?<h1>(.*?)</h1>', prev_html, re.DOTALL)
        if h1_match:
            prev_headline = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()

    # 6. Generate article page
    article_html = generate_article_page(
        date_key=newsletter.date,
        headline=headline,
        snippet=snippet,
        newsletter_html=site_content_html,
        prev_date=prev_date,
        prev_headline=prev_headline,
    )
    article_path.write_text(article_html)
    log.info("Created %s", article_path.name)

    # 7. Update index.html
    update_index_html(repo_path, newsletter.date, headline, snippet, read_time)

    # 8. Git commit and push
    git_commit_and_push(repo_path, newsletter.date, headline)

    log.info("Done! Briefing published for %s", newsletter.date)


if __name__ == "__main__":
    main()
