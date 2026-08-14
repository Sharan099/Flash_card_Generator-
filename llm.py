import os
from dotenv import load_dotenv
from openai import OpenAI

# load env using absolute path to ensure reliability under uvicorn
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

def _config():
    return {
        "base_url": os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        "api_key": os.getenv("LLM_API_KEY"),
        "model": os.getenv("LLM_MODEL", "nvidia/nemotron-nano-9b-v2:free"),
    }

def _client(api_key: str, base_url: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key)

def chat(prompt: str, system: str = "") -> str:
    cfg = _config()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        if not cfg["api_key"]:
            raise RuntimeError("LLM_API_KEY is not set.")

        response = _client(cfg["api_key"], cfg["base_url"]).chat.completions.create(
            model=cfg["model"],
            messages=messages,
            temperature=0.2,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        error_msg = str(e)
        groq_key = os.getenv("GROQ_API_KEY")

        # If OpenRouter is rate-limited (HTTP 429), fall back to Groq automatically
        if ("429" in error_msg or "rate limit" in error_msg.lower()) and groq_key:
            print("OpenRouter rate-limited. Falling back to Groq Llama-3.1...")
            try:
                response = _client(groq_key, "https://api.groq.com/openai/v1").chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages,
                    temperature=0.2,
                )
                return response.choices[0].message.content or ""
            except Exception as groq_err:
                raise RuntimeError(f"Fallback Groq API failed: {groq_err}. (Original OpenRouter error: {e})")
        else:
            raise RuntimeError(f"LLM API request failed: {e}")
