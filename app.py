"""Hugging Face Space entry point — Gradio UI for flashcard generation."""
import os
import tempfile

import gradio as gr

from services import flashcard_service


def _clear_outputs():
    return "", "", "", gr.update(value=None)


def _generate(pdf_file, level):
    if pdf_file is None:
        raise gr.Error("Please upload a PDF file.")

    path = pdf_file if isinstance(pdf_file, str) else getattr(pdf_file, "name", None)
    if not path:
        raise gr.Error("Could not read the uploaded file.")

    filename = os.path.basename(path)
    status = "Processing…"

    def on_progress(msg: str):
        nonlocal status
        status = msg

    try:
        result = flashcard_service.generate_flashcards(
            path,
            level=level,
            filename=filename,
            progress_callback=on_progress,
        )
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    except Exception as exc:
        raise gr.Error(f"Unexpected error: {exc}") from exc

    cards = result["cards"]
    md = flashcard_service.format_cards_markdown(cards)
    js = flashcard_service.cards_to_json(cards)
    summary = (
        f"Generated **{result['count']}** flashcards at level **{result['level']}** "
        f"via **{result['source']}** extraction."
    )
    if result.get("warnings"):
        summary += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in result["warnings"])
    if result.get("chunks_failed"):
        summary += f"\n\n{result['chunks_failed']} LLM chunk(s) failed."

    return summary, md, js


with gr.Blocks(title="Flash Card Generator") as demo:
    gr.Markdown(
        "# 📚 Flash Card Generator\n"
        "Upload a German vocabulary PDF to extract flashcards. "
        "Structured PDFs are parsed directly; others use an LLM fallback."
    )

    with gr.Row():
        pdf_input = gr.File(label="Upload PDF", file_types=[".pdf"], type="filepath")
        level = gr.Dropdown(
            choices=["A1", "A2", "B1", "B2", "C1", "C2"],
            value="B1",
            label="CEFR Level",
        )

    with gr.Row():
        generate_btn = gr.Button("Generate Flashcards", variant="primary")
        clear_btn = gr.Button("Clear")

    status_out = gr.Markdown("")
    cards_out = gr.Markdown(label="Flashcards")
    json_out = gr.Code(label="JSON (debug)", language="json")

    generate_btn.click(
        fn=_generate,
        inputs=[pdf_input, level],
        outputs=[status_out, cards_out, json_out],
    )
    clear_btn.click(fn=_clear_outputs, outputs=[status_out, cards_out, json_out, pdf_input])

if __name__ == "__main__":
    demo.launch(theme=gr.themes.Soft())
