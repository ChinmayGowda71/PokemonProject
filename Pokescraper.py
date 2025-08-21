# html_to_json_parser.py
# Python script to parse HTML from Bulbapedia (FireRed & LeafGreen walkthrough) into nested JSON for RAG

import os
import json
import requests
from bs4 import BeautifulSoup
import re
from bs4.element import Tag
import unicodedata


BATTLE_BALL_RE = re.compile(r'Ball(full|empty)\.png', re.I)

def normalize(s):
    return s.strip().lower()

def table_rows(tbl):
    """
    Return the immediate <tr> rows for a table, accounting for implicit <tbody>.
    """
    body = tbl.find('tbody') or tbl
    return body.find_all('tr', recursive=False)

def find_battle_card_wrappers(root):
    """
    Find unique wrapper <table>s that contain a Bulbapedia battle card
    anywhere under `root`. We locate all head rows id='collapsible-section_*'
    and return the outermost <table> that contains each head.
    """
    wrappers = []
    seen = set()

    for head in root.find_all('tr', id=re.compile(r'^collapsible-section_\d+$')):
        # Climb to the nearest <table> that contains this header. Use the
        # first ancestor that's a <table> — that is the wrapper to parse.
        wrapper = head.find_parent('table')
        if not wrapper:
            continue
        key = id(wrapper)
        if key in seen:
            continue
        # sanity: ensure the matching hidden row exists under the same wrapper
        sib = head.find_next_sibling('tr')
        if not (sib and 'display' in (sib.get('style',''))):
            continue
        seen.add(key)
        wrappers.append(wrapper)

    return wrappers

def _find_expandable_pair(wrapper_tbl):
    """
    Return (head_tr, hidden_tr) for a Bulbapedia battle card inside wrapper_tbl,
    or (None, None) if not present.
    """
    if not wrapper_tbl or wrapper_tbl.name != 'table':
        return (None, None)

    # The header row has id like collapsible-section_10
    head = wrapper_tbl.find('tr', id=re.compile(r'^collapsible-section_\d+$'))
    if not head:
        return (None, None)

    hidden = head.find_next_sibling('tr')
    if not (hidden and 'display' in (hidden.get('style',''))):
        return (None, None)

    # extra guard: the header must contain a circular portrait and a reward cell
    portrait_th = head.find(lambda th: th.name=='th' and 'overflow:hidden' in (th.get('style','')))
    reward_cell = head.find(lambda t: t.name in ('td','th') and 'reward:' in t.get_text(strip=True).lower())
    if not (portrait_th and reward_cell):
        return (None, None)

    return (head, hidden)

def is_battle_card(tbl):
    if not (tbl and getattr(tbl, 'name', None) == 'table'):
        return False

    # 1) Head + hidden row
    head = tbl.find('tr', id=re.compile(r'^collapsible-section_'))
    hidden = head.find_next_sibling('tr') if head else None
    if not (head and hidden and hidden.has_attr('style') and 'display' in hidden['style']):
        return False

    header_text = head.get_text(' ', strip=True)

    # 2) Exclude "Available Pokémon" expandables early
    if re.search(r'available pok[eé]mon', header_text, re.I):
        return False

    # 3) Must have a Reward: somewhere in the header block
    if 'Reward:' not in header_text:
        # allow a search in the immediate header cluster just in case Reward cell sits next to it
        reward_cell = head.find(lambda t: t.name in ('td', 'th') and 'Reward:' in t.get_text())
        if not reward_cell:
            return False

    # 4) Must show Poké Ball strip (full/empty balls) in the header cluster
    balls = head.find_all('img', src=re.compile(r'Ball(full|empty)\.(png|svg)$', re.I))
    if not balls:
        # sometimes the ball strip is a sibling table in the header block
        balls = tbl.find_all('img', src=re.compile(r'Ball(full|empty)\.(png|svg)$', re.I))
        if not balls:
            return False

    # 5) Hidden pane should look like a party: either has "Lv." or a Types/Type subsection
    hidden_txt = hidden.get_text(' ', strip=True)
    if not (re.search(r'\bLv\.?\b', hidden_txt) or re.search(r'\bType[s]?:\b', hidden_txt, re.I)):
        # fallback: any roundy move/type tiles present?
        roundies = hidden.find_all('table', class_='roundy')
        clue = any(r.find(string=re.compile(r'\bType[s]?:\b', re.I)) or r.find(string=re.compile(r'\bLv\.?\b'))
                   for r in roundies)
        if not clue:
            return False

    return True


