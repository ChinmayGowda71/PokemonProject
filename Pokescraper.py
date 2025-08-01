# html_to_json_parser.py
# Python script to parse HTML from Bulbapedia (FireRed & LeafGreen walkthrough) into nested JSON for RAG

import os
import json
import requests
from bs4 import BeautifulSoup
import re

def table_rows(tbl):
    """
    Return the immediate <tr> rows for a table, accounting for implicit <tbody>.
    """
    body = tbl.find('tbody') or tbl
    return body.find_all('tr', recursive=False)

def row_cells(tr):
    """
    Return the immediate cells for a row (<td>/<th>), not diving into nested tables.
    """
    return tr.find_all(['td', 'th'], recursive=False)

def norm_text(el):
    """Extract visible text, replace <br> with spaces, strip [show]/[hide], collapse whitespace."""
    if not el:
        return ""
    for br in el.find_all('br'):
        br.replace_with(' ')
    txt = el.get_text(' ', strip=True)
    txt = re.sub(r'\[[^\]]*\]', '', txt)      # remove [show], [hide], etc.
    return ' '.join(txt.split())

def title_matches(title, *needles):
    """Case-insensitive contains, normalize 'Pokémon'."""
    t = title.lower().replace('pokémon', 'pokemon')
    return all(n.lower().replace('pokémon', 'pokemon') in t for n in needles)

def th_texts_for(table):
    """Yield text for <th> whose closest parent table is this table (ignore deeper nested tables)."""
    for th in table.find_all('th'):
        if th.find_parent('table') == table:
            txt = norm_text(th)
            if txt:
                yield txt

def get_table_title(table):
    """Best-effort table title: first top-level th/td, else first scoped <th> text."""
    first_tr = table.find('tr', recursive=False)
    if first_tr:
        first_cell = first_tr.find(['th','td'], recursive=False)
        if first_cell:
            t = norm_text(first_cell)
            if t:
                return t
    for t in th_texts_for(table):
        return t
    return ""

def unwrap_inner_data_table(wrapper_table):
    """
    Bulbapedia uses a wrapper table with [show] + a hidden <td><table class='roundy'>…</table></td>.
    Return the inner 'roundy' table if present; otherwise the wrapper itself.
    """
    # If first row is a [show] header, grab the first nested <table>
    top_trs = wrapper_table.find_all('tr', recursive=False)
    if top_trs:
        first_cell = top_trs[0].find(['th','td'], recursive=False)
        if first_cell and norm_text(first_cell).startswith('['):  # e.g., “[show] Trainers”
            inner = wrapper_table.find('table')
            if inner:
                return inner
    # Prefer a 'roundy' table when present
    inner_roundy = wrapper_table.find('table', class_='roundy')
    return inner_roundy or wrapper_table

def is_headerish_row(cells):
    """
    Return True if this <tr> is a header/sub-header row we should skip.
    Catches:
      - rows composed only of <th>
      - rows whose non-empty texts are just labels like 'Pokémon', 'Location', 'FR', 'LG', ...
      - pattern like ['Pokémon', '', 'Location', ...]
    """
    labels = {'Pokémon', 'Pokemon', 'Games', 'Location', 'Levels', 'Rate', 'FR', 'LG'}
    texts = [norm_text(c) for c in cells]
    nonempty = [t for t in texts if t]

    if not nonempty:
        return True
    if all(c.name == 'th' for c in cells):
        return True
    if set(nonempty).issubset(labels):
        return True
    if (texts and texts[0] in ('Pokémon', 'Pokemon')) and any(t == 'Location' for t in texts[1:]):
        return True
    return False


def parse_table(table):
    """Extract headers and rows from an HTML table, handling nested tables as cells."""
    # Collect all rows whose closest table parent is this table (excludes nested table rows)
    all_trs = table.find_all('tr')
    rows = [tr for tr in all_trs if tr.find_parent('table') == table]
    if not rows:
        return {'headers': [], 'rows': []}

    # First row as header row
    header_row = rows[0]
    header_cells = header_row.find_all(['th', 'td'], recursive=False)
    headers = []
    for cell in header_cells:
        # Replace <br> tags with spaces for clarity
        for br in cell.find_all('br'):
            br.replace_with(' ')
        text = cell.get_text(' ', strip=True)
        if text:
            headers.append(text)

    # Parse data rows
    data_rows = []
    for row in rows[1:]:
        cells = row.find_all(['td', 'th'], recursive=False)
        if not cells:
            continue
        row_data = []
        for cell in cells:
            # If nested table exists, recurse
            nested = cell.find('table')
            if nested:
                nested_data = parse_table(nested)
                row_data.append(nested_data)
            else:
                for br in cell.find_all('br'):
                    br.replace_with(' ')
                text = cell.get_text(' ', strip=True)
                if text:
                    row_data.append(text)
        if row_data:
            data_rows.append(row_data)

    return {'headers': headers, 'rows': data_rows}

