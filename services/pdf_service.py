"""PDF extraction and vocabulary parsing."""
import json
import os
import re

import pymupdf as fitz

def extract_clean_cell_text(page, rect) -> str:
    blocks = page.get_text("dict", clip=rect)["blocks"]
    spans = []
    for b in blocks:
        if "lines" in b:
            for l in b["lines"]:
                for s in l["spans"]:
                    font_name = s["font"]
                    if "165" in font_name or "166" in font_name or "GERMANONLINETESTS" in s["text"].upper() or "GERMAN ONLINE TESTS" in s["text"].upper():
                        continue
                    if s["size"] < 8.0:
                        continue
                    if abs(s["size"] - 9.75) < 0.1:
                        continue
                    spans.append(s)
                    
    if not spans:
        return ""
        
    spans.sort(key=lambda x: x["bbox"][1])
    
    lines = []
    current_line = []
    current_y_limit = None
    
    for s in spans:
        y_top = s["bbox"][1]
        y_bottom = s["bbox"][3]
        
        if current_y_limit is None or y_top > current_y_limit:
            if current_line:
                current_line.sort(key=lambda x: x["bbox"][0])
                lines.append(current_line)
            current_line = [s]
            current_y_limit = y_bottom - 2
        else:
            current_line.append(s)
            
    if current_line:
        current_line.sort(key=lambda x: x["bbox"][0])
        lines.append(current_line)
        
    line_texts = []
    for line in lines:
        text = "".join([s["text"] for s in line]).strip()
        text = " ".join(text.split())
        if text:
            line_texts.append(text)
            
    return "\n".join(line_texts)

def get_noun_info(german_word, wtype):
    if "noun" in wtype.lower():
        parts = german_word.split()
        if len(parts) >= 2:
            article = parts[0].strip()
            article = re.sub(r'[^a-zA-Z]', '', article)
            base_word = parts[1].split(',')[0].strip()
            base_word = re.sub(r'[^a-zA-ZäöüÄÖÜß]', '', base_word)
            return article, base_word
    return None, german_word

def highlight_german_sentence(sentence, target_word, wtype):
    article, base = get_noun_info(target_word, wtype)
    if "verb" in wtype.lower():
        if target_word.endswith("en"):
            stem = target_word[:-2]
        elif target_word.endswith("n"):
            stem = target_word[:-1]
        else:
            stem = target_word
    else:
        stem = base
        
    words = re.findall(r'[a-zA-ZäöüÄÖÜß]+', sentence)
    matched_word = None
    for w in words:
        w_clean = w.lower()
        if len(w_clean) >= 3 and stem.lower() in w_clean:
            matched_word = w
            break
        elif len(w_clean) < 3 and stem.lower() == w_clean:
            matched_word = w
            break
            
    if matched_word:
        return re.sub(r'\b' + re.escape(matched_word) + r'\b', f"**{matched_word}**", sentence), article
    return sentence, article

def highlight_english_sentence(sentence, target_english):
    eng = target_english.strip()
    if eng.lower().startswith("to "):
        eng = eng[3:].strip()
    
    syns = [s.strip() for s in re.split(r'[/,]', eng) if s.strip()]
    words = re.findall(r'[a-zA-Z]+', sentence)
    matched_word = None
    for w in words:
        w_clean = w.lower()
        if len(w_clean) < 2:
            continue
        for s in syns:
            s_clean = s.lower()
            if len(s_clean) >= 4 and s_clean.endswith('e'):
                s_stem = s_clean[:-1]
            else:
                s_stem = s_clean
                
            if len(s_stem) < 3:
                if s_stem == w_clean:
                    matched_word = w
                    break
            else:
                if s_stem in w_clean:
                    matched_word = w
                    break
        if matched_word:
            break
            
    if matched_word:
        return re.sub(r'\b' + re.escape(matched_word) + r'\b', f"**{matched_word}**", sentence)
    return sentence

