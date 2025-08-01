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
print(API_KEY)
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
    resp.raise_for_status()          # raises on 4xx/5xx
    return resp.json()["data"]

out_dir = Path("frlg_scrape")
out_dir.mkdir(exist_ok=True)

for url in get_part_urls("Pokémon FireRed and LeafGreen"):
    part_no = re.search(r"Part_(\d+)", url).group(1)
    print(f"➡️  Scraping Part {part_no} … ", end="", flush=True)

    try:
        data = firecrawl_scrape(url)
    except Exception as e:
        print("FAILED", e)
        continue

    (out_dir / f"FRLG_part{part_no}.html").write_text(data["html"], encoding="utf-8")
    (out_dir / f"FRLG_part{part_no}.md"  ).write_text(data["markdown"], encoding="utf-8")

    print(f"OK  (html {len(data['html'])//1000} kB, md {len(data['markdown'])//1000} kB)")
    time.sleep(10)            # be polite to Firecrawl’s free tier

print("✅  All parts downloaded to", out_dir)