def extract_reward(tbl):
    reward = ''
    cell = tbl.find(lambda t: t.name in ('td','th') and 'reward:' in t.get_text(strip=True).lower())
    if cell:
        raw = cell.get_text(' ', strip=True)
        m = re.search(r'([$\u00A3\u00A5]?\s*\d[\d,]*)', raw)  # $1,560 or 5000 etc.
        if m:
            reward = m.group(1).replace(',', '').strip().lstrip('$')
    return reward

def parse_battle_header(tbl):
    portrait = ''
    portrait_th = tbl.find('th', attrs={'width': re.compile(r'^80'), 'height': re.compile(r'^80')})
    if portrait_th:
        img = portrait_th.find('img')
        if img and img.get('src'):
            portrait = img['src']

    # right-side mini table
    info_tbl = portrait_th.find_next('table') if portrait_th else None
    role = name = venue = games = ''

    if info_tbl:
        rows = [r.get_text(' ', strip=True) for r in info_tbl.find_all('tr')]
        rows = [r for r in rows if r]  # drop blanks
        # expected: [role, name, maybe blank, venue, games]
        if rows: role = rows[0]
        if len(rows) > 1: name = rows[1]
        # skip noise lines like hidden display:none
        tail = [t for t in rows[2:] if t]
        if tail:
            venue = tail[0]
            if len(tail) > 1:
                games = tail[1]

    # Reward
    reward = ''
    reward_cell = tbl.find(lambda t: t.name in ('td','th') and 'Reward:' in t.get_text())
    if reward_cell:
        # keep digits only
        text = reward_cell.get_text()
        match = re.search(r'[\$₽¥€£]?\s?(\d[\d,]*)', text)
        if match:
            reward = match.group(1).replace(',', '')  

    # Ballfull/Ballempty row to count party size
    balls = 0
    balls_cell = reward_cell.find_next('td') if reward_cell else None
    if balls_cell:
        balls = len(balls_cell.find_all('img', src=lambda s: s and 'Ball' in s))

    # Normalize a “category” to help downstream (optional)
    role_low = role.lower()
    if 'elite four' in role_low:
        category = 'elite_four'
    elif 'leader' in role_low or 'gym leader' in role_low:
        category = 'gym_leader'
    elif 'rival' in role_low:
        category = 'rival'
    elif 'rocket' in role_low:
        category = 'team_rocket'
    else:
        category = 'trainer'

    return {
        'role': role,          # "Elite Four" / "Leader" / "Rival" / ...
        'name': name,          # "Lorelei" / "Giovanni"
        'venue': venue,        # "Indigo Plateau" / "Viridian Gym"
        'games': games,        # "FireRed and LeafGreen"
        'reward': reward,      # "6600" / "5000"
        'portrait': portrait,  # sprite url
        'balls': balls,
        'category': category,
    }


