"""Hugging Face Space entry point — Gradio UI for flashcard generation."""
import base64
import inspect
import json
import os

# ponytail: HF may set SSR=true; force off to avoid Node proxy crash on gradio 6.24 /config bug
os.environ["GRADIO_SSR_MODE"] = "false"

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


def _iter_routes(routes):
    for route in routes:
        nested = getattr(route, "routes", None)
        if nested:
            yield from _iter_routes(nested)
        else:
            yield route


def _patch_gradio624_get_config():
    """ponytail: gradio 6.24 get_config calls async get_current_user without await."""
    import gradio.route_utils as route_utils
    import gradio.routes as gr_routes
    import gradio.utils as utils
    from gradio.routes import ORJSONResponse

    if getattr(gr_routes, "_fcg_get_config_patched", False):
        return

    orig_create_app = gr_routes.App.create_app

    @staticmethod
    def patched_create_app(
        blocks,
        app=None,
        app_kwargs=None,
        auth_dependency=None,
        strict_cors=True,
        mcp_server=None,
        debug=False,
    ):
        starlette_app = orig_create_app(
            blocks, app, app_kwargs, auth_dependency, strict_cors, mcp_server, debug
        )

        get_current_user_fn = None
        for route in _iter_routes(starlette_app.routes):
            ep = getattr(route, "endpoint", None)
            if getattr(ep, "__name__", "") == "get_current_user":
                get_current_user_fn = ep
                break

        if get_current_user_fn is None or not inspect.iscoroutinefunction(get_current_user_fn):
            return starlette_app

        for route in _iter_routes(starlette_app.routes):
            if getattr(getattr(route, "endpoint", None), "__name__", "") != "get_config":
                continue

            _blocks = blocks
            _app = starlette_app
            _gcu = get_current_user_fn

            async def get_config_fixed(
                request,
                deep_link="",
                _blocks=_blocks,
                _app=_app,
                _gcu=_gcu,
            ):
                config = utils.safe_deepcopy(_app.get_blocks().config)
                root = route_utils.get_root_url(
                    request=request,
                    route_path="/config",
                    root_path=_app.root_path
                    or request.scope.get("root_path")
                    or _blocks.custom_mount_path,
                )
                config["username"] = await _gcu(request)
                if hasattr(_blocks, "i18n_instance") and _blocks.i18n_instance:
                    config["i18n_translations"] = _blocks.i18n_instance.translations_dict
                else:
                    config["i18n_translations"] = None
                config = route_utils.update_root_in_config(config, root)
                return ORJSONResponse(content=config)

            route.endpoint = get_config_fixed

        gr_routes._fcg_get_config_patched = True
        return starlette_app

    gr_routes.App.create_app = patched_create_app


_patch_gradio624_get_config()

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
    demo.launch(
        css=GRADIO_CSS,
        ssr_mode=False,
        server_name="0.0.0.0",
        server_port=int(os.getenv("PORT", "7860")),
    )