DIRECT_CARDS_MARKER = "<!-- DIRECT_CARDS_JSON_LINES -->"
WORD_TYPES = {
    "noun", "verb", "phrase", "adjective", "adverb", "preposition", "conjunction",
    "particle", "determiner", "pronoun", "interjection", "prefix", "suffix",
    "numeral", "article",
}
WORD_TYPE_ALIASES = {
    "n": "noun", "n.": "noun",
    "v": "verb", "v.": "verb",
    "adj": "adjective", "adj.": "adjective",
    "adv": "adverb", "adv.": "adverb",
    "phr": "phrase", "phr.": "phrase",
    "prep": "preposition", "prep.": "preposition",
    "conj": "conjunction", "conj.": "conjunction",
}
EMBEDDED_VOCAB_RE = re.compile(
    r"([\wäöüÄÖÜß-]+(?:\s+[\wäöüÄÖÜß-]+)?)\s+"
    r"(adj\.|adv\.|noun|verb|phrase)\.?\s+"
    r"([^—\n]+?)—\s*"
    r"([^.]+\.)\s*"
    r"([A-Z][^.]+\.)",
    re.UNICODE,
)
COLUMN_HEADERS = {
    "german_word": ("german word",),
    "word_type": ("type", "word type"),
    "english_gloss": ("english",),
    "partizip": ("partizip ii", "partizip"),
    "german_sentence": ("german sentence", "satz (de)", "beispielsatz"),
    "english_sentence": ("english sentence", "satz (en)", "translation sentence"),
}

def strip_bold(text: str) -> str:
    return text.replace("**", "").strip()

def format_direct_cards_markdown(cards: list[dict]) -> str:
    lines = [DIRECT_CARDS_MARKER]
    for card in cards:
        lines.append(json.dumps(card, ensure_ascii=False))
    return "\n".join(lines)

def build_direct_card(g_word: str, w_type: str, e_gloss: str, g_sent: str, e_sent: str, level: str) -> dict:
    g_word = " ".join(g_word.split())
    e_gloss = " ".join(e_gloss.split())
    g_sent = " ".join(g_sent.split())
    e_sent = " ".join(e_sent.split())
    if not g_word.startswith("**"):
        g_word = f"**{g_word}**"
    if not e_gloss.startswith("**"):
        e_gloss = f"**{e_gloss}**"
    highlighted_g, _ = highlight_german_sentence(g_sent, strip_bold(g_word), w_type)
    highlighted_e = highlight_english_sentence(e_sent, strip_bold(e_gloss))
    return {
        "german_word": g_word,
        "english_gloss": e_gloss,
        "level": level,
        "english_sentence": highlighted_e,
        "german_sentence": highlighted_g,
    }

def match_column_header(text: str) -> str | None:
    t = text.strip().lower()
    for field, aliases in COLUMN_HEADERS.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if t == alias:
                return field
    return None

def _flatten_vocab_lines(text: str) -> list[str]:
    lines = []
    for para in text.split("\n\n"):
        for line in para.split("\n"):
            line = line.strip()
            if line:
                lines.append(line)
    return lines