def extract_mon_card(card_tbl):
    mon = {
        'sprite': '',
        'name': '',
        'level': '',
        'types': [],
        'ability': '',
        'item': '',
        'moves': []
    }

    # sprite
    img = card_tbl.find('img')
    if img and img.get('src'):
        mon['sprite'] = img['src']

    # name + level
    name_cell = card_tbl.find(lambda t: t.name == 'td' and 'Lv.' in t.get_text())
    if name_cell:
        txt = name_cell.get_text(' ', strip=True)
        m = re.search(r'Lv\.?\s*([0-9\-–, ]+)', txt)
        if m:
            mon['level'] = m.group(1).strip()
            txt = txt[:m.start()].strip()
        mon['name'] = txt

    for rt in card_tbl.find_all('table', class_='roundy'):
        if 'display:none' in (rt.get('style', '') or '').replace(' ', ''):
            continue

        rows = rt.find_all('tr')
        if len(rows) != 2:
            continue

        label = rows[0].get_text(' ', strip=True).strip().lower()
        value_td = rows[1].find('td')
        if not value_td:
            continue

        if label in ('types:', 'type:'):
            types = []
            for td in rows[1].find_all('td'):
                style = td.get('style', '').lower()
                if 'display:none' in style:
                    continue  # skip hidden dummy types

                a = td.find('a', href=lambda h: h and '(type)' in h)
                if a:
                    type_text = a.get_text(' ', strip=True)
                    if type_text.lower() != 'unknown':
                        types.append(type_text)
            mon['types'] = types


        elif label == 'ability:':
            mon['ability'] = value_td.get_text(' ', strip=True)

        elif label == 'held item:':
            for img in value_td.find_all('img'):
                img.decompose()
            mon['item'] = value_td.get_text(' ', strip=True)

    # moves: only valid visible roundy tables with 2 non-empty rows (name + type)
    for mt in card_tbl.find_all('table', class_='roundy'):
        if 'display:none' in (mt.get('style', '') or '').replace(' ', ''):
            continue

        rows = mt.find_all('tr')
        if len(rows) != 2:
            continue  # valid move cards always have exactly 2 rows

        move_name = rows[0].get_text(' ', strip=True)
        move_type = rows[1].get_text(' ', strip=True)

        # Reject if first row is a label like "Types:", "Ability:", etc.
        if move_name.strip().lower() in ('types:', 'ability:', 'held item:'):
            continue

        # Reject if second row contains multiple words (likely not a type)
        if len(move_type.strip().split()) > 1:
            continue

        if move_name and move_type and move_name != '—':
            mon['moves'].append({'name': move_name, 'type': move_type})

    return mon

def parse_battle_party(tbl):
    party = []
    hidden = tbl.find(lambda t: t.name == 'tr' and t.has_attr('style') and 'display' in t['style'])
    if not hidden:
        return party

    # Usually the immediate child is the colored frame; under it we see many width=250px roundy tables (each mon)
    card_tables = hidden.find_all('table', class_='roundy', attrs={'width': re.compile(r'^250')})
    if not card_tables:
        # fallback: any 'roundy' tables that contain a Lv. marker
        card_tables = [t for t in hidden.find_all('table', class_='roundy')
                       if t.find(string=lambda s: s and 'Lv.' in s)]

    seen = 0
    for card in card_tables:
        mon = extract_mon_card(card)
        if mon.get('name') or mon.get('sprite') or mon.get('level'):
            party.append(mon); seen += 1

    # Safety: if zero found, try a broader sweep (rare layouts)
    if not party:
        for card in hidden.find_all('table'):
            if card.find('img') and card.find(string=lambda s: s and 'Lv.' in s):
                mon = extract_mon_card(card)
                if mon.get('name') or mon.get('sprite') or mon.get('level'):
                    party.append(mon)

    return party

