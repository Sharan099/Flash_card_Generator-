"""Hugging Face Space entry point — Gradio UI for flashcard generation."""
import inspect
import os

# ponytail: HF Spaces force gradio 6.24; disable SSR to avoid Node proxy crash on /config
os.environ.setdefault("GRADIO_SSR_MODE", "false")


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
        config_routes = []
        for route in starlette_app.routes:
            ep = getattr(route, "endpoint", None)
            name = getattr(ep, "__name__", "")
            if name == "get_current_user":
                get_current_user_fn = ep
            elif name == "get_config":
                config_routes.append(route)

        if get_current_user_fn and inspect.iscoroutinefunction(get_current_user_fn):
            for route in config_routes:
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
    demo.launch(ssr_mode=False)