def _is_partizip_marker(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s in {"—", "–", "-", "−", "‐", "‑", "‒", "―"}:
        return True
    # ponytail: PDF dashes often degrade to replacement chars or punctuation-only tokens
    if len(s) <= 2 and not re.search(r"[a-zA-ZäöüÄÖÜ]", s):
        return True
    return False

def _looks_like_german_sentence(line: str) -> bool:
    s = line.strip()
    if re.search(r"[äöüßÄÖÜ]", s):
        return True
    starters = (
        "Ich ", "Wir ", "Der ", "Die ", "Das ", "Ein ", "Eine ", "Er ", "Sie ", "Es ",
        "Kannst ", "Können ", "Bitte ", "Geben ", "Bevor ", "Unsere ", "Frau ", "Man ",
        "Nach ", "Bei ", "Wenn ", "Hier ", "Am ", "Im ", "Zum ", "Zur ", "Du ", "Diese ",
        "Dieser ", "Dieses ", "In ", "Als ", "Obwohl ", "Während ", "Sobald ", "Falls ",
    )
    return any(s.startswith(p) for p in starters)

def _looks_like_english_sentence(line: str) -> bool:
    s = line.strip()
    if re.search(r"[äöüßÄÖÜ]", s):
        return False
    if _looks_like_german_sentence(s):
        return False
    starters = (
        "I ", "We ", "The ", "A ", "An ", "He ", "She ", "It ", "They ", "You ", "Our ",
        "Please ", "Can ", "Before ", "Ms ", "Mr ", "This ", "That ", "In ", "At ", "On ",
        "Workplace ", "Area ", "Responsibility ", "Meeting ", "German ", "English ",
        "Professional ", "Vocational ", "Further ", "Equal ", "University ", "Activity ",
        "Assignment ", "Coordination ", "Participant ", "Attendance ", "Feedback ",
    )
    if any(s.startswith(p) for p in starters):
        return True
    return bool(re.search(
        r"\b(the|is|are|in|this|for|to|and|of|with|at|on|important|context|today|before|after|have|has|will|should|would|could|been|being)\b",
        s,
        re.I,
    ))

def _normalize_word_type(line: str) -> str | None:
    low = line.strip().lower().rstrip(".")
    if low in WORD_TYPES:
        return low
    if low in WORD_TYPE_ALIASES:
        return WORD_TYPE_ALIASES[low]
    if "adj" in low and "adv" in low:
        return "adjective"
    return None

def _is_german_word_fragment(line: str) -> bool:
    s = line.strip().strip('",')
    if re.fullmatch(r"-?[ensr]{1,3}", s, re.I):
        return True
    return s in {"-e", "-en", "-er", "-n", "-s"}

def _truncate_grammar_intrusion(text: str) -> str:
    cut = len(text)
    for marker in (" Grammar:", " grammar:", " adj.", " adv.", " Hauptsatz", " Nebensatz", " Wortstellung"):
        idx = text.find(marker)
        if 0 < idx < cut:
            cut = idx
    return text[:cut].strip() if cut < len(text) else text.strip()

def _extract_embedded_vocab_cards(text: str, level: str) -> list[dict]:
    cards = []
    for match in EMBEDDED_VOCAB_RE.finditer(text):
        g_word, w_type, e_gloss, g_sent, e_sent = match.groups()
        w_type = _normalize_word_type(w_type) or "adjective"
        cards.append(build_direct_card(
            g_word.strip(), w_type, e_gloss.strip(), g_sent.strip(), e_sent.strip(), level
        ))
    return cards

def _is_valid_german_word(g_word: str) -> bool:
    clean = strip_bold(g_word).strip('",')
    if _is_german_word_fragment(clean):
        return False
    return len(clean) >= 2

def repair_extracted_cards(cards: list[dict], level: str) -> list[dict]:
    repaired = []
    seen = set()
    for card in cards:
        extras = _extract_embedded_vocab_cards(card.get("english_sentence", ""), level)
        trimmed = _truncate_grammar_intrusion(card.get("english_sentence", ""))
        main = {**card, "english_sentence": trimmed}
        for item in [main, *extras]:
            if not _is_valid_german_word(item.get("german_word", "")):
                continue
            key = (item.get("german_word"), item.get("german_sentence"))
            if key in seen:
                continue
            seen.add(key)
            repaired.append(item)
    return repaired

def parse_markdown_cards(content: str, level: str = "B1") -> list[dict]:
    if content.startswith(DIRECT_CARDS_MARKER):
        cards = []
        for line in content.splitlines()[1:]:
            if line.strip():
                try:
                    cards.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return repair_extracted_cards(cards, level)
    return repair_extracted_cards(parse_vocabulary_text(content, level), level)

def _is_particip_line(line: str, w_type: str) -> bool:
    if _is_partizip_marker(line):
        return True
    if w_type.lower() not in {"verb", "phrase"}:
        return False
    return _looks_like_german_sentence(line) and not _looks_like_english_sentence(line) and len(line.split()) <= 3

def parse_vocabulary_text(text: str, level: str = "B1") -> list[dict]:
    lines = _flatten_vocab_lines(text)
    header = {"german word", "type", "english", "partizip ii", "german sentence", "english sentence"}
    skip_prefixes = ("Set ", "Day ", "How to use", "Scope:", "Important:", "Grammar", "telc ", "Teil ")
    cards = []
    i = 0
    while i < len(lines) - 1:
        line = lines[i]
        low = line.lower()
        if low in header or line.isdigit() or any(line.startswith(p) for p in skip_prefixes):
            i += 1
            continue
        w_type = _normalize_word_type(lines[i + 1])
        if w_type is None:
            i += 1
            continue

        g_word = lines[i]
        if _is_german_word_fragment(g_word) and i > 0:
            prev = lines[i - 1]
            if _normalize_word_type(prev) is None and prev.lower() not in header:
                g_word = prev.rstrip('",') + " " + g_word.lstrip('",')
        i += 2

        gloss_parts = []
        while i < len(lines):
            if _is_particip_line(lines[i], w_type):
                i += 1
                if (
                    i < len(lines)
                    and w_type.lower() in {"verb", "phrase"}
                    and _looks_like_german_sentence(lines[i])
                    and not _looks_like_english_sentence(lines[i])
                    and len(lines[i].split()) <= 2
                ):
                    i += 1
                break
            if _looks_like_german_sentence(lines[i]):
                break
            gloss_parts.append(lines[i])
            i += 1

        if i < len(lines) and _is_partizip_marker(lines[i]):
            i += 1
            if (
                i < len(lines)
                and w_type.lower() in {"verb", "phrase"}
                and _looks_like_german_sentence(lines[i])
                and not _looks_like_english_sentence(lines[i])
                and len(lines[i].split()) <= 2
            ):
                i += 1

        g_sent_parts = []
        while i < len(lines):
            if _looks_like_english_sentence(lines[i]):
                break
            if i + 1 < len(lines) and _normalize_word_type(lines[i + 1]) is not None:
                break
            if lines[i].startswith("Set ") or lines[i].lower() in header:
                break
            g_sent_parts.append(lines[i])
            i += 1

        e_sent_parts = []
        while i < len(lines):
            if i + 1 < len(lines) and _normalize_word_type(lines[i + 1]) is not None:
                break
            if lines[i].startswith("Set ") or lines[i].lower() in header:
                break
            chunk = lines[i]
            if any(marker in chunk for marker in (" Grammar:", " grammar:", " adj.", " adv.")):
                chunk = _truncate_grammar_intrusion(chunk)
                if chunk:
                    e_sent_parts.append(chunk)
                break
            e_sent_parts.append(chunk)
            i += 1

        e_gloss = " ".join(gloss_parts)
        g_sent = " ".join(g_sent_parts)
        e_sent = " ".join(e_sent_parts)
        if g_word and e_gloss and g_sent and e_sent and _is_valid_german_word(g_word):
            cards.append(build_direct_card(g_word, w_type, e_gloss, g_sent, e_sent, level))
    return repair_extracted_cards(cards, level)

def _merge_column_cells(blocks: list[tuple[float, str]], y_gap: float = 10.0) -> list[str]:
    if not blocks:
        return []
    cells = []
    current = [blocks[0][1]]
    last_y = blocks[0][0]
    for y, text in blocks[1:]:
        if y - last_y > y_gap:
            cells.append("\n".join(current))
            current = [text]
        else:
            current.append(text)
        last_y = y
    cells.append("\n".join(current))
    return cells

def _nearest_column(x_center: float, headers: dict[str, float]) -> str:
    return min(headers, key=lambda field: abs(headers[field] - x_center))

def extract_column_layout_cards(doc, level: str = "B1") -> list[dict]:
    cards = []
    for page in doc:
        blocks = page.get_text("blocks")
        if not blocks:
            continue

        headers = {}
        header_y = 0.0
        for b in blocks:
            x0, y0, x1, y1, text, *_ = b
            field = match_column_header(text)
            if field:
                headers[field] = (x0 + x1) / 2
                header_y = max(header_y, y1)

        required = {"german_word", "english_gloss", "german_sentence", "english_sentence"}
        if not required.issubset(headers):
            continue

        col_blocks = {field: [] for field in headers}
        for b in blocks:
            x0, y0, x1, y1, text, *_ = b
            if y0 < header_y - 2:
                continue
            t = text.strip()
            if not t or match_column_header(t):
                continue
            field = _nearest_column((x0 + x1) / 2, headers)
            col_blocks[field].append((y0, t))

        columns = {field: _merge_column_cells(col_blocks[field]) for field in headers}
        row_count = len(columns.get("german_word", []))
        if row_count == 0:
            continue

        for idx in range(row_count):
            try:
                g_word = columns["german_word"][idx]
                e_gloss = columns["english_gloss"][idx]
                g_sent = columns["german_sentence"][idx]
                e_sent = columns["english_sentence"][idx]
                w_type = columns.get("word_type", ["noun"] * row_count)[idx] if "word_type" in columns else "noun"
            except IndexError:
                continue
            if not all([g_word, e_gloss, g_sent, e_sent]) or not _is_valid_german_word(g_word):
                continue
            cards.append(build_direct_card(g_word, w_type, e_gloss, g_sent, e_sent, level))
    return repair_extracted_cards(cards, level)

def validate_direct_cards(cards: list[dict]) -> bool:
    if not cards or len(cards) < 5:
        return False
        
    invalid_count = 0
    # Hyphenated/cut-off keywords seen in truncated PDF column rendering
    truncated_words = {"applica", "increas", "develo", "relatio", "emplo", "entran", "qualific", "prob", "unive", "vocati", "trainin", "opport", "impo", "sala", "prom"}

    for c in cards:
        e_sent = c.get("english_sentence", "")
        g_sent = c.get("german_sentence", "")
        e_gloss = c.get("english_gloss", "")
        g_word = c.get("german_word", "")

        if not all([e_sent, g_sent, e_gloss, g_word]):
            invalid_count += 1
            continue

        e_sent_clean = e_sent.replace("**", "").strip()
        g_sent_clean = g_sent.replace("**", "").strip()

        e_words = e_sent_clean.split()
        g_words = g_sent_clean.split()

        if len(e_words) < 4 or len(g_words) < 4:
            invalid_count += 1
            continue

        sent_words = {w.lower() for w in re.findall(r"[a-zA-Z]+", e_sent_clean)}
        if sent_words & truncated_words:
            invalid_count += 1
            continue
            
    failure_rate = invalid_count / len(cards)
    if failure_rate > 0.10:
        return False
    return True

def extract_clean_markdown(pdf_path: str, level: str = "B1") -> str:
    doc = fitz.open(pdf_path)
    pdf_filename = os.path.basename(pdf_path).lower()
    if "b2" in pdf_filename:
        level = "B2"
    elif "b1" in pdf_filename:
        level = "B1"

    # Try dynamic multi-column table extraction first
    direct_cards = []

    for page_idx, page in enumerate(doc):
        tables = page.find_tables()
        if not tables or not tables.tables:
            continue

        for table in tables.tables:
            header_row = None
            col_mapping = {}

            # Look at first 2 rows to locate column headers dynamically
            for r_idx in range(min(2, len(table.rows))):
                row_cells = []
                for cell in table.rows[r_idx].cells:
                    if cell:
                        text = extract_clean_cell_text(page, cell).strip().lower()
                        row_cells.append(text)
                    else:
                        row_cells.append("")

                is_header = False
                for cell_text in row_cells:
                    if any(h in cell_text for h in ["german", "deutsch", "wort", "vocabulary", "english", "meaning", "gloss", "sentence", "beispiel", "art", "type"]):
                        is_header = True
                        break

                if is_header:
                    header_row = r_idx
                    for c_idx, cell_text in enumerate(row_cells):
                        if any(h in cell_text for h in ["german word", "wort", "deutsch", "vocabulary"]):
                            if "sentence" not in cell_text and "satz" not in cell_text:
                                col_mapping["german_word"] = c_idx
                        elif any(h in cell_text for h in ["english gloss", "meaning", "translation", "bedeutung", "english"]):
                            if "sentence" not in cell_text and "satz" not in cell_text:
                                col_mapping["english_gloss"] = c_idx
                        elif any(h in cell_text for h in ["word type", "type", "art", "wortart"]):
                            col_mapping["word_type"] = c_idx
                        elif any(h in cell_text for h in ["german sentence", "beispielsatz", "satz (de)", "satz de", "beispiel"]):
                            col_mapping["german_sentence"] = c_idx
                        elif any(h in cell_text for h in ["english sentence", "translation sentence", "satz (en)", "satz en"]):
                            col_mapping["english_sentence"] = c_idx
                    break

            # If we mapped the essential keys, perform extraction
            if header_row is not None and "german_word" in col_mapping and "english_gloss" in col_mapping and "german_sentence" in col_mapping and "english_sentence" in col_mapping:
                start_r = header_row + 1
                for r_idx in range(start_r, len(table.rows)):
                    row = table.rows[r_idx]
                    cells_text = []
                    for cell in row.cells:
                        if cell:
                            text = extract_clean_cell_text(page, cell).strip()
                            cells_text.append(text)
                        else:
                            cells_text.append("")

                    try:
                        g_word = cells_text[col_mapping["german_word"]].strip()
                        e_gloss = cells_text[col_mapping["english_gloss"]].strip()
                        w_type = cells_text[col_mapping["word_type"]].strip() if "word_type" in col_mapping and col_mapping["word_type"] < len(cells_text) else ""
                        g_sent = cells_text[col_mapping["german_sentence"]].strip()
                        e_sent = cells_text[col_mapping["english_sentence"]].strip()

                        if not g_word or not g_sent or not e_sent:
                            continue

                        direct_cards.append(build_direct_card(g_word, w_type, e_gloss, g_sent, e_sent, level))
                    except Exception:
                        continue

    # Check if cards are complete and high quality
    if validate_direct_cards(direct_cards):
        return format_direct_cards_markdown(direct_cards)

    # Side-by-side column layout (telc-style vocabulary lists)
    column_cards = extract_column_layout_cards(doc, level)
    if validate_direct_cards(column_cards):
        return format_direct_cards_markdown(column_cards)

    # Fallback: extract text blocks, then parse the repeating vocabulary field pattern
    markdown_lines = []
    for page_idx, page in enumerate(doc):
        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (round(b[1], 1), b[0]))
        for b in blocks:
            text = b[4].strip()
            if text:
                markdown_lines.append(text)

    raw_text = "\n\n".join(markdown_lines)
    parsed_cards = parse_vocabulary_text(raw_text, level)
    if validate_direct_cards(parsed_cards):
        return format_direct_cards_markdown(parsed_cards)

    return raw_text
