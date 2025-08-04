# html_to_json_parser.py
# Python script to parse HTML from Bulbapedia (FireRed & LeafGreen walkthrough) into nested JSON for RAG

import os
import json
import requests
from bs4 import BeautifulSoup
import re
from bs4.element import Tag
import unicodedata

def normalize(s):
    return s.strip().lower()

def table_rows(tbl):
    """
    Return the immediate <tr> rows for a table, accounting for implicit <tbody>.
    """
    body = tbl.find('tbody') or tbl
    return body.find_all('tr', recursive=False)


def is_gym_leader_href(href: str) -> bool:
    if not href:
        return False
    h = href.strip().lower()
    h = re.sub(r'^https?:', '', h)  # strip scheme if present
    # match absolute or relative
    return '/wiki/gym_leader' in h.split('#')[0].split('?')[0]

def _closest_expandable_table(node: Tag) -> Tag | None:
    """Climb from a node to the nearest <table class='expandable'>, else nearest <table>."""
    if not isinstance(node, Tag):
        return None
    t = node.find_parent('table', class_='expandable')
    if t:
        return t
    return node.find_parent('table')

def closest_expandable_wrapper(node: Tag) -> Tag | None:
    """Climb to nearest <table class='expandable'>; if none, nearest <table>."""
    if not isinstance(node, Tag):
        return None
    t = node.find_parent('table', class_='expandable')
    if t:
        return t
    return node.find_parent('table')

def _extract_moves(card_tbl, return_compact=False, sep=', '):
    """
    Extract moves from a per-Pokémon card table.
    - Skips hidden tables (style contains display:none).
    - Skips placeholders like '--'.
    - Dedupes by (name, type) preserving order.
    - If return_compact=True, returns one element:
        [{'name': 'Move1 Move2', 'type': 'Type1 Type2'}]
      else: [{'name': name, 'type': type}, ...]
    """
    seen = set()
    moves = []

    for mt in card_tbl.find_all('table', class_='roundy'):
        style = (mt.get('style') or '').lower().replace(' ', '')
        if 'display:none' in style:
            continue

        m_rows = table_rows(mt)
        if len(m_rows) < 2:
            continue

        name_txt = norm_text(m_rows[0])
        if not name_txt or name_txt.strip() in ('--', '\xa0'):
            continue

        # Prefer an <a> link on the 2nd row for the move type
        type_cell = m_rows[1]
        type_link = type_cell.find('a')
        type_txt = norm_text(type_link) if type_link else norm_text(type_cell)
        type_txt = type_txt.strip()

        key = (name_txt, type_txt)
        if key in seen:
            continue
        seen.add(key)

        moves.append({'name': name_txt, 'type': type_txt})

    if return_compact:
        names = sep.join(m['name'] for m in moves)
        types = sep.join(m['type'] for m in moves)
        return [{'name': names, 'type': types}]

    return moves

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

def is_gym_leader_block(table: Tag) -> bool:
    """
    True iff THIS exact <table> is the gym-leader wrapper:
    - it contains a descendant <a> whose href points to /wiki/Gym_Leader
    - and that anchor's closest expandable table == this table
    """
    if not isinstance(table, Tag) or table.name != 'table':
        return False
    a = table.find('a', href=lambda h: is_gym_leader_href(h))
    if not a:
        return False
    owner = _closest_expandable_table(a)
    return owner is not None and owner == table

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
    
    def is_sparse_fake_header(row, headers):
        normalized = [normalize(cell) for cell in row]
        nonempty = [(i, val) for i, val in enumerate(normalized) if val]
        if len(nonempty) < 3:
            # If all non-empty cells match expected header names at those positions
            return all(headers[i].lower() == val for i, val in nonempty)
        return False
    if out and is_sparse_fake_header(out[0], headers):
        print("Removing fake header row:", out[0])
        out = out[1:]
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




