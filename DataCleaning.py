"""
firecrawl_single_test.py
↳ One-page Firecrawl test with /v1/scrape
"""

import os, json, requests
from pathlib import Path
from dotenv import load_dotenv
import requests
from urllib.parse import quote
from bs4 import BeautifulSoup
import re, json, time
BASE_WIKI = "https://bulbapedia.bulbagarden.net"
PREFIX = "Walkthrough:"
HEADERS = {"User-Agent": "Mozilla/5.0"}

def get_part_urls(GAME_NAME):
    # Step 1: Get the main page and parse all links to /Part_*
  ENCODED_TITLE_BASE = f"{PREFIX}{quote(GAME_NAME.replace(' ', '_'))}"
  CATEGORY_PAGE = f"{BASE_WIKI}/wiki/{ENCODED_TITLE_BASE}"

  response = requests.get(CATEGORY_PAGE)
  soup = BeautifulSoup(response.text, "html.parser")
  links = soup.find_all("a", href=True)
  part_urls = []

  for a in links:
      href = a['href']
      if href.startswith(f"/wiki/{ENCODED_TITLE_BASE}/Part_"):
          full_url = BASE_WIKI + href
          part_urls.append(full_url)

  # Deduplicate and sort by Part number
  part_urls = sorted(set(part_urls), key=lambda x: int(re.search(r"Part_(\d+)", x).group(1)))
  return part_urls



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

def firecrawl_scrape(url: str) -> dict: 
    endpoint = "https://api.firecrawl.dev/v1/scrape"
    payload  = {
    "url": url,
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
    timeout=120
)
TEST_URL = "https://bulbapedia.bulbagarden.net/wiki/Walkthrough:Pok%C3%A9mon_X_and_Y/Part_6"   # any page

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
