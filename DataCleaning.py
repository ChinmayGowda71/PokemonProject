"""
firecrawl_single_test.py
↳ One-page Firecrawl test with /v1/scrape
"""

import os, json, requests
from pathlib import Path
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 1.  Load API key from .env   (add FIRECRAWL_API_KEY=fc-xxxx there)
# ------------------------------------------------------------------
load_dotenv()
API_KEY = os.getenv("FIRECRAWL_API_KEY")
if not API_KEY:
    raise RuntimeError("Set FIRECRAWL_API_KEY in .env or environment")

# ------------------------------------------------------------------
# 2.  Choose the page to test
# ------------------------------------------------------------------
TEST_URL = "https://bulbapedia.bulbagarden.net/wiki/Walkthrough:Pok%C3%A9mon_FireRed_and_LeafGreen/Part_2"   # any page

# ------------------------------------------------------------------
# 3. Firecrawl /v1/scrape request
# ------------------------------------------------------------------
endpoint = "https://api.firecrawl.dev/v1/scrape"
payload  = {
    "url": TEST_URL,
    "formats": ["html", "markdown"],   # get both
    "onlyMainContent": True            # strip nav/ads
}

resp = requests.post(
    endpoint,
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type":  "application/json"
    },
    json=payload,
    timeout=60
)
resp.raise_for_status()          # raises on 4xx/5xx
data = resp.json()["data"]

# ------------------------------------------------------------------
# 4.  Inspect & save
# ------------------------------------------------------------------
print("✅ Firecrawl success")
print("  HTML chars   :", len(data["html"]))
print("  Markdown chars:", len(data["markdown"]))
print("  Title        :", data["metadata"]["title"])

Path("firecrawl_test_html.html").write_text(data["html"], encoding="utf-8")
Path("firecrawl_test_md.md").write_text(data["markdown"], encoding="utf-8")
print("Saved files: firecrawl_test_html.html & firecrawl_test_md.md")
