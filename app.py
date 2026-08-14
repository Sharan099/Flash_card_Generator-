import os
import sqlite3
import datetime
import time
import json
import re
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
import pymupdf as fitz
import llm

DB_PATH = "data/lingo.db"
os.makedirs("data/markdown", exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def purge_orphan_cards(conn=None) -> int:
    own = conn is None
    if own:
        conn = get_db()
    deleted = conn.execute(
        "DELETE FROM cards WHERE doc_id NOT IN (SELECT id FROM documents)"
    ).rowcount
    if own:
        conn.commit()
        conn.close()
    return deleted

# ponytail: simple database initialization on startup
def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        markdown_path TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
        level TEXT NOT NULL,
        german_word TEXT NOT NULL,
        english_gloss TEXT NOT NULL,
        english_sentence TEXT NOT NULL,
        german_sentence TEXT NOT NULL,
        box INTEGER DEFAULT 1,
        due_date TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cursor.execute("DROP INDEX IF EXISTS idx_doc_word")
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_card
        ON cards(doc_id, german_word, german_sentence)
    """)
    purge_orphan_cards(conn)
    conn.commit()
    conn.close()

init_db()

app = FastAPI(title="Lingo Local")

SYSTEM_PROMPT = (
    "You are a German language teacher creating vocabulary flashcards. "
    "Return ONLY a JSON array. No prose, no markdown fences. "
    "All double quotes inside string values must be properly escaped as \\\" or replaced with single quotes. "
    "Ensure there are no trailing commas or mismatched braces."
)

USER_PROMPT_TEMPLATE = """From the German vocabulary list below, extract ALL vocabulary items. For each item, produce ONE flashcard as a JSON object:
{{
  "german_word":      "the dictionary form (lemma). For nouns: article + noun + plural, e.g., '**die Entscheidung, -en**'. For verbs: infinitive + principal parts, e.g., '**treffen (traf, hat getroffen)**'. Wrap it in markdown bold.",
  "english_gloss":    "2-5 word English meaning, e.g., '**decision**'. Wrap it in markdown bold.",
  "level":            "one of A1 A2 B1 B2 C1 C2",
  "english_sentence": "A natural, everyday English sentence. Wrap the translated target English word or phrase in markdown bold, e.g., 'We have to make a **decision** by Friday.' Do NOT include the German target word or brackets in this sentence.",
  "german_sentence":  "The same sentence in natural German, using the target word correctly inflected. Wrap the inflected German target word in markdown bold, e.g., 'Wir müssen bis Freitag eine **Entscheidung** treffen.'"
}}

Rules:
- The two sentences must be translations of each other, same meaning, same register.
- Sentences must be everyday and usable, not textbook-abstract. 8-16 words.
- Do NOT include the German target word in brackets in the english_sentence.
- Use only characters from the German alphabet including ä ö ü ß.
- Output a JSON array of objects. Nothing else.
- NEVER use unescaped double quotes inside the JSON string values. For example, use '**das Amt, \\"-er**' or '**das Amt, \\'-er**', NEVER '**das Amt, "-er**'.
- Ensure the JSON is completely valid and parses correctly.

TEXT:
{chunk}"""
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

def insert_cards_for_doc(cursor, doc_id: int, items: list[dict], now_str: str) -> tuple[int, int]:
    added = skipped = 0
    due_date = datetime.date.today().isoformat()
    for item in items:
        g_word = item.get("german_word")
        e_gloss = item.get("english_gloss")
        c_level = item.get("level", "B1")
        e_sent = item.get("english_sentence")
        g_sent = item.get("german_sentence")
        if not all([g_word, e_gloss, c_level, e_sent, g_sent]):
            skipped += 1
            continue
        cursor.execute(
            """
            INSERT OR IGNORE INTO cards
            (doc_id, level, german_word, english_gloss, english_sentence, german_sentence, box, due_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (doc_id, c_level, g_word, e_gloss, e_sent, g_sent, due_date, now_str),
        )
        if cursor.rowcount > 0:
            added += 1
        else:
            skipped += 1
    return added, skipped

def resync_cards_from_markdown(doc_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT markdown_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row or not row[0] or not os.path.exists(row[0]):
        conn.close()
        raise HTTPException(status_code=404, detail="Markdown file not found.")
    with open(row[0], "r", encoding="utf-8") as f:
        content = f.read()
    level = "B2" if "b2" in content.lower()[:500] else "B1"
    items = parse_markdown_cards(content, level)
    now_str = datetime.datetime.utcnow().isoformat()
    with open(row[0], "w", encoding="utf-8") as f:
        f.write(format_direct_cards_markdown(items))
    conn.execute("DELETE FROM cards WHERE doc_id = ?", (doc_id,))
    added, skipped = insert_cards_for_doc(conn.cursor(), doc_id, items, now_str)
    conn.commit()
    conn.close()
    return {"markdown_cards": len(items), "cards_added": added, "cards_skipped": skipped}

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

class ReviewRequest(BaseModel):
    card_id: int
    correct: bool

def parse_llm_json(text: str) -> list[dict]:
    import ast
    import re
    text_clean = text.strip()
    if text_clean.startswith("```"):
        lines = text_clean.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text_clean = "\n".join(lines).strip()
    
    start = text_clean.find("[")
    end = text_clean.rfind("]")
    if start != -1 and end != -1 and end > start:
        text_clean = text_clean[start:end+1]
        
    try:
        return json.loads(text_clean)
    except Exception:
        try:
            val = ast.literal_eval(text_clean)
            if isinstance(val, list):
                return val
        except Exception:
            pass
            
        # Regex line-by-line fallback parser for unescaped double quotes in values
        cards = []
        card_blocks = re.findall(r'\{[^{}]*\}', text)
        for block in card_blocks:
            card = {}
            lines = block.splitlines()
            for line in lines:
                for key in ["german_word", "english_gloss", "level", "english_sentence", "german_sentence"]:
                    match = re.search(r'"' + key + r'"\s*:\s*"(.*)"\s*,?\s*$', line.strip())
                    if match:
                        card[key] = match.group(1).strip()
            if len(card) >= 4:
                cards.append(card)
                
        if cards:
            return cards
        raise

def chunk_markdown(text: str, max_chars: int = 10000) -> list[str]:
    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = []
    current_len = 0
    for para in paragraphs:
        para_len = len(para) + 2
        if current_len + para_len > max_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
        current_chunk.append(para)
        current_len += para_len
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))
    return chunks