def parse_battle_card(wrapper_tbl):
    head, hidden = _find_expandable_pair(wrapper_tbl)
    if not head:
        inner = unwrap_inner_data_table(wrapper_tbl)
        head, hidden = _find_expandable_pair(inner)
    assert head and hidden, "not a battle card wrapper"

    # ---- portrait (only the circular TH in the header) ----
    th_portrait = head.find('th', attrs={'style': re.compile(r'overflow\s*:\s*hidden', re.I)})
    img = th_portrait.find('img') if th_portrait else None
    portrait = img['src'] if (img and img.get('src')) else ''

    # ---- role / name / venue / games (header mini-table) ----
    info_tbl = head.find('table', class_='roundy')
    role = name = venue = games = ''
    if info_tbl:
        # role: the first td that literally contains one of these words
        rcell = info_tbl.find(lambda t: t.name == 'td' and re.search(
            r'\b(leader|elite four|admin|grunt|scientist|tamer|cooltrainer)\b', t.get_text(' ', strip=True), re.I))
        role = rcell.get_text(' ', strip=True) if rcell else ''

        # name: the BIG bold text cell (reliable on Bulbapedia cards)
        b = info_tbl.find('big')
        if b:
            name = b.get_text(' ', strip=True)

        # venue: the cell that has "Gym" / "Plateau" / "League"
        vcell = info_tbl.find(lambda t: t.name == 'td' and re.search(r'gym|plateau|league', t.get_text(' ', strip=True), re.I))
        venue = vcell.get_text(' ', strip=True) if vcell else ''

        # games: the small text line at the bottom
        scell = info_tbl.find('small')
        games = scell.get_text(' ', strip=True) if scell else ''

    # ---- reward (header only) ----
    reward = ''
    reward_cell = head.find(lambda t: t.name in ('td', 'th') and 'reward:' in t.get_text(strip=True).lower())
    if reward_cell:
        raw = reward_cell.get_text(' ', strip=True)
        m = re.search(r'(\d[\d,]*)', raw)
        if m:
            reward = m.group(1).replace(',', '')

    # ---- balls (header only) ----
    balls = len(head.find_all('img', src=re.compile(r'Ball(full|empty)\.png', re.I)))

    # ---- party (only 250px roundy tables inside the hidden row) ----
    # These are the actual per-mon cards; don't search broadly.
    party = []
    mon_tables = hidden.select('table.roundy[width="250px"]')
    for card_tbl in mon_tables:
        party.append(extract_mon_card(card_tbl))

    # conservative fallback (rare): pick any roundy table that clearly has a Lv.
    if not party:
        for candidate in hidden.find_all('table', class_='roundy'):
            if candidate.find(string=re.compile(r'\bLv\.\b')):
                party.append(extract_mon_card(candidate))

    category = ('elite_four' if re.search(r'elite\s+four', role, re.I)
                else 'gym_leader' if re.search(r'\bgym\b', venue, re.I)
                else 'trainer')

    return {
        'type': 'battle_card',
        'data': {
            'trainer': {
                'role': role,
                'name': name,
                'venue': venue,
                'games': games,
                'reward': reward,
                'portrait': portrait,
                'balls': balls,
                'category': category
            },
            'party': party
        }
    }

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

def _find_expandable_pair(wrapper_tbl):
    """
    Return (head_tr, hidden_tr) for a Bulbapedia battle card inside wrapper_tbl,
    or (None, None) if not present.
    """
    if not wrapper_tbl or wrapper_tbl.name != 'table':
        return (None, None)

    # The header row has id like collapsible-section_10
    head = wrapper_tbl.find('tr', id=re.compile(r'^collapsible-section_\d+$'))
    if not head:
        return (None, None)

    hidden = head.find_next_sibling('tr')
    if not (hidden and 'display' in (hidden.get('style',''))):
        return (None, None)

    # extra guard: the header must contain a circular portrait and a reward cell
    portrait_th = head.find(lambda th: th.name=='th' and 'overflow:hidden' in (th.get('style','')))
    reward_cell = head.find(lambda t: t.name in ('td','th') and 'reward:' in t.get_text(strip=True).lower())
    if not (portrait_th and reward_cell):
        return (None, None)

    return (head, hidden)
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

def _is_white_bg(tag):
    style = (tag.get('style') or '').lower()
    return '#fff' in style or 'white' in style

def _extract_games_tokens_bg(cells):
    tokens = []
    for c in cells:
        txt = norm_text(c).upper()
        if ('FR' in txt or 'LG' in txt) and not _is_white_bg(c):
            if 'FR' in txt and 'FR' not in tokens:
                tokens.append('FR')
            if 'LG' in txt and 'LG' not in tokens:
                tokens.append('LG')
    return '/'.join(tokens)

def _emit_available_rows(inner_tbl, section_label, rows_out):
    for tr in table_rows(inner_tbl):
        cells = row_cells(tr)
        if not cells:
            continue

        # Skip in-table banners like "Surfing", "Fishing"
        if len(cells) == 1 and cells[0].name == 'th' and cells[0].has_attr('colspan'):
            continue

        name = _extract_pokemon_name_from_left(cells[0])
        if not name:   # guard against stray layout rows
            continue

        games = _extract_games_tokens_bg(cells[1:8])

        # Location: tiny nested table (Grass/Surfing/Cave/Rock Smash/Walking…)
        location = ''
        for c in cells:
            loc_tbl = c.find('table')
            if loc_tbl:
                t = norm_text(loc_tbl)
                if any(k in t for k in ('Grass','Surf','Surfing','Cave','Fishing','Walking','Rock Smash')):
                    location = t
                    break
        if not location and len(cells) >= 3:
            location = norm_text(cells[-3])

        # Levels
        levels = ''
        for c in cells:
            t = norm_text(c)
            if re.search(r'\d', t) and any(sym in t for sym in ('-', ',', 'Lv', 'level', 'Levels')):
                levels = t.replace('Lv. ', '').replace('Lv.', '')
                break

        # Rate
        rate = ''
        for c in reversed(cells):
            t = norm_text(c)
            if '%' in t:
                rate = t
                break

        rows_out.append([name, games, location, levels, rate, section_label])


