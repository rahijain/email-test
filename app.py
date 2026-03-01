import os
import base64
import traceback
from typing import Optional
from html.parser import HTMLParser
import ollama
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB max upload

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2-vision")


# ── HTML audit parser ────────────────────────────────────────────────────────

class EmailAuditParser(HTMLParser):
    """Extract all <a> links and <img> tags from an HTML email."""

    def __init__(self):
        super().__init__()
        self.links = []
        self.images = []
        self._in_a = False
        self._current_href = ""
        self._current_title = ""
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self._in_a = True
            self._current_href = a.get("href", "")
            self._current_title = a.get("title", "")
            self._current_text = []
        elif tag == "img":
            alt_val = a.get("alt", None)
            self.images.append({
                "src": a.get("src", ""),
                "alt": alt_val,
                "title": a.get("title", ""),
            })

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            self.links.append({
                "href": self._current_href,
                "text": " ".join(self._current_text).strip(),
                "title": self._current_title,
            })
            self._in_a = False
            self._current_href = ""
            self._current_title = ""
            self._current_text = []

    def handle_data(self, data):
        if self._in_a:
            stripped = data.strip()
            if stripped:
                self._current_text.append(stripped)


def extract_audit_data(html_content: str) -> dict:
    """Parse HTML and return link + image audit data."""
    parser = EmailAuditParser()
    parser.feed(html_content)

    href_counts: dict = {}
    for lnk in parser.links:
        href_counts[lnk["href"]] = href_counts.get(lnk["href"], 0) + 1

    seen: set = set()
    processed_links = []
    for lnk in parser.links:
        href = lnk["href"]
        duplicate = href in seen and bool(href) and href != "#"
        seen.add(href)
        processed_links.append({
            "href": href,
            "text": lnk["text"] or lnk["title"] or "(no text)",
            "occurrences": href_counts[href],
            "duplicate": duplicate,
            "empty": not href or href.strip() == "#",
        })

    processed_images = []
    for img in parser.images:
        alt = img["alt"]
        processed_images.append({
            "src": img["src"],
            "alt": alt if alt is not None else "",
            "alt_missing": alt is None,
            "alt_empty": alt == "",
            "title": img["title"],
        })

    missing_alt = sum(1 for i in processed_images if i["alt_missing"] or i["alt_empty"])

    return {
        "links": processed_links,
        "images": processed_images,
        "link_count": len(processed_links),
        "unique_link_count": len(set(l["href"] for l in processed_links)),
        "image_count": len(processed_images),
        "missing_alt_count": missing_alt,
    }


def detect_media_type(content_type: str, filename: str) -> str:
    """Detect image MIME type from content-type header or filename extension."""
    ct = (content_type or "").lower()
    fn = (filename or "").lower()
    if "jpeg" in ct or "jpg" in ct or fn.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if "gif" in ct or fn.endswith(".gif"):
        return "image/gif"
    if "webp" in ct or fn.endswith(".webp"):
        return "image/webp"
    return "image/png"


def render_html_to_screenshot(html_content: str) -> Optional[bytes]:
    """Render HTML to a screenshot using Playwright. Returns None if unavailable."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 640, "height": 900})
            page.set_content(html_content, wait_until="networkidle", timeout=30_000)
            screenshot = page.screenshot(full_page=True)
            browser.close()
            print("[INFO] HTML rendered to screenshot successfully.")
            return screenshot
    except Exception as e:
        print(f"[INFO] Playwright not available or failed ({e}), falling back to HTML code.")
        return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/compare", methods=["POST"])
def compare():
    try:
        html_file = request.files.get("html")
        design_file = request.files.get("design")

        if not html_file or not design_file:
            return jsonify({"error": "Both an HTML file and a design image are required"}), 400

        html_content = html_file.read().decode("utf-8")
        design_bytes = design_file.read()

        if not design_bytes:
            return jsonify({"error": "Design image file is empty"}), 400

        if len(design_bytes) > 5 * 1024 * 1024:
            return jsonify({"error": "Design image is too large. Please use an image under 5 MB."}), 400

        print(f"[INFO] HTML: {len(html_content)} chars | Image: {len(design_bytes)/1024:.1f} KB")

        audit = extract_audit_data(html_content)
        print(f"[INFO] Audit: {audit['link_count']} links, {audit['image_count']} images, {audit['missing_alt_count']} missing alt")

        screenshot = render_html_to_screenshot(html_content)
        rendered = screenshot is not None

        # Build images list and prompt for Ollama
        images = [design_bytes]
        prompt_lines = [
            "I need you to compare an email design mockup against its HTML implementation "
            "and tell me whether they match.\n\n## Design Mockup:\n(First image provided)\n"
        ]

        if rendered:
            images.append(screenshot)
            prompt_lines.append("## Rendered HTML Email:\n(Second image provided)\n")
        else:
            excerpt = html_content[:15000] + ("...[truncated]" if len(html_content) > 15000 else "")
            prompt_lines.append(f"\n## HTML Code:\n```html\n{excerpt}\n```")

        prompt_lines.append(
            """Analyze whether the HTML email matches the design mockup and produce a structured report:

## Overall Match Score
Rate the match out of 10 and give a one-line verdict (e.g. "8/10 – Matches well with minor spacing issues").

## What Matches ✅
List elements that correctly match the design (layout, colours, images, CTAs, footer, etc.)

## Issues Found ❌
List every discrepancy. For each one include:
- **Element**: what is affected
- **Design**: what the design shows
- **HTML**: what the HTML/render actually shows
- **Severity**: High / Medium / Low

## Priority Fixes
Rank the top issues to fix first, numbered 1–N.

## Verdict
Does this email match the design? Choose one: **Yes** / **Mostly** / **Partially** / **No**

Be specific and actionable."""
        )

        print(f"[INFO] Calling Ollama ({OLLAMA_MODEL})...")

        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{
                "role": "user",
                "content": "\n".join(prompt_lines),
                "images": images,
            }],
        )

        analysis = response["message"]["content"]

        print("[INFO] Analysis complete.")
        return jsonify({"analysis": analysis, "rendered": rendered, "audit": audit})

    except ollama.ResponseError as e:
        msg = f"Ollama error: {e.error}"
        if "model" in e.error.lower() and "not found" in e.error.lower():
            msg = (
                f"Model '{OLLAMA_MODEL}' not found. "
                f"Run: ollama pull {OLLAMA_MODEL}"
            )
        print(f"[ERROR] {msg}")
        return jsonify({"error": msg}), 502

    except Exception as e:
        err_str = str(e)
        # Connection refused = Ollama not running
        if "connection refused" in err_str.lower() or "connection error" in err_str.lower():
            msg = "Cannot connect to Ollama. Make sure it's running: ollama serve"
            print(f"[ERROR] {msg}")
            return jsonify({"error": msg}), 503
        print(f"[ERROR] Unexpected error:\n{traceback.format_exc()}")
        return jsonify({"error": f"Server error: {err_str}"}), 500


if __name__ == "__main__":
    print(f"[INFO] Using Ollama model: {OLLAMA_MODEL}")
    print(f"[INFO] Starting server on http://localhost:5001")
    app.run(debug=True, port=5001, threaded=True)