@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), level: str = "B1", n: int = 5):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed.")
        
    # Auto detect default level from filename
    if "b2" in file.filename.lower():
        level = "B2"
    elif "b1" in file.filename.lower():
        level = "B1"
    
    # Read the file to check magic bytes and size
    content = await file.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds 25 MB limit.")
    
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Invalid PDF file format.")

    def generate_progress():
        now_str = datetime.datetime.utcnow().isoformat()
        
        # Save to DB first to get doc_id
        conn = get_db()
        cursor = conn.cursor()
        
        # Reset existing document with same filename to clean progress & avoid duplicates!
        cursor.execute("SELECT id, markdown_path FROM documents WHERE filename = ?", (file.filename,))
        existing_doc = cursor.fetchone()
        if existing_doc:
            existing_doc_id = existing_doc[0]
            existing_md = existing_doc[1]
            cursor.execute("DELETE FROM cards WHERE doc_id = ?", (existing_doc_id,))
            cursor.execute("DELETE FROM documents WHERE id = ?", (existing_doc_id,))
            if existing_md and os.path.exists(existing_md):
                try:
                    os.remove(existing_md)
                except Exception:
                    pass
                    
        cursor.execute(
            "INSERT INTO documents (filename, markdown_path, created_at) VALUES (?, ?, ?)",
            (file.filename, "", now_str)
        )
        doc_id = cursor.lastrowid
        markdown_path = f"data/markdown/{doc_id}.md"
        cursor.execute(
            "UPDATE documents SET markdown_path = ? WHERE id = ?",
            (markdown_path, doc_id)
        )
        conn.commit()
        conn.close()

        # Save binary file temporarily to extract text
        temp_pdf_path = f"data/temp_{doc_id}.pdf"
        with open(temp_pdf_path, "wb") as f:
            f.write(content)

        yield json.dumps({"status": "converting", "message": "Converting PDF to Markdown..."}) + "\n"

        try:
            markdown_content = extract_clean_markdown(temp_pdf_path, level=level)
            with open(markdown_path, "w", encoding="utf-8") as f:
                f.write(markdown_content)
        except Exception as e:
            try:
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
            except Exception:
                pass
            yield json.dumps({"status": "error", "message": f"PDF conversion failed: {str(e)}"}) + "\n"
            return
        finally:
            try:
                if os.path.exists(temp_pdf_path):
                    os.remove(temp_pdf_path)
            except Exception:
                pass

        if not markdown_content.startswith(DIRECT_CARDS_MARKER):
            parsed_cards = parse_vocabulary_text(markdown_content, level)
            if validate_direct_cards(parsed_cards):
                markdown_content = format_direct_cards_markdown(parsed_cards)
                with open(markdown_path, "w", encoding="utf-8") as f:
                    f.write(markdown_content)

        if markdown_content.startswith(DIRECT_CARDS_MARKER):
            yield json.dumps({"status": "progress", "chunk": 1, "total": 1}) + "\n"

            parsed_cards = parse_markdown_cards(markdown_content, level)
            with open(markdown_path, "w", encoding="utf-8") as f:
                f.write(format_direct_cards_markdown(parsed_cards))

            conn = get_db()
            cards_added, cards_skipped = insert_cards_for_doc(conn.cursor(), doc_id, parsed_cards, now_str)
            conn.commit()
            conn.close()

            yield json.dumps({
                "status": "complete",
                "doc_id": doc_id,
                "filename": file.filename,
                "markdown_cards": len(parsed_cards),
                "cards_added": cards_added,
                "cards_skipped": cards_skipped,
                "chunks_failed": 0,
            }) + "\n"
            return

        chunks = chunk_markdown(markdown_content)
        total_chunks = len(chunks)
        
        yield json.dumps({"status": "extracting", "message": f"Extracted {total_chunks} chunks. Starting vocabulary extraction..."}) + "\n"

        cards_added = 0
        chunks_failed = 0
        all_extracted_cards = []

        for idx, chunk in enumerate(chunks):
            # rate limiting helper for free-tier gateway
            if idx > 0:
                time.sleep(2)
            
            yield json.dumps({"status": "progress", "chunk": idx + 1, "total": total_chunks}) + "\n"
            
            prompt = USER_PROMPT_TEMPLATE.format(level=level, chunk=chunk)
            try:
                response_text = llm.chat(prompt, system=SYSTEM_PROMPT)
                parsed_cards = parse_llm_json(response_text)
                
                conn = get_db()
                cursor = conn.cursor()
                chunk_added, _ = insert_cards_for_doc(cursor, doc_id, parsed_cards, now_str)
                for item in parsed_cards:
                    if all([item.get("german_word"), item.get("english_gloss"), item.get("english_sentence"), item.get("german_sentence")]):
                        all_extracted_cards.append({
                            "german_word": item.get("german_word"),
                            "english_gloss": item.get("english_gloss"),
                            "level": item.get("level", level),
                            "english_sentence": item.get("english_sentence"),
                            "german_sentence": item.get("german_sentence"),
                        })
                conn.commit()
                conn.close()
                cards_added += chunk_added
            except Exception as e:
                chunks_failed += 1
                yield json.dumps({"status": "warning", "message": f"Chunk {idx + 1} extraction failed: {str(e)}"}) + "\n"

        # Rewrite markdown file to standard JSON Lines format to match 11.md/12.md
        if all_extracted_cards:
            try:
                with open(markdown_path, "w", encoding="utf-8") as f:
                    f.write(format_direct_cards_markdown(all_extracted_cards))
            except Exception:
                pass

        yield json.dumps({
            "status": "complete",
            "doc_id": doc_id,
            "filename": file.filename,
            "cards_added": cards_added,
            "chunks_failed": chunks_failed
        }) + "\n"

    return StreamingResponse(generate_progress(), media_type="application/x-ndjson")

