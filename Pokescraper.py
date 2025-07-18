import requests
from urllib.parse import quote
from bs4 import BeautifulSoup
import mwparserfromhell
import re, json, time
from langchain_text_splitters import RecursiveCharacterTextSplitter
BASE_WIKI = "https://bulbapedia.bulbagarden.net"
PREFIX = "Walkthrough:"
SPLITTER  = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
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

def raw_wiki_page(part_url):
  raw_url = f"{BASE_WIKI}/w/index.php?title={part_url}&action=raw"
  response = requests.get(raw_url)
  return response.text

def soup_wiki_page(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def parse_text_image(url, game_name):
  title = url.split("/wiki/")[1]
  raw_url = raw_wiki_page(title)
  soup = soup_wiki_page(url)
  #raw text from wiki page
  parser = mwparserfromhell.parse(raw_url)
  sections = parser.get_sections(include_lead=True, levels=[2])
  narrative_chunks = []
  for section in sections:
      heading = section.filter_headings()
      heading = heading[0].title.strip() if heading else "Introduction"
      text = section.strip_code().strip()
      for doc in SPLITTER.create_documents([text]):
            narrative_chunks.append({
                "type": "text",
                "section": heading,
                "content": doc.page_content
            })

  tables = []
  for tbl in soup.find_all("table", class_="roundy"):
      rows = []
      for tr in tbl.find_all("tr"):
          cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th","td"])]
          if cells: rows.append(cells)
      if rows:
          tables.append({
              "type": "table",
              "html_section": tbl.get("title",""),
              "rows": rows
          })

  images = []
  for img in soup.find_all("img"):
      src  = img.get("src","")
      if not src or src.startswith("data:"): continue
      images.append({
          "type":  "image",
          "src":  "https:" + src,
          "alt":   img.get("alt",""),
          "title": img.get("title","")
      })

  return {
      "source_url": url,
      "game":  game_name,
      "text_chunks": narrative_chunks,
      "tables": tables,
      "images": images
  }

def crawl_walkthrough(game_title:str):
    part_urls = get_part_urls(game_title)
    print(f"🔎 Found {len(part_urls)} parts for {game_title}")

    for url in part_urls:
        part_no  = re.search(r"Part_(\d+)", url).group(1)
        print(f"  • Parsing Part {part_no} …")
        part_doc = parse_text_image(url, game_title)
        out_file = f"{game_title.replace(' ','_')}_part{part_no}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(part_doc, f, indent=2, ensure_ascii=False)
        time.sleep(1)   # be polite

    print("✅  Done. JSON files ready for RAG ingestion.")


crawl_walkthrough("Pokémon FireRed and LeafGreen")
