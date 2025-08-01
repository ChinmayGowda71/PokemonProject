import os, json, textwrap, sys
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
import tiktoken
# ------------------------------ CONFIG --------------------------------
MD_PATH      = Path("frlg_scrape/FRLG_part15.md")   #  <—  set the file here
MODEL        = "gpt-4o-mini"                        #  or "gpt-3.5-turbo-0125"
MAX_CHARS    = 15000                            #  ~30 k tokens; adjust if needed
OUTPUT_FILE  = MD_PATH.with_suffix(".json")         # saved alongside .md
# ----------------------------------------------------------------------

PROMPT = textwrap.dedent("""

You are a parser. You are given cleaned Markdown of a Bulbapedia page and you need to extract the data from the page.
You are a parser. Return ONLY valid JSON matching this schema:

{
 "sections": [
   {
     "heading": "<string>",          // e.g. "Viridian City"  (## in Markdown)
     "text":    "<plain text>",      // paragraphs until the first sub-heading
     "tables":      [ { "rows": [ ... ] } ],
     "tableFacts":  [ "<sentence>", ... ],
     "rivalFacts":  [ { ... } ],
     "images":      [ { "src": "...", "alt": "..." } ],

     "subsections": [
       {
         "heading": "<string>",      // e.g. "Pokémon Center"   (### or bold or ####)
         "text":    "<plain text>",
         "tables":      [ { "rows": [ ... ] } ],
         "tableFacts":  [ "<sentence>", ... ],
         "rivalFacts":  [ { ... } ],
         "images":      [ { "src": "...", "alt": "..." } ],

         /* OPTIONAL nested sub-subsections produced from ####, bullet labels, etc. */
         "subsections": [ /* …same object shape… */ ]
       },
       {
         "heading": "Poké Mart",
         "text": "List of items you can buy …",
         "tables":      [ { "rows": [ ["Item","Price"],["Potion","300"] ] } ],
         "tableFacts":  [ "Potion costs ₽300." ],
         "rivalFacts":  [],
         "images":      [],
         "subsections": []
       }
     ]
   }
 ]
}

For every data table:
  • Put the raw rows in "tables".
  • Make sure to record the pokemon dollar amount as well and use it in the sentence descriptions.
  • ALSO write one natural-language sentence per row and place
    those sentences in "tableFacts". 
  Example row  →  Sentence
["Bulbasaur","Grass","5-7","25 %"]
→ "Bulbasaur can be encountered in the grass at levels 5-7 with a 25 % encounter rate."
Write a sentence for rival facts as well highlighting every detail.


Heading Formatting Guidelines:
  • Treat headings starting with ### and #### as subsections of the header starting with ##.
  • If there are multiple tables right after each other, make sure to put them in the same subsection.
* Do NOT lose evolution level numbers or move names.
* Temperature 0. Return nothing but JSON.
""").strip()

# ----------------------------- OPENAI ---------------------------------
load_dotenv()
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def gpt_parse(md_text:str)->dict:
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format={"type":"json_object"},
        messages=[
            {"role":"system","content":PROMPT},
            {"role":"user","content": md_text[:MAX_CHARS]}
        ]
    )
    return json.loads(resp.choices[0].message.content)

# ---------------------- (optional) chunk helper -----------------------
def parse_with_chunks(md_text:str)->dict:
    parts = [md_text[i:i+MAX_CHARS] for i in range(0,len(md_text),MAX_CHARS)]
    merged = {"sections":[]}
    for i,part in enumerate(parts,1):
        print(f"⚙️  chunk {i}/{len(parts)}")
        out = gpt_parse(part)
        merged["sections"].extend(out["sections"])
    return merged
# ----------------------------------------------------------------------

if not MD_PATH.exists():
    sys.exit(f"Markdown file not found: {MD_PATH}")

md = MD_PATH.read_text("utf-8")
print("Chars in MD:", len(md))

# ---- choose single call or chunked ----
result = gpt_parse(md) if len(md) <= MAX_CHARS else parse_with_chunks(md)

OUTPUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), "utf-8")
print("✅ Saved JSON →", OUTPUT_FILE)