@app.get("/api/cards")
def get_cards(level: str = None, limit: int = 5, doc_id: str = None, due_only: bool = True):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = """
        SELECT c.* FROM cards c
        INNER JOIN documents d ON c.doc_id = d.id
        WHERE 1=1
    """
    params = []
    
    if level and level.upper() != "ALL":
        query += " AND UPPER(c.level) = ?"
        params.append(level.upper())

    if doc_id and doc_id != "" and doc_id.upper() != "ALL":
        query += " AND c.doc_id = ?"
        params.append(int(doc_id))

    if due_only:
        query += " AND c.due_date <= ?"
        params.append(datetime.date.today().isoformat())

    query += " ORDER BY c.due_date ASC, c.box ASC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    cards = [dict(row) for row in rows]
    conn.close()
    return cards

@app.post("/api/review")
def review_card(req: ReviewRequest):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT c.* FROM cards c
        INNER JOIN documents d ON c.doc_id = d.id
        WHERE c.id = ?
    """, (req.card_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Card not found.")
        
    card = dict(row)
    current_box = card["box"]
    
    if req.correct:
        new_box = min(current_box + 1, 5)
    else:
        new_box = 1
        
    intervals = [0, 1, 3, 7, 21]
    days = intervals[new_box - 1]
    new_due_date = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    
    cursor.execute(
        "UPDATE cards SET box = ?, due_date = ? WHERE id = ?",
        (new_box, new_due_date, req.card_id)
    )
    conn.commit()
    
    cursor.execute("SELECT * FROM cards WHERE id = ?", (req.card_id,))
    updated_card = dict(cursor.fetchone())
    conn.close()
    return updated_card

@app.get("/api/stats")
def get_stats():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT c.level, COUNT(*) FROM cards c
        INNER JOIN documents d ON c.doc_id = d.id
        GROUP BY c.level
    """)
    by_level = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("""
        SELECT c.box, COUNT(*) FROM cards c
        INNER JOIN documents d ON c.doc_id = d.id
        GROUP BY c.box
    """)
    by_box = {str(row[0]): row[1] for row in cursor.fetchall()}

    cursor.execute("""
        SELECT COUNT(*) FROM cards c
        INNER JOIN documents d ON c.doc_id = d.id
        WHERE c.due_date <= ?
    """, (datetime.date.today().isoformat(),))
    due_today = cursor.fetchone()[0]
    
    conn.close()
    return {
        "by_level": by_level,
        "by_box": by_box,
        "due_today": due_today
    }

