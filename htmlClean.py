import os, json, textwrap
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# ------------------------------------------------------------------
# 1.  Load API key & client
# ------------------------------------------------------------------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in .env or environment")
client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------------------------------------------------
# 2.  Read Firecrawl Markdown (produced by previous script)
# ------------------------------------------------------------------
markdown_path = Path("firecrawl_test_md.md")
markdown_text = markdown_path.read_text(encoding="utf-8")

EXTRACT_PROMPT =  textwrap.dedent("""

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

     /* NEW  ↓  recursive list of subsections */
     "subsections": [
       {
         "heading": "<string>",      // e.g. "Pokémon Center"   (### or bold)
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
  • ALSO write one natural-language sentence per row and place
    those sentences in "tableFacts". 
  Example row  →  Sentence
["Bulbasaur","Grass","5-7","25 %"]
→ "Bulbasaur can be encountered in the grass at levels 5-7 with a 25 % encounter rate."
Write a sentence for rival facts as well highlighting every detail.


Heading Formatting Guidelines:
  • Treat headings starting with ### and #### as subsections of the header starting with ##.

* Do NOT lose evolution level numbers or move names.
* Temperature 0. Return nothing but JSON.
""").strip()

response = client.chat.completions.create(
    model="gpt-4o-mini",        # or "gpt-4o" / "gpt-3.5-turbo-0125"
    temperature=0,
    response_format={ "type": "json_object" },
    messages=[
        { "role": "system", "content": EXTRACT_PROMPT },
        { "role": "user",   "content": markdown_text } 
    ]
)

json_output = json.loads(response.choices[0].message.content)
out_path = Path("route1_rag_chunks_2.json")
out_path.write_text(json.dumps(json_output, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"✅ Saved {len(json_output['sections'])} sections → {out_path}")




