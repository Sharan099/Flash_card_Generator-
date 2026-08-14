"""Hugging Face Space entry point — Gradio UI for flashcard generation."""
import base64
import json
import os

_WIDGET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "study_widget.html")
_EMPTY_WIDGET = '<div style="font-family:Outfit,sans-serif;padding:40px;text-align:center;color:#666">Upload a PDF to begin.</div>'

GRADIO_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;700;800&display=swap');
.gradio-container { background: #faf6ed !important; max-width: 1100px !important; font-family: 'Outfit', sans-serif !important; }
footer { display: none !important; }
.fcg-upload-panel {
  border: 2px solid #1a1a1a !important; border-radius: 16px !important;
  background: #fff !important; box-shadow: 4px 4px 0 #1a1a1a !important; padding: 16px !important;
}
.fcg-gen-btn button {
  background: #f2c94c !important; border: 2px solid #1a1a1a !important;
  box-shadow: 3px 3px 0 #1a1a1a !important; font-weight: 800 !important; border-radius: 50px !important;
}
"""


import gradio as gr

from services import flashcard_service


def build_study_html(cards: list[dict]) -> str:
    if not os.path.isfile(_WIDGET_PATH):
        return _EMPTY_WIDGET
    with open(_WIDGET_PATH, encoding="utf-8") as f:
        body = f.read()
    payload = json.dumps(cards, ensure_ascii=False).replace("</", "<\\/")
    body = body.replace("__CARDS_JSON__", payload)
    doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "</head><body style='margin:0;background:#faf6ed'>"
        f"{body}</body></html>"
    )
    b64 = base64.b64encode(doc.encode("utf-8")).decode("ascii")
    return (
        '<iframe title="Flashcard Study" '
        f'src="data:text/html;base64,{b64}" '
        'style="width:100%;min-height:950px;border:none;background:#faf6ed;"></iframe>'
    )


def _generate(pdf_file, level):
    if pdf_file is None:
        raise gr.Error("Please upload a PDF file.")

    path = pdf_file if isinstance(pdf_file, str) else getattr(pdf_file, "name", None)
    if not path:
        raise gr.Error("Could not read the uploaded file.")

    filename = os.path.basename(path)

    try:
        result = flashcard_service.generate_flashcards(
            path,
            level=level,
            filename=filename,
        )
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    except Exception as exc:
        raise gr.Error(f"Unexpected error: {exc}") from exc

    return build_study_html(result["cards"])


with gr.Blocks(title="Flashcard") as demo:
    with gr.Group(elem_classes=["fcg-upload-panel"]):
        gr.Markdown("### Upload a German vocabulary PDF")
        with gr.Row():
            pdf_input = gr.File(label="PDF", file_types=[".pdf"], type="filepath")
            level = gr.Dropdown(
                choices=["A1", "A2", "B1", "B2", "C1", "C2"],
                value="B1",
                label="CEFR Level",
            )
        generate_btn = gr.Button("Generate Flashcards", elem_classes=["fcg-gen-btn"])

    study_view = gr.HTML(value=build_study_html([]), padding=False)

    generate_btn.click(
        fn=_generate,
        inputs=[pdf_input, level],
        outputs=[study_view],
    )

demo.queue()

if __name__ == "__main__":
    demo.launch(css=GRADIO_CSS, ssr_mode=False)