def parse_available_pokemon_table(wrapper, parent_section):
    """
    Emits rows with Section set to the immediate sub-area (e.g., '1F', 'Back Cave').
    If there are no sub-areas, Section falls back to the enclosing header (e.g., 'Four Island (Town)').
    """
    headers = ['Pokémon', 'Games', 'Location', 'Levels', 'Rate', 'Section']
    rows_out = []

    # Helper: get wrapper title once
    title = (get_table_title(wrapper) or '').strip()
    title_lc = title.lower()

    # Find all collapsible headers inside this wrapper
    all_heads = wrapper.find_all('tr', id=re.compile(r'^collapsible-section_'))

    # Keep only sub-headers that are NOT the wrapper's own "Available Pokémon" title
    sub_heads = []
    for h in all_heads:
        th = h.find('th')
        ht = norm_text(th).strip().lower() if th else ''
        if ht and ht not in ('available pokémon', 'available pokemon'):
            sub_heads.append(h)

    if len(sub_heads) >= 1:
        # Multi-subtable mode: each sub-head (e.g., "1F", "Back Cave")
        for head in sub_heads:
            section_label = norm_text(head.find('th'))  # e.g., "1F", "Back Cave"
            hidden = head.find_next_sibling('tr')
            if not (hidden and hidden.has_attr('style') and 'display' in hidden['style']):
                continue
            inner_tbl = hidden.find('table', class_='roundy') or hidden.find('table')
            if inner_tbl:
                _emit_available_rows(inner_tbl, section_label, rows_out)
    else:
        # Single-table mode:
        # If the wrapper's title is literally "Available Pokémon", use the parent/enclosing section.
        # Otherwise (e.g., "Ruby Path, 1F") use that as the Section.
        if title and title_lc not in ('available pokémon', 'available pokemon'):
            section_label = title
        else:
            section_label = parent_section or enclosing_section_title(wrapper)

        inner_tbl = unwrap_inner_data_table(wrapper)
        if inner_tbl:
            _emit_available_rows(inner_tbl, section_label, rows_out)

    # Strip junk rows (fake header / legend)
    clean = []
    for r in rows_out:
        first = (r[0] or '').strip().lower()
        if first == 'pokémon':  # fake top header row
            continue
        if 'colored background means' in first:  # legend row
            continue
        clean.append(r)

    return {'headers': headers, 'rows': clean}




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

def enclosing_section_title(node):
    h = node.find_previous(['h6','h5','h4','h3','h2'])
    return norm_text(h) if h else ''

def is_trainers_table(tbl):
    """
    Heuristic detector for Bulbapedia 'Trainers' tables.

    Rules (in order of confidence):
    1) Wrapper/inner title contains 'Trainers' (or 'Trainer').
    2) Header THs contain 'Trainer' (common on some pages).
    3) Structural fallback: multiple centered rows with small sprites (16–32 px)
       and text containing 'Lv.' — typical of trainer party listings.
    Guard: if the element (or its inner) is a battle card, return False.
    """
    if not tbl or tbl.name != 'table':
        return False

    # Never misclassify a battle card as a trainers table
    inner = unwrap_inner_data_table(tbl) or tbl
    if is_battle_card(tbl) or (inner is not tbl and is_battle_card(inner)):
        return False

    # 1) Title check on wrapper/inner
    title_wr = (get_table_title(tbl) or '').strip().lower()
    title_in = (get_table_title(inner) or '').strip().lower()
    if ('trainer' in title_wr) or ('trainer' in title_in):
        return True
    if ('trainers' in title_wr) or ('trainers' in title_in):
        return True

    # 2) Header THs include 'Trainer'
    th_texts = [norm_text(th).lower() for th in inner.find_all('th')]
    if any('trainer' in t for t in th_texts):
        return True

    # 3) Structural fallback: many centered rows with tiny sprites + 'Lv.'
    #    (Bulbapedia trainer-party rows use 16/24/32px icons and show levels)
    centered_rows = inner.find_all('tr', attrs={'align': re.compile(r'^\s*center\s*$', re.I)})
    hits = 0
    for tr in centered_rows:
        if tr.find('img', attrs={'width': re.compile(r'^(16|24|32)$')}) or \
           tr.find('img', attrs={'height': re.compile(r'^(16|24|32)$')}):
            txt = tr.get_text(' ', strip=True).lower()
            if 'lv.' in txt or 'lv' in txt:
                hits += 1
        # quick exit if we’ve seen enough signal
        if hits >= 3:
            return True
    return False