def _extract_pokemon_name_from_left(cell):
    """Leftmost cell is often a tiny table: [sprite] | name. Grab the name text."""
    tbl = cell.find('table') or cell
    trs = table_rows(tbl)
    if not trs:
        return norm_text(cell)
    first = trs[0]
    c = row_cells(first)
    if len(c) >= 2:
        return norm_text(c[1])
    return norm_text(cell)

def _extract_games_tokens(cells):
    """Find FR/LG tokens among a few adjacent cells."""
    tokens = []
    for c in cells:
        t = norm_text(c)
        for p in re.split(r'\W+', t):
            if p in ('FR', 'LG') and p not in tokens:
                tokens.append(p)
    return '/'.join(tokens) if tokens else ''




def parse_available_pokemon_table(wrapper):
    """Return {'headers': ['Pokémon','Games','Location','Levels','Rate'], 'rows': [...]}."""
    tbl = unwrap_inner_data_table(wrapper)
    rows = table_rows(tbl)
    if not rows:
        return {'headers': [], 'rows': []}

    headers = ['Pokémon', 'Games', 'Location', 'Levels', 'Rate']
    out = []
    current_section = None  # e.g., 'Walking', 'Surfing', 'Fishing' (optional)

    for tr in rows:
        cells = row_cells(tr)
        if not cells:
            continue

        # Section banner: single <th colspan="...">Walking/Surfing/Fishing/...
        if len(cells) == 1 and cells[0].name == 'th' and cells[0].has_attr('colspan'):
            current_section = norm_text(cells[0])
            continue

        # Data row: extract fields robustly
        name = _extract_pokemon_name_from_left(cells[0])

        games = _extract_games_tokens(cells[1:6])  # scan a small window for FR/LG

        # Location: nested tiny table with icon + text (Grass/Surfing/etc.)
        location = ''
        for c in cells:
            loc_tbl = c.find('table')
            if loc_tbl:
                t = norm_text(loc_tbl)
                if any(key in t for key in ('Grass', 'Surf', 'Surfing', 'Cave', 'Fishing', 'Walking')):
                    location = t
                    break
        if not location and len(cells) >= 3:
            location = norm_text(cells[-3])  # heuristic fallback

        # Levels: first cell that looks like a level/range
        levels = ''
        for c in cells:
            t = norm_text(c)
            if re.search(r'\d', t) and any(sym in t for sym in ('-', ',', 'Lv', 'level', 'Levels')):
                levels = t.replace('Lv. ', '').replace('Lv.', '')
                break

        # Rate: last cell that has a percentage
        rate = ''
        for c in reversed(cells):
            t = norm_text(c)
            if '%' in t:
                rate = t
                break

        out.append([name, games, location, levels, rate])

    return {'headers': headers, 'rows': out}



def _extract_one_pokemon(poke_tbl):
    """
    A Pokémon cell is typically a tiny inner table:
      row 1: [sprite] | name | level
      row 2: item text (e.g., 'No item')
    Return dict {'name':..., 'level':..., 'item':...}
    """
    trs = table_rows(poke_tbl)
    if not trs:
        return None

    name, level, item = '', '', ''

    # Row 1: name/level
    r1 = trs[0]
    r1_cells = row_cells(r1)
    # Often: r1_cells[0]=sprite(th), r1_cells[1]=name, r1_cells[2]=level
    if len(r1_cells) >= 2:
        name = norm_text(r1_cells[1])
    if len(r1_cells) >= 3:
        level = norm_text(r1_cells[2]).replace('Lv. ', '').replace('Lv.', '')

    # Row 2: item (if present)
    if len(trs) >= 2:
        item = norm_text(trs[1])

    return {'name': name, 'level': level, 'item': item}



def parse_trainers_table(wrapper):
    """Return {'headers': ['Trainer','Pokémon'], 'rows': [[trainer_text, [pokemon_dict...]], ...]}"""
    tbl = unwrap_inner_data_table(wrapper)

    rows = table_rows(tbl)
    if not rows:
        return {'headers': ['Trainer','Pokémon'], 'rows': []}

    # Header resolution (fallback to canonical)
    hdr_cells = row_cells(rows[0])
    headers = ['Trainer', 'Pokémon']
    if hdr_cells:
        hdrs = [norm_text(c) for c in hdr_cells]
        if any('trainer' in h.lower() for h in hdrs):
            headers = ['Trainer', 'Pokémon']

    out = []
    current = None  # {'trainer': str, 'pokelist': []}

    # Iterate data rows (skip header row)
    for tr in rows[1:]:
        cells = row_cells(tr)
        if not cells:
            continue

        # New trainer row usually has 2 cells: left trainer cell, right mon cell (which itself is a tiny table)
        if len(cells) >= 2:
            trainer_text = norm_text(cells[0])
            if trainer_text:
                # flush previous group
                if current:
                    out.append([current['trainer'], current['pokelist']])
                current = {'trainer': trainer_text, 'pokelist': []}

            mon_cell = cells[-1]
            poke_tbl = mon_cell.find('table')
            if poke_tbl:
                mon = _extract_one_pokemon(poke_tbl)
                if mon and current:
                    current['pokelist'].append(mon)

        # Continuation row (rowspan on trainer cell): only the right cell present
        elif len(cells) == 1 and current:
            mon_cell = cells[0]
            poke_tbl = mon_cell.find('table')
            if poke_tbl:
                mon = _extract_one_pokemon(poke_tbl)
                if mon:
                    current['pokelist'].append(mon)

    # flush last
    if current:
        out.append([current['trainer'], current['pokelist']])

    return {'headers': headers, 'rows': out}




