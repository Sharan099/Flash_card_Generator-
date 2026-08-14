"""API smoke tests — run: python test_api.py"""
import datetime
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

# ponytail: isolated DB per test run so we never touch the user's deck
TEST_DB = os.path.join(tempfile.gettempdir(), "lingo_test.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from services import pdf_service

import server as app_module

app_module.DB_PATH = TEST_DB
app_module.init_db()
client = TestClient(app_module.app)

SAMPLE_MD = """<!-- DIRECT_CARDS_JSON_LINES -->
{"german_word": "**Haus**", "english_gloss": "**house**", "level": "B1", "english_sentence": "The **house** is big.", "german_sentence": "Das **Haus** ist groß."}
{"german_word": "**Auto**", "english_gloss": "**car**", "level": "B1", "english_sentence": "The **car** is fast.", "german_sentence": "Das **Auto** ist schnell."}
"""


def seed_doc_with_cards():
    conn = app_module.get_db()
    now = datetime.datetime.utcnow().isoformat()
    today = datetime.date.today().isoformat()
    conn.execute(
        "INSERT INTO documents (filename, markdown_path, created_at) VALUES (?, ?, ?)",
        ("test.pdf", "data/markdown/test.md", now),
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for word, gloss, en, de in [
        ("**Haus**", "**house**", "The **house** is big.", "Das **Haus** ist groß."),
        ("**Auto**", "**car**", "The **car** is fast.", "Das **Auto** ist schnell."),
    ]:
        conn.execute(
            """INSERT INTO cards (doc_id, level, german_word, english_gloss, english_sentence, german_sentence, box, due_date, created_at)
               VALUES (?, 'B1', ?, ?, ?, ?, 1, ?, ?)""",
            (doc_id, word, gloss, en, de, today, now),
        )
    conn.commit()
    conn.close()
    return doc_id


def test_empty_state():
    r = client.get("/api/docs")
    assert r.status_code == 200
    assert r.json() == []
    r = client.get("/api/cards?limit=100&due_only=false")
    assert r.status_code == 200
    assert r.json() == []
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert sum(r.json()["by_box"].values()) == 0


def test_delete_removes_cards():
    doc_id = seed_doc_with_cards()
    r = client.get("/api/cards?limit=100&due_only=false")
    assert len(r.json()) == 2
    r = client.delete(f"/api/docs/{doc_id}")
    assert r.status_code == 200
    assert client.get("/api/docs").json() == []
    assert client.get("/api/cards?limit=100&due_only=false").json() == []
    assert sum(client.get("/api/stats").json()["by_box"].values()) == 0


def test_orphan_cards_purged_and_hidden():
    conn = sqlite3.connect(TEST_DB)
    conn.execute("PRAGMA foreign_keys = OFF")
    now = datetime.datetime.utcnow().isoformat()
    today = datetime.date.today().isoformat()
    conn.execute(
        """INSERT INTO cards (doc_id, level, german_word, english_gloss, english_sentence, german_sentence, box, due_date, created_at)
           VALUES (9999, 'B1', '**x**', '**y**', 'An **y** here.', 'Ein **x** hier.', 1, ?, ?)""",
        (today, now),
    )
    conn.commit()
    orphan_count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    conn.close()
    assert orphan_count == 1

    # API hides orphans even before purge
    assert client.get("/api/cards?limit=100&due_only=false").json() == []
    app_module.purge_orphan_cards()
    conn = sqlite3.connect(TEST_DB)
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    conn.close()


def test_review_and_due_filter():
    doc_id = seed_doc_with_cards()
    cards = client.get("/api/cards?limit=100&due_only=false").json()
    card_id = cards[0]["id"]
    updated = client.post("/api/review", json={"card_id": card_id, "correct": True}).json()
    assert updated["box"] == 2
    r = client.get(f"/api/cards?limit=100&due_only=true&doc_id={doc_id}")
    assert all(c["id"] != card_id for c in r.json())
    client.delete(f"/api/docs/{doc_id}")


def test_doc_filter():
    doc_id = seed_doc_with_cards()
    r = client.get(f"/api/cards?limit=100&due_only=false&doc_id={doc_id}")
    assert len(r.json()) == 2
    r = client.get("/api/cards?limit=100&due_only=false&doc_id=ALL")
    assert len(r.json()) == 2
    client.delete(f"/api/docs/{doc_id}")


def test_format_helpers():
    cards = pdf_service.parse_vocabulary_text(
        "German Word\nType\nEnglish\nPartizip II\nGerman Sentence\nEnglish Sentence\n\n"
        "die Abteilung, -en\nnoun\ndepartment\n—\nUnsere Abteilung ist groß.\nOur department is big.",
        "B2",
    )
    assert len(cards) == 1
    assert cards[0]["german_word"] == "**die Abteilung, -en**"
    assert "**Abteilung**" in cards[0]["german_sentence"]


def test_study_widget_features():
    widget = Path("static/study_widget.html").read_text(encoding="utf-8")
    assert 'aria-label="CEFR level filter"' in widget
    assert "function playFlipSound()" in widget
    assert "speakGerman(filtered[current].german_sentence" in widget


if __name__ == "__main__":
    test_empty_state()
    test_delete_removes_cards()
    test_orphan_cards_purged_and_hidden()
    test_review_and_due_filter()
    test_doc_filter()
    test_format_helpers()
    test_study_widget_features()
    print("All tests passed.")