def is_expandable_battle_card(wrapper):
    if not (wrapper and wrapper.name == 'table'):
        return False
    clz = wrapper.get('class') or []
    if 'expandable' not in clz:
        return False

    # Must have the collapsible header row and a hidden details row right after
    head = wrapper.find('tr', id=re.compile(r'^collapsible-section_\d+'))
    if not head:
        return False
    hidden = head.find_next_sibling('tr')
    if not (hidden and hidden.has_attr('style') and 'display' in hidden['style']):
        return False

    # Must have a Reward: label somewhere in the wrapper
    has_reward = wrapper.find(lambda t: t.name in ('td','th') and 'Reward:' in t.get_text())
    if not has_reward:
        return False

    # Must show pokéball strip (full/empty balls)
    has_balls = wrapper.find('img', src=BATTLE_BALL_RE)
    if not has_balls:
        return False

    return True

def looks_like_trainers_table(tbl: Tag) -> bool:
    """
    Simple heuristic: Bulbapedia trainer tables usually have
    'Trainers' in the wrapper/caption title or 'Trainer' in header text.
    """
    # Table caption/title
    caption = tbl.find('caption')
    wrapper_title = caption.get_text(strip=True) if caption else ''

    # Header row text
    header_cells = [th.get_text(" ", strip=True) for th in tbl.find_all("th")]
    header_text = " ".join(header_cells)

    return ('Trainers' in wrapper_title) or ('Trainer' in header_text)


