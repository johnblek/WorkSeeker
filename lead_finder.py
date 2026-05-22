import requests
from bs4 import BeautifulSoup
from google import genai
import json
import time
import os
import re
import sqlite3
import hashlib
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================
GEMINI_KEY = os.environ["GEMINI_API_KEY"]

TAGS = [
    "horror", "atmospheric", "dark-fantasy", "souls-like",
    "rpg", "action-rpg", "gacha", "cinematic",
    "sci-fi", "ambient", "fantasy", "narrative"
]

PAGES_PER_TAG = 1

BLOCKED_AUTHORS = [
    # "somedevname",
]


# ============================================================
# DATABASE
# ============================================================
def init_db():
    conn = sqlite3.connect("leads.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_found TEXT,
            title TEXT,
            author TEXT,
            author_profile_url TEXT,
            url TEXT UNIQUE,
            source TEXT,
            description TEXT,
            explicitly_hiring INTEGER,
            hiring_quote TEXT,
            needs_sound_design INTEGER,
            needs_music INTEGER,
            confidence TEXT,
            pitch_sfx TEXT,
            pitch_music TEXT,
            genre_match TEXT,
            priority INTEGER,
            reason TEXT,
            contact TEXT
        )
    """)
    conn.commit()
    return conn


def already_seen(conn, url):
    c = conn.cursor()
    c.execute("SELECT 1 FROM leads WHERE url = ?", (url,))
    return c.fetchone() is not None


def save_lead(conn, lead, date_str):
    try:
        conn.execute("""
            INSERT INTO leads
            (date_found, title, author, author_profile_url, url, source, description,
             explicitly_hiring, hiring_quote,
             needs_sound_design, needs_music, confidence,
             pitch_sfx, pitch_music, genre_match, priority, reason, contact)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            lead.get("title", ""),
            lead.get("author", ""),
            lead.get("author_profile_url", ""),
            lead.get("url", ""),
            lead.get("source", ""),
            lead.get("description", ""),
            1 if lead.get("explicitly_hiring") else 0,
            lead.get("hiring_quote", ""),
            1 if lead.get("needs_sound_design") else 0,
            1 if lead.get("needs_music") else 0,
            lead.get("confidence", ""),
            lead.get("pitch_sfx", ""),
            lead.get("pitch_music", ""),
            lead.get("genre_match", ""),
            lead.get("priority", 0),
            lead.get("reason", ""),
            lead.get("contact", "")
        ))
        conn.commit()
    except sqlite3.IntegrityError:
        pass


# ============================================================
# SCRAPER
# ============================================================
def scrape_itch_tag(tag, pages=1):
    leads = []
    for page in range(1, pages + 1):
        url = f"https://itch.io/games/newest/tag-{tag}?page={page}"
        try:
            resp = requests.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for cell in soup.select(".game_cell"):
                title_el = cell.select_one(".title")
                link_el = cell.select_one("a.title")
                desc_el = cell.select_one(".game_text")
                author_el = cell.select_one(".game_author a")
                author_name = author_el.text.strip() if author_el else ""
                author_href = author_el.get("href", "") if author_el else ""
                if author_href and author_href.startswith("/"):
                    author_profile = f"https://itch.io{author_href}"
                elif author_href:
                    author_profile = author_href
                else:
                    author_profile = ""
                leads.append({
                    "title": title_el.text.strip() if title_el else "",
                    "url": link_el["href"] if link_el else "",
                    "description": desc_el.text.strip() if desc_el else "",
                    "author": author_name,
                    "author_profile_url": author_profile,
                    "source": f"itch.io/tag-{tag}"
                })
        except Exception as e:
            print(f"Error scraping tag '{tag}' page {page}: {e}")
    return leads


def collect_all_leads(conn):
    all_leads = []
    for tag in TAGS:
        print(f"Scraping: {tag}")
        all_leads.extend(scrape_itch_tag(tag, PAGES_PER_TAG))
        time.sleep(2)

    blocked_lower = [b.lower() for b in BLOCKED_AUTHORS]
    seen = set()
    unique = []
    skipped_seen = 0
    skipped_blocked = 0

    for lead in all_leads:
        if not lead["url"] or lead["url"] in seen:
            continue
        if lead["author"].lower() in blocked_lower:
            skipped_blocked += 1
            continue
        if already_seen(conn, lead["url"]):
            skipped_seen += 1
            continue
        seen.add(lead["url"])
        unique.append(lead)

    print(f"Found {len(unique)} new unique leads.")
    print(f"Skipped {skipped_seen} already-seen games.")
    print(f"Skipped {skipped_blocked} blocked authors.")
    return unique


