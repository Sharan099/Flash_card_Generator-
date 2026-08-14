"""Flashcard generation orchestration — framework-agnostic."""
import ast
import json
import os
import re
import time
from typing import Callable

from services import llm_service, pdf_service

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


def parse_llm_json(text: str) -> list[dict]:
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
        text_clean = text_clean[start : end + 1]

    try:
        return json.loads(text_clean)
    except Exception:
        try:
            val = ast.literal_eval(text_clean)
            if isinstance(val, list):
                return val
        except Exception:
            pass

        cards = []
        card_blocks = re.findall(r"\{[^{}]*\}", text)
        for block in card_blocks:
            card = {}
            for line in block.splitlines():
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


def _detect_level(filename: str, level: str) -> str:
    fn = filename.lower()
    if "b2" in fn:
        return "B2"
    if "b1" in fn:
        return "B1"
    return level


def _normalize_card(item: dict, default_level: str) -> dict | None:
    g_word = item.get("german_word")
    e_gloss = item.get("english_gloss")
    e_sent = item.get("english_sentence")
    g_sent = item.get("german_sentence")
    if not all([g_word, e_gloss, e_sent, g_sent]):
        return None
    return {
        "german_word": g_word,
        "english_gloss": e_gloss,
        "level": item.get("level", default_level),
        "english_sentence": e_sent,
        "german_sentence": g_sent,
    }


def generate_flashcards(
    file_path: str,
    *,
    level: str = "B1",
    filename: str = "",
    progress_callback: Callable[[str], None] | None = None,
) -> dict:
    """Extract flashcards from a PDF file path. No Gradio or FastAPI dependencies."""
    if not file_path or not os.path.isfile(file_path):
        raise ValueError("No PDF file provided.")

    name = filename or os.path.basename(file_path)
    if not name.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are supported.")

    level = _detect_level(name, level)

    with open(file_path, "rb") as f:
        content = f.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise ValueError("File size exceeds 25 MB limit.")
    if not content.startswith(b"%PDF"):
        raise ValueError("Invalid PDF file format.")

    if progress_callback:
        progress_callback("Extracting text from PDF…")

    try:
        markdown_content = pdf_service.extract_clean_markdown(file_path, level=level)
    except Exception as exc:
        raise ValueError(f"PDF extraction failed: {exc}") from exc

    if not markdown_content.strip():
        raise ValueError("PDF appears empty or contains no extractable text.")

    cards: list[dict] = []
    source = "direct"
    chunks_failed = 0
    warnings: list[str] = []

    if not markdown_content.startswith(pdf_service.DIRECT_CARDS_MARKER):
        parsed = pdf_service.parse_vocabulary_text(markdown_content, level)
        if pdf_service.validate_direct_cards(parsed):
            markdown_content = pdf_service.format_direct_cards_markdown(parsed)

    if markdown_content.startswith(pdf_service.DIRECT_CARDS_MARKER):
        cards = pdf_service.parse_markdown_cards(markdown_content, level)
    else:
        source = "llm"
        chunks = chunk_markdown(markdown_content)
        total = len(chunks)
        if progress_callback:
            progress_callback(f"LLM extraction: {total} chunk(s)…")

        for idx, chunk in enumerate(chunks):
            if idx > 0:
                time.sleep(2)
            if progress_callback:
                progress_callback(f"LLM chunk {idx + 1} of {total}…")

            prompt = USER_PROMPT_TEMPLATE.format(level=level, chunk=chunk)
            try:
                response_text = llm_service.chat(prompt, system=SYSTEM_PROMPT)
                parsed_cards = parse_llm_json(response_text)
                for item in parsed_cards:
                    card = _normalize_card(item, level)
                    if card:
                        cards.append(card)
            except Exception as exc:
                chunks_failed += 1
                warnings.append(f"Chunk {idx + 1} failed: {exc}")

    if not cards:
        if chunks_failed:
            raise ValueError(
                "Flashcard generation failed. Check LLM_API_KEY and try again."
            )
        raise ValueError("No flashcards could be generated from this PDF.")

    return {
        "cards": cards,
        "count": len(cards),
        "level": level,
        "source": source,
        "chunks_failed": chunks_failed,
        "warnings": warnings,
    }


def format_cards_markdown(cards: list[dict]) -> str:
    """Readable Q/A presentation for the UI."""
    parts = []
    for i, card in enumerate(cards, 1):
        gloss = pdf_service.strip_bold(card.get("english_gloss", ""))
        en_sent = pdf_service.strip_bold(card.get("english_sentence", ""))
        de_word = pdf_service.strip_bold(card.get("german_word", ""))
        de_sent = pdf_service.strip_bold(card.get("german_sentence", ""))
        lvl = card.get("level", "")
        parts.append(
            f"### Card {i} ({lvl})\n\n"
            f"**Question:**\n\n{gloss}\n\n{en_sent}\n\n"
            f"**Answer:**\n\n{de_word}\n\n{de_sent}"
        )
    return "\n\n---\n\n".join(parts)


def cards_to_json(cards: list[dict]) -> str:
    return json.dumps(cards, ensure_ascii=False, indent=2)