def download_image(src, output_dir):
    """Download an image from a URL into output_dir, returning local path."""
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.basename(src)
    path = os.path.join(output_dir, filename)
    if not os.path.exists(path):
        resp = requests.get(src)
        resp.raise_for_status()
        with open(path, 'wb') as f:
            f.write(resp.content)
    return path


def parse_content(soup, image_dir='images'):
    """Traverse the HTML DOM and build nested JSON structure, unwrapping collapsible tables."""
    content_root = soup.find(id='mw-content-text')
    main = content_root.find(class_='mw-parser-output')

    sections = []
    stack = [{'level': 1, 'node_list': sections}]

    for el in main.children:
        if not hasattr(el, 'name'):
            continue

        # Handle headings h2-h6
        if el.name and el.name.startswith('h') and el.name[1].isdigit():
            level = int(el.name[1])
            title = el.get_text(' ', strip=True)
            node = {
                'type': 'section',
                'title': title,
                'level': level,
                'metadata': { 'id': el.get('id'), 'class': el.get('class', []) },
                'content': []
            }
            while stack and stack[-1]['level'] >= level:
                stack.pop()
            stack[-1]['node_list'].append(node)
            stack.append({'level': level, 'node_list': node['content']})
            continue

        # Paragraphs
        if el.name == 'p':
            for br in el.find_all('br'):
                br.replace_with(' ')
            text = el.get_text(' ', strip=True)
            if text:
                stack[-1]['node_list'].append({'type': 'paragraph', 'text': text})
            continue
        # Lists
        if el.name in ['ul', 'ol']:
            items = []
            for li in el.find_all('li'):
                for br in li.find_all('br'):
                    br.replace_with(' ')
                text = li.get_text(' ', strip=True)
                if text:
                    items.append(text)
            stack[-1]['node_list'].append({ 'type': 'list', 'ordered': (el.name == 'ol'), 'items': items })
            continue

        # Tables (including collapsible wrappers)
        if el.name == 'table':
            wrapper_title = get_table_title(el)
            inner = unwrap_inner_data_table(el)
            inner_title = get_table_title(inner)

            # Optional debug to verify triggering:
            print("TABLE:", {"wrapper": wrapper_title, "inner": inner_title})

            if title_matches(wrapper_title, 'Trainers') or title_matches(inner_title, 'Trainer'):
                table_data = parse_trainers_table(el)
            elif title_matches(wrapper_title, 'Available', 'Pokemon') or title_matches(inner_title, 'Available', 'Pokemon'):
                table_data = parse_available_pokemon_table(el)
            else:
                # generic fallback parses the inner table (not the [show] wrapper)
                table_data = parse_table(inner)

            stack[-1]['node_list'].append({'type': 'table', 'data': table_data})
            continue

        # Figures / Images
        if el.name == 'figure':
            img = el.find('img')
            if img and img.get('src'):
                src = img.get('src')
                local_path = None  # or download with download_image()
                caption_el = el.find('figcaption')
                caption = caption_el.get_text(' ', strip=True) if caption_el else None
                stack[-1]['node_list'].append({ 'type': 'image', 'src': src, 'local_path': local_path, 'caption': caption })
            continue

    return sections


def html_to_json(html_file, json_file, image_dir='images'):
    """Load HTML, parse it, and write out nested JSON."""
    with open(html_file, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'lxml')
    parsed = parse_content(soup, image_dir)
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)
    print(f"Parsed '{html_file}' → '{json_file}', images in '{image_dir}/'")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Parse FireRed/LeafGreen HTML to nested JSON for RAG')
    parser.add_argument('html_file', help='Input HTML file')
    parser.add_argument('json_file', help='Output JSON file')
    parser.add_argument('--image-dir', default='images', help='Directory to save images')
    args = parser.parse_args()
    html_to_json(args.html_file, args.json_file, args.image_dir)
