import os
import sqlite3
import datetime
import time
import json
import re
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from services import flashcard_service, llm_service, pdf_service

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
    items = pdf_service.parse_markdown_cards(content, level)
    now_str = datetime.datetime.utcnow().isoformat()
    with open(row[0], "w", encoding="utf-8") as f:
        f.write(pdf_service.format_direct_cards_markdown(items))
    conn.execute("DELETE FROM cards WHERE doc_id = ?", (doc_id,))
    added, skipped = insert_cards_for_doc(conn.cursor(), doc_id, items, now_str)
    conn.commit()
    conn.close()
    return {"markdown_cards": len(items), "cards_added": added, "cards_skipped": skipped}


class ReviewRequest(BaseModel):
    card_id: int
    correct: bool


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
            markdown_content = pdf_service.extract_clean_markdown(temp_pdf_path, level=level)
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

        if not markdown_content.startswith(pdf_service.DIRECT_CARDS_MARKER):
            parsed_cards = pdf_service.parse_vocabulary_text(markdown_content, level)
            if pdf_service.validate_direct_cards(parsed_cards):
                markdown_content = pdf_service.format_direct_cards_markdown(parsed_cards)
                with open(markdown_path, "w", encoding="utf-8") as f:
                    f.write(markdown_content)

        if markdown_content.startswith(pdf_service.DIRECT_CARDS_MARKER):
            yield json.dumps({"status": "progress", "chunk": 1, "total": 1}) + "\n"

            parsed_cards = pdf_service.parse_markdown_cards(markdown_content, level)
            with open(markdown_path, "w", encoding="utf-8") as f:
                f.write(pdf_service.format_direct_cards_markdown(parsed_cards))

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

        chunks = flashcard_service.chunk_markdown(markdown_content)
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
            
            prompt = flashcard_service.USER_PROMPT_TEMPLATE.format(level=level, chunk=chunk)
            try:
                response_text = llm_service.chat(prompt, system=flashcard_service.SYSTEM_PROMPT)
                parsed_cards = flashcard_service.parse_llm_json(response_text)
                
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
                    f.write(pdf_service.format_direct_cards_markdown(all_extracted_cards))
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
    cards = pdf_service.parse_vocabulary_text(sample, "B2")
    assert len(cards) == 1
    assert cards[0]["german_word"] == "**die Abteilung, -en**"
    assert "**Abteilung**" in cards[0]["german_sentence"]
    assert pdf_service.format_direct_cards_markdown(cards).startswith(pdf_service.DIRECT_CARDS_MARKER)