@app.get("/api/docs")
def get_docs():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT d.id, d.filename, d.created_at, COUNT(c.id) as card_count
        FROM documents d
        LEFT JOIN cards c ON d.id = c.doc_id
        GROUP BY d.id
        ORDER BY d.created_at DESC
    """)
    rows = cursor.fetchall()
    docs = [dict(row) for row in rows]
    conn.close()
    return docs

@app.delete("/api/docs/{doc_id}")
def delete_document(doc_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT markdown_path FROM documents WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Document not found.")
        
    markdown_path = row[0]
    cursor.execute("DELETE FROM cards WHERE doc_id = ?", (doc_id,))
    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    purge_orphan_cards(conn)
    conn.commit()
    conn.close()
    
    if markdown_path and os.path.exists(markdown_path):
        try:
            os.remove(markdown_path)
        except Exception:
            pass
            
    return {"status": "success", "message": "Document and cards deleted."}

@app.post("/api/docs/{doc_id}/resync")
def resync_document(doc_id: int):
    return resync_cards_from_markdown(doc_id)

@app.get("/api/markdown/{doc_id}")
def get_markdown(doc_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT markdown_path FROM documents WHERE id = ?", (doc_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Document markdown not found.")
        
    markdown_path = row[0]
    if not os.path.exists(markdown_path):
        raise HTTPException(status_code=404, detail="Markdown file not found on disk.")
        
    with open(markdown_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    return PlainTextResponse(content)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
def read_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    sample = """German Word
Type
English
Partizip II
German Sentence
English Sentence

die Abteilung, -en
noun
department
—
Unsere Abteilung ist für die Kundenbetreuung zuständig.
Our department is responsible for customer support."""
    cards = parse_vocabulary_text(sample, "B2")
    assert len(cards) == 1
    assert cards[0]["german_word"] == "**die Abteilung, -en**"
    assert "**Abteilung**" in cards[0]["german_sentence"]
    assert format_direct_cards_markdown(cards).startswith(DIRECT_CARDS_MARKER)