# ============================================================
# CONTACT FINDER
# ============================================================
def scrape_contact_from_html(html_text, soup):
    emails = re.findall(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        html_text
    )
    emails = [
        e for e in emails
        if not any(skip in e.lower() for skip in [
            "sentry", "example", "noreply", "no-reply",
            "itch.io", "support@", "help@", "abuse@"
        ])
    ]
    socials = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if any(s in href for s in [
            "twitter.com", "x.com", "discord.gg", "discord.com",
            "mastodon", "bsky.app", "linkedin.com",
            "instagram.com", "youtube.com", "linktr.ee", "carrd.co"
        ]):
            socials.append(href)
    return list(set(emails)), list(set(socials))


def find_contact_info(game_url, author_profile_url):
    all_emails = []
    all_socials = []
    external_links = []

    try:
        resp = requests.get(game_url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        emails, socials = scrape_contact_from_html(resp.text, soup)
        all_emails.extend(emails)
        all_socials.extend(socials)
        time.sleep(1)
    except Exception:
        pass

    if author_profile_url:
        try:
            resp = requests.get(author_profile_url, timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            emails, socials = scrape_contact_from_html(resp.text, soup)
            all_emails.extend(emails)
            all_socials.extend(socials)
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if href.startswith("http") and "itch.io" not in href and href not in all_socials:
                    external_links.append(href)
            time.sleep(1)
        except Exception:
            pass

    if not all_emails and external_links:
        try:
            resp = requests.get(external_links[0], timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            emails, socials = scrape_contact_from_html(resp.text, soup)
            all_emails.extend(emails)
            all_socials.extend(socials)
            time.sleep(1)
        except Exception:
            pass

    parts = []
    if all_emails:
        unique_emails = list(dict.fromkeys(all_emails))[:2]
        parts.append("Email: " + ", ".join(unique_emails))
    if all_socials:
        unique_socials = list(dict.fromkeys(all_socials))[:4]
        parts.append("Links: " + ", ".join(unique_socials))
    return " | ".join(parts) if parts else "Check page"


# ============================================================
# HIRING CHECK
# ============================================================
def check_if_hiring(game_url):
    try:
        resp = requests.get(game_url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        text_blocks = []
        for el in soup.select(".game_description, .devlog_post, p, li, .body"):
            text_blocks.append(el.get_text(separator=" "))
        full_text = " ".join(text_blocks).lower()

        audio_keywords = ["sound", "music", "audio", "sfx", "composer", "soundtrack", "musician"]
        hiring_triggers = ["looking for", "seeking", "need a", "need an", "hiring", "open to", "want a", "collaborat"]

        for trigger in hiring_triggers:
            idx = full_text.find(trigger)
            while idx != -1:
                window = full_text[max(0, idx - 80): idx + 80]
                if any(ak in window for ak in audio_keywords):
                    original_text = " ".join(text_blocks)
                    start = max(0, original_text.lower().find(trigger) - 40)
                    snippet = original_text[start: start + 160].strip()
                    snippet = " ".join(snippet.split())
                    return True, f'"{snippet}..."'
                idx = full_text.find(trigger, idx + 1)
        return False, ""
    except Exception:
        return False, ""


# ============================================================
# GEMINI ANALYSIS
# ============================================================
def analyze_lead(client, lead):
    prompt = f"""You are helping a freelance sound designer and composer find work.

His specialties:
- ATMOSPHERIC SOUND DESIGN: Deep, immersive, environmental audio. FromSoftware-style
  (Elden Ring, Bloodborne, Dark Souls) — brooding textures, haunting ambience,
  tension-building soundscapes.
- PUNCHY SOUND DESIGN: High-impact, snappy, satisfying SFX. Hits that feel
  physical. UI clicks that feel premium. Weapons that feel heavy and deliberate.
- HEAVY MUSIC: Metalcore, doom metal, argent metal (DOOM 2016/Eternal style),
  and Thall (Meshuggah-influenced djent).
- MIXING & MASTERING: Heavy and snappy. Punchy low-end, tight transients.
- GACHA GAME AUDIO: Polished UI SFX, summoning sequences, character reveal stings,
  cinematic character themes.

Analyze this game. Be honest — low priority if it's a bad fit.

Game Title: {lead['title']}
Description: {lead['description']}
Developer: {lead['author']}
Source: {lead['source']}
URL: {lead['url']}
Explicitly Hiring for Audio: {lead.get('explicitly_hiring', False)}
Hiring Context: {lead.get('hiring_quote', 'N/A')}

Return ONLY valid JSON, no markdown, no code fences:
{{
    "needs_sound_design": true or false,
    "needs_music": true or false,
    "confidence": "high" or "medium" or "low",
    "pitch_sfx": "One sentence why his style fits their SFX needs. Write N/A if false.",
    "pitch_music": "One sentence why his heavy/doom style fits their music needs. Write N/A if false.",
    "genre_match": "high" or "medium" or "low",
    "priority": 1 to 10,
    "reason": "One sentence why this is or isn't a good lead."
}}"""

    max_retries = 4
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt
            )
            raw = response.text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except Exception as e:
            error_str = str(e)
            if "503" in error_str:
                wait = 15 * (attempt + 1)
                print(f"  503 overloaded. Waiting {wait}s (retry {attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue
            elif "429" in error_str:
                match = re.search(r'retry in (\d+\.?\d*)s', error_str)
                wait = float(match.group(1)) + 5 if match else 65
                print(f"  429 rate limit. Waiting {wait:.0f}s...")
                time.sleep(wait)
                continue
            else:
                print(f"  Gemini error on '{lead['title']}': {e}")
                return None
    print(f"  Gave up on '{lead['title']}' after {max_retries} retries.")
    return None


# ============================================================
# MARKDOWN REPORT
# ============================================================
def format_lead_block(lead):
    sfx = "✅" if lead.get("needs_sound_design") else "❌"
    mus = "✅" if lead.get("needs_music") else "❌"
    hiring_badge = " 🚨 **HIRING**" if lead.get("explicitly_hiring") else ""
    lines = [
        f"### {lead.get('title', 'Untitled')} — by {lead.get('author', '?')}{hiring_badge}",
        f"",
        f"- **Priority:** {lead.get('priority', '?')}/10  |  **Genre Match:** {lead.get('genre_match', '?')}  |  **Confidence:** {lead.get('confidence', '?')}",
        f"- **Why:** {lead.get('reason', '')}",
        f"- **Needs SFX:** {sfx} {lead.get('pitch_sfx', 'N/A')}",
        f"- **Needs Music:** {mus} {lead.get('pitch_music', 'N/A')}",
    ]
    if lead.get("explicitly_hiring") and lead.get("hiring_quote"):
        lines.append(f"- **They Said:** {lead.get('hiring_quote', '')}")
    lines += [
        f"- **Contact:** {lead.get('contact', 'Check page')}",
        f"- **Game:** [{lead.get('url', '')}]({lead.get('url', '')})",
    ]
    if lead.get("author_profile_url"):
        lines.append(f"- **Dev Profile:** [{lead.get('author_profile_url', '')}]({lead.get('author_profile_url', '')})")
    lines += [f"- **Source:** {lead.get('source', '')}", f"", f"---", f""]
    return lines


def generate_report(leads, date_str):
    hiring = [l for l in leads if l.get("explicitly_hiring") and l.get("priority", 0) >= 4]
    hot = [l for l in leads if not l.get("explicitly_hiring") and l.get("priority", 0) >= 7]
    warm = [l for l in leads if not l.get("explicitly_hiring") and 4 <= l.get("priority", 0) < 7]
    cold = [l for l in leads if l.get("priority", 0) < 4]

    lines = [
        f"# Lead Report — {date_str}",
        f"",
        f"**{len(leads)} leads analyzed this run.**",
        f"",
    ]

    if hiring:
        lines.append(f"## 🚨 Explicitly Hiring for Audio ({len(hiring)})")
        lines.append(f"> These devs have directly stated they need a sound designer or musician.")
        lines.append(f"")
        for lead in hiring:
            lines.extend(format_lead_block(lead))

    for tier_name, tier_leads in [
        ("🔥 Hot Leads", hot),
        ("🟡 Warm Leads", warm),
        ("🔵 Cold Leads", cold)
    ]:
        if not tier_leads:
            continue
        lines.append(f"## {tier_name} ({len(tier_leads)})")
        lines.append("")
        for lead in tier_leads:
            lines.extend(format_lead_block(lead))

    os.makedirs("reports", exist_ok=True)
    report_path = f"reports/{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Markdown report written: {report_path}")
    return report_path


# ============================================================
# JSON REPORT (for web GUI)
# ============================================================
def generate_json_report(leads, date_str):
    """Write a JSON version of the report for the web GUI."""
    output = {
        "date": date_str,
        "total": len(leads),
        "counts": {
            "hiring": len([l for l in leads if l.get("explicitly_hiring")]),
            "hot":    len([l for l in leads if not l.get("explicitly_hiring") and l.get("priority", 0) >= 7]),
            "warm":   len([l for l in leads if not l.get("explicitly_hiring") and 4 <= l.get("priority", 0) < 7]),
            "cold":   len([l for l in leads if l.get("priority", 0) < 4]),
        },
        "leads": []
    }

    for lead in leads:
        # Stable short ID from the URL
        lead_id = hashlib.md5(lead.get("url", "").encode()).hexdigest()[:12]
        output["leads"].append({
            "id":                 lead_id,
            "title":              lead.get("title", ""),
            "author":             lead.get("author", ""),
            "author_profile_url": lead.get("author_profile_url", ""),
            "url":                lead.get("url", ""),
            "source":             lead.get("source", ""),
            "description":        lead.get("description", ""),
            "explicitly_hiring":  bool(lead.get("explicitly_hiring")),
            "hiring_quote":       lead.get("hiring_quote", ""),
            "needs_sound_design": bool(lead.get("needs_sound_design")),
            "needs_music":        bool(lead.get("needs_music")),
            "confidence":         lead.get("confidence", ""),
            "pitch_sfx":          lead.get("pitch_sfx", ""),
            "pitch_music":        lead.get("pitch_music", ""),
            "genre_match":        lead.get("genre_match", ""),
            "priority":           int(lead.get("priority", 0)),
            "reason":             lead.get("reason", ""),
            "contact":            lead.get("contact", "")
        })

    os.makedirs("reports", exist_ok=True)
    json_path = f"reports/{date_str}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"JSON report written: {json_path}")


# ============================================================
# MANIFEST (tells the GUI which weeks exist)
# ============================================================
def update_manifest(date_str):
    """Keep a sorted list of all report dates for the web GUI."""
    manifest_path = "reports_manifest.json"

    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            dates = json.load(f)
    else:
        dates = []

    if date_str not in dates:
        dates.append(date_str)

    dates.sort(reverse=True)  # Newest first

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(dates, f, indent=2)
    print(f"Manifest updated: {dates}")


# ============================================================
# MAIN
# ============================================================
def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("=" * 50)
    print(f"LEAD FINDER — {today}")
    print("=" * 50)

    conn = init_db()
    leads = collect_all_leads(conn)
    leads = leads[:10]

    if not leads:
        print("No new leads to analyze this run.")
        os.makedirs("reports", exist_ok=True)
        empty = {"date": today, "total": 0, "counts": {}, "leads": []}
        with open(f"reports/{today}.json", "w") as f:
            json.dump(empty, f)
        with open(f"reports/{today}.md", "w") as f:
            f.write(f"# Lead Report — {today}\n\nNo new leads found this run.\n")
        update_manifest(today)
        conn.close()
        return

    client = genai.Client(api_key=GEMINI_KEY)
    analyzed = []

    for i, lead in enumerate(leads):
        print(f"[{i+1}/{len(leads)}] {lead['title']}")

        print(f"  Checking for hiring signals...")
        is_hiring, hiring_quote = check_if_hiring(lead["url"])
        lead["explicitly_hiring"] = is_hiring
        lead["hiring_quote"] = hiring_quote
        if is_hiring:
            print(f"  ⚡ HIRING SIGNAL FOUND")
        time.sleep(1)

        result = analyze_lead(client, lead)

        if result:
            if result.get("priority", 0) >= 4 or is_hiring:
                print(f"  -> Priority {result['priority']}. Hunting for contact info...")
                lead["contact"] = find_contact_info(lead["url"], lead.get("author_profile_url", ""))
            else:
                lead["contact"] = "Low priority — skipped"

            lead.update(result)
            analyzed.append(lead)
            save_lead(conn, lead, today)

        time.sleep(7)

    analyzed.sort(key=lambda x: (
        -(1 if x.get("explicitly_hiring") else 0),
        -x.get("priority", 0)
    ))

    generate_report(analyzed, today)
    generate_json_report(analyzed, today)  # ← NEW
    update_manifest(today)                 # ← NEW

    conn.close()
    print(f"Done. {len(analyzed)} leads analyzed and saved.")


if __name__ == "__main__":
    main()
