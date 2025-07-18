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
markdown_path = Path("Pokemon/firecrawl_test_md.md")
markdown_text = markdown_path.read_text(encoding="utf-8")

EXTRACT_PROMPT =  textwrap.dedent("""

You are a parser. You are given cleaned Markdown of a Bulbapedia page and you need to extract the data from the page.
You are a parser. Return ONLY valid JSON matching this schema:

{
 "sections": [
   {
     "heading": "<string>",             // e.g. "Pallet Town"
     "text":    "<plain text>",         // main paragraph(s)
     "tables": [
       { "rows": [ ["Pokémon","Location","Levels","Rate"],
                   ["Bulbasaur","Grass","5-7","25 %"],
                   ["Pidgey","Grass","3-5","55 %"] ] }
     ],
     "rivalFacts": [                    // zero or more per section
       {
         "starter": "<Bulbasaur|Charmander|Squirtle|...>",
         "rival":   "<name>",
         "location":"<where the battle happens>",
         "reward":  "<integer or '—'>",
         "team":    ["Pokemon (Lv X)", ...]
       }
     ],
      "tableFacts": ["sentence1", "sentence2"],
     "images": [
       { "src":"<https url>", "alt":"<alt text>" }
     ]
   }
 ]
}

For every data table:
  • Put the raw rows in "tables".
  • ALSO write one natural-language sentence per row and place
    those sentences in "tableFacts".
  • If you see a **bold label** or a list item that starts with “Pokémon Center”, “Poké Mart”, “Pokémon Academy”, etc., treat it as a new subsection whose heading is that label.

Example row  →  Sentence
["Bulbasaur","Grass","5-7","25 %"]
→ "Bulbasaur can be encountered in the grass at levels 5-7 with a 25 % encounter rate."
Write a sentence for rival facts as well highlighting every detail.
* Do NOT lose evolution level numbers or move names.
* Temperature 0. Return nothing but JSON.
""").strip()

response = client.chat.completions.create(
    model="gpt-4o-mini",        # or "gpt-4o" / "gpt-3.5-turbo-0125"
    temperature=0,
    response_format={ "type": "json_object" },
    messages=[
        { "role": "system", "content": EXTRACT_PROMPT },
        { "role": "user",   "content": markdown_text }  # 30k token guard
    ]
)

json_output = json.loads(response.choices[0].message.content)
out_path = Path("route1_rag_chunks_1.json")
out_path.write_text(json.dumps(json_output, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"✅ Saved {len(json_output['sections'])} sections → {out_path}")