def parse_content(soup, image_dir='images'):
    content_root = soup.find(id='mw-content-text')
    main = content_root.find(class_='mw-parser-output')
    section_title = ""

    sections = []
    stack = [{'level': 1, 'node_list': sections}]
    processed_tables = set()

    def mark_consumed(tbl, *, include_inner=True, deep=False):
        processed_tables.add(id(tbl))
        if include_inner:
            inner = unwrap_inner_data_table(tbl)
            if inner and inner is not tbl:
                processed_tables.add(id(inner))
        if deep:
            for t in tbl.find_all('table'):
                processed_tables.add(id(t))

    def looks_like_trainers_table(tbl):
        inner = unwrap_inner_data_table(tbl)
        wrapper_title = get_table_title(tbl) if 'get_table_title' in globals() else ''
        # Use ALL inner text (not just THs)
        header_text = inner.get_text(" ", strip=True)
        if 'Trainers' in wrapper_title:
            return True
        if re.search(r'\bTrainer(s)?\b', header_text, re.I):
            return True
        # Heuristic: many trainers tables have multiple small sprite rows + “Reward:”
        if inner.find(string=re.compile(r'\bReward:\b', re.I)) and inner.find('img', attrs={'width': '64', 'height': '64'}):
            return True
        return False

    def handle_table(tbl):
        if id(tbl) in processed_tables:
            return

        # 0) If this very table is a battle card wrapper, do it first
        if is_expandable_battle_card(tbl):
            stack[-1]['node_list'].append(parse_battle_card(tbl))
            mark_consumed(tbl, include_inner=True, deep=True)
            return

        inner = unwrap_inner_data_table(tbl)
        wrapper_title = get_table_title(tbl) if 'get_table_title' in globals() else ''
        # use full text for trainer detection; some pages don't put it in <th>
        full_text = inner.get_text(" ", strip=True)

        # 1) Trainers table
        if looks_like_trainers_table(tbl):
            stack[-1]['node_list'].append({
                'type': 'table',
                'data': parse_trainers_table(tbl)  # parser already unwraps
            })
            # Swallow so we don't fall through to generic duplicates
            mark_consumed(tbl, include_inner=True, deep=False)

            # …but still extract any embedded battle cards (Giovanni, Rival, etc.)
            for exp in tbl.find_all('table', class_='expandable'):
                if id(exp) in processed_tables:
                    continue
                if is_expandable_battle_card(exp):
                    stack[-1]['node_list'].append(parse_battle_card(exp))
                    mark_consumed(exp, include_inner=True, deep=True)
            mark_consumed(tbl, include_inner=True, deep=True)
            return

        # 2) Available Pokémon
        if ('Available Pokémon' in wrapper_title) or ('Available Pokémon' in full_text):
            stack[-1]['node_list'].append({
                'type': 'Available Pokémon',
                'data': parse_available_pokemon_table(tbl, section_title)
            })
            # These wrappers contain many subtables; mark deep to avoid generic dupes
            mark_consumed(tbl, include_inner=True, deep=True)
            return

        # 3) Before generic fallback, look for nested interesting tables
        #    (battle cards embedded in layout tables, etc.)
        for child in tbl.find_all('table'):
            if id(child) in processed_tables:
                continue
            if is_expandable_battle_card(child):
                stack[-1]['node_list'].append(parse_battle_card(child))
                mark_consumed(child, include_inner=True, deep=True)

        # 4) Generic fallback for the current wrapper (don’t deep-mark so nested
        #    generic tables can still be picked up if needed)
        if id(tbl) not in processed_tables:
            stack[-1]['node_list'].append({'type': 'table', 'data': parse_table(inner)})
            mark_consumed(tbl, include_inner=True, deep=False)

        # 5) Finally, walk immediate child tables (and deeper) to catch any remaining
        #    non-special generic tables without duplicating already-processed ones.
        for child in tbl.find_all('table'):
            if id(child) not in processed_tables:
                handle_table(child)

    # Walk in DOM order; for non-table containers, only process their immediate child tables
    for el in main.children:
        if not isinstance(el, Tag):
            continue

        # headings (keep order exact as page)
        if el.name and len(el.name) == 2 and el.name.startswith('h') and el.name[1].isdigit():
            level = int(el.name[1])
            title = el.get_text(' ', strip=True)
            section_title = title
            node = {'type': 'section', 'title': title, 'level': level,
                    'metadata': {'id': el.get('id'), 'class': el.get('class', [])},
                    'content': []}
            while stack and stack[-1]['level'] >= level:
                stack.pop()
            stack[-1]['node_list'].append(node)
            stack.append({'level': level, 'node_list': node['content']})
            continue

        # paragraph
        if el.name == 'p':
            for br in el.find_all('br'):
                br.replace_with(' ')
            text = el.get_text(' ', strip=True)
            if text:
                stack[-1]['node_list'].append({'type': 'paragraph', 'text': text})
            continue

        # lists
        if el.name in ('ul', 'ol'):
            items = []
            for li in el.find_all('li', recursive=False):
                for br in li.find_all('br'):
                    br.replace_with(' ')
                t = li.get_text(' ', strip=True)
                if t:
                    items.append(t)
            stack[-1]['node_list'].append({'type': 'list', 'ordered': (el.name == 'ol'), 'items': items})
            continue

        # figure/image
        if el.name == 'figure':
            img = el.find('img')
            if img and img.get('src'):
                src = img.get('src')
                cap = el.find('figcaption')
                caption = cap.get_text(' ', strip=True) if cap else None
                stack[-1]['node_list'].append({'type': 'image', 'src': src, 'local_path': None, 'caption': caption})
            continue

        # tables
        if el.name == 'table':
            handle_table(el)
        else:
            # non-table container: only immediate child tables
            for tbl in el.find_all('table', recursive=False):
                handle_table(tbl)

    return sections


def html_to_json(html_file, json_file, image_dir='images'):
    """Load HTML, parse it, and write out nested JSON."""
    with open(html_file, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'lxml')
    parsed = parse_content(soup, image_dir)
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Parse FireRed/LeafGreen HTML to nested JSON for RAG')
    parser.add_argument('html_file', help='Input HTML file')
    parser.add_argument('json_file', help='Output JSON file')
    parser.add_argument('--image-dir', default='images', help='Directory to save images')
    args = parser.parse_args()
    html_to_json(args.html_file, args.json_file, args.image_dir)