def parse_gym_leader_block(wrapper: Tag) -> dict:
    """
    Extract leader card + party (Pokémon panels) from the expandable wrapper table.
    Output:
      {
        'leader': {'name','role','gym','games','reward','portrait','balls'},
        'party': [
          {'sprite','name','level','types':[],'ability','item','moves':[{'name','type'}]}
        ]
      }
    """
    leader = {'name': '', 'role': 'Leader', 'gym': '', 'games': '', 'reward': '', 'portrait': '', 'balls': 0}
    party = []

    # 1) CARD AREA (visible): find the <a href="/wiki/Gym_Leader"> anchor and its info table
    a = wrapper.find('a', href=lambda h: is_gym_leader_href(h))
    info_tbl = a.find_parent('table') if a else None
    if info_tbl:
        # portrait often sits in a <th> sibling in the same header block
        th = info_tbl.find_previous('th')
        if th:
            img = th.find('img')
            if img and img.get('src'):
                leader['portrait'] = img['src']

        # parse rows for name, gym, games
        for tr in table_rows(info_tbl):
            t = norm_text(tr)
            if not t:
                continue
            # name row usually has <big>
            if not leader['name'] and tr.find('big'):
                leader['name'] = norm_text(tr)
            elif 'Gym' in t and not leader['gym']:
                leader['gym'] = t
            elif any(k in t for k in ('FireRed', 'LeafGreen', 'Gold', 'Silver', 'Crystal', 'Emerald', 'Yellow', 'Blue', 'Red')):
                leader['games'] = t

    # reward and party-size balls anywhere under wrapper
    full_text = wrapper.get_text(' ', strip=True)
    m = re.search(r'Reward:\s*\$?\s*([0-9,]+)', full_text)
    if m:
        leader['reward'] = m.group(1).replace(',', '')
    leader['balls'] = len(wrapper.find_all('img', src=lambda s: s and 'Ballfull.png' in s))

    # 2) HIDDEN PARTY: sibling row with style containing 'display'
    hidden_rows = wrapper.find_all(
    lambda tag: tag.name == 'tr' and tag.has_attr('style') and 'display' in tag['style']
)
    party = []
    seen = set()  # de-dupe by (name, level)
    for hidden in hidden_rows:
    # Search the entire hidden area, not just the first inner table
        for card in hidden.find_all('table', class_='roundy'):
            if not _looks_like_mon_card(card):
                continue
            mon = _extract_mon_from_card(card)
            if not mon['name'] or not mon['level']:
                continue 
            # accept if we have strong signals
            key = (mon.get('name',''), mon.get('level',''))
            if key in seen:
                continue
            seen.add(key)
            party.append(mon)

    return {'leader': leader, 'party': party}


def _extract_leader_card(header_tbl):
    """
    From the visible card: portrait, role (Leader), name, gym, games.
    """
    data = {'name': '', 'role': '', 'gym': '', 'games': '', 'portrait': ''}

    # Portrait image: the first <img> inside a <th> is usually the portrait
    portrait = header_tbl.find('th')
    if portrait:
        img = portrait.find('img')
        if img and img.get('src'):
            data['portrait'] = img['src']

    # The right-hand side is a small roundy table with Leader / Name / Gym / Games
    info_tbl = header_tbl.find('table', class_='roundy')
    if info_tbl:
        trs = table_rows(info_tbl)
        for tr in trs:
            t = norm_text(tr)
            if not t:
                continue
            if 'Leader' in t and not data['role']:
                data['role'] = 'Leader'
            elif not data['name'] and tr.find('big'):
                # Name often wrapped in <big>
                a = tr.find('a')
                data['name'] = norm_text(a) if a else norm_text(tr)
            elif 'Gym' in t and not data['gym']:
                data['gym'] = norm_text(tr)
            elif not data['games'] and ('FireRed' in t or 'LeafGreen' in t or 'Gold' in t or 'Silver' in t):
                data['games'] = t

    return data


def _extract_reward_and_balls(header_tbl):
    """
    In the header block, one cell has 'Reward: $XXXX'.
    Another cell with a small roundy table shows ball icons indicating party size.
    """
    out = {'reward': '', 'balls': 0}
    # Search reward in the whole header_tbl text
    txt = header_tbl.get_text(' ', strip=True)
    m = re.search(r'Reward:\s*\$?\s*([0-9,]+)', txt)
    if m:
        out['reward'] = m.group(1).replace(',', '')
    # Count 'Ballfull.png' occurrences near header
    out['balls'] = len(header_tbl.find_all('img', src=lambda s: s and 'Ballfull.png' in s))
    return out

def _looks_like_mon_card(tbl: Tag) -> bool:
    """A per-Pokémon card table if it links to a Pokémon page and contains 'Lv.' somewhere."""
    hrefs = [a.get('href','') for a in tbl.find_all('a', href=True)]
    has_pkmn_link = any('_(Pok%C3%A9mon)' in h or '(Pokémon)' in h for h in hrefs)
    if not has_pkmn_link:
        return False
    text = tbl.get_text(' ', strip=True)
    return ('Lv.' in text) or re.search(r'\bLv\.?\s*\d+', text, flags=re.I) is not None

def _extract_party_from_container(container: Tag) -> list[dict]:
    mons = []
    for card in container.find_all('table', class_='roundy'):
        if not _looks_like_mon_card(card):
            continue
        mon = _extract_mon_from_card(card)
        if mon.get('name'):
            mons.append(mon)
    return mons



def _extract_mon_from_card(card_tbl: Tag) -> dict:
    mon = {'sprite': '', 'name': '', 'level': '', 'types': [], 'ability': '', 'item': '', 'moves': []}
    rows = table_rows(card_tbl)
    if not rows:
        return mon

    # Row 1: sprite (left) + right block (types/ability/item)
    r1 = rows[0]
    r1_cells = row_cells(r1)
    if r1_cells:
        img = r1_cells[0].find('img') if len(r1_cells) >= 1 else None
        if img and img.get('src'):
            mon['sprite'] = img['src']

        if len(r1_cells) >= 2:
            right = r1_cells[1]
            t_tbl = _find_labeled_table(right, 'Types')
            if t_tbl:
                t_rows = table_rows(t_tbl)
                if len(t_rows) >= 2:
                    mon['types'] = [norm_text(c) for c in row_cells(t_rows[1]) if norm_text(c)]
            a_tbl = _find_labeled_table(right, 'Ability')
            if a_tbl:
                a_rows = table_rows(a_tbl)
                if len(a_rows) >= 2:
                    mon['ability'] = norm_text(a_rows[1])
            i_tbl = _find_labeled_table(right, 'Held item')
            if i_tbl:
                i_rows = table_rows(i_tbl)
                if len(i_rows) >= 2:
                    mon['item'] = norm_text(i_rows[1])

    # Row 2: name + Lv.
    if len(rows) >= 2:
        name_cell = row_cells(rows[1])[0] if row_cells(rows[1]) else None
        if name_cell:
            a = name_cell.find('a', href=True)
            if a:
                mon['name'] = norm_text(a)  # 'Rhyhorn'
            m = re.search(r'Lv\.?\s*(\d+)', norm_text(name_cell), flags=re.I)
            if m:
                mon['level'] = m.group(1)

    # Moves: later rows contain multiple tiny roundy tables (each name row + type row)
    for tr in rows[2:]:
        for mt in tr.find_all('table', class_='roundy'):
            m_rows = table_rows(mt)
            if len(m_rows) >= 2:
                move_name = norm_text(m_rows[0])
                move_type = norm_text(m_rows[1])
                if move_name:
                    mon['moves'].append({'name': move_name, 'type': move_type})
    return mon


def _find_labeled_table(scope: Tag, label: str) -> Tag | None:
    """Find a mini roundy table whose first row contains the label (e.g., 'Types:', 'Ability:', 'Held item:')."""
    for t in scope.find_all('table', class_='roundy'):
        trs = table_rows(t)
        if trs:
            first_txt = norm_text(trs[0])
            if label.lower() in first_txt.lower():
                return t
    return None



def parse_content(soup, image_dir='images'):
    """Traverse the HTML DOM and build nested JSON structure, unwrapping collapsible tables."""
    content_root = soup.find(id='mw-content-text')
    main = content_root.find(class_='mw-parser-output')

    sections = []
    stack = [{'level': 1, 'node_list': sections}]
    processed_tables = set()

    for el in main.children:
        if not isinstance(el, Tag):
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

        if el.name == 'figure':
            img = el.find('img')
            if img and img.get('src'):
                src = img.get('src')
                local_path = None  # or download with download_image()
                caption_el = el.find('figcaption')
                caption = caption_el.get_text(' ', strip=True) if caption_el else None
                stack[-1]['node_list'].append({ 'type': 'image', 'src': src, 'local_path': local_path, 'caption': caption })

        tables_here = [el] if el.name == 'table' else el.find_all('table')
        # Tables (including collapsible wrappers)
    
        for tbl in tables_here:
            anchor = tbl.find('a', href=lambda h: is_gym_leader_href(h))
            if anchor:
                wrapper = closest_expandable_wrapper(anchor)
                if wrapper:
                    wid = id(wrapper)
                    if wid not in processed_tables:
                        processed_tables.add(wid)
                        gym_data = parse_gym_leader_block(wrapper)
                        # Debug once to verify:
                        # print("GYM LEADER:", gym_data['leader'].get('name'), "party size:", len(gym_data['party']))
                        stack[-1]['node_list'].append({'type': 'gym_battle', 'data': gym_data})
            key = id(tbl)
            if key in processed_tables:
                continue
            processed_tables.add(key)

            if is_gym_leader_block(tbl):
                gym_data = parse_gym_leader_block(tbl)
                print("GYM LEADER:", gym_data['leader'].get('name'), "party size:", len(gym_data['party']))
                stack[-1]['node_list'].append({'type': 'gym_battle', 'data': gym_data})
                continue

            wrapper_title = get_table_title(tbl) if 'get_table_title' in globals() else ''
            inner = unwrap_inner_data_table(tbl)
            inner_title = get_table_title(inner)

            header_text = norm_text(inner)
            

            if ('Trainers' in wrapper_title) or ('Trainer' in header_text):
                table_data = parse_trainers_table(tbl)  # pass the wrapper; parser unwraps
                stack[-1]['node_list'].append({'type': 'table', 'data': table_data})
                continue

            if ('Available Pokémon' in wrapper_title) or ('Available' in header_text and 'Pokémon' in header_text):
                table_data = parse_available_pokemon_table(tbl)
                stack[-1]['node_list'].append({'type': 'table', 'data': table_data})
                continue

                # Items (or anything else) → generic fallback on the inner data table
            table_data = parse_table(inner)
            stack[-1]['node_list'].append({'type': 'table', 'data': table_data})

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
