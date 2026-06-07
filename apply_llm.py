"""
apply_llm.py — Swappable LLM for the auto-apply agent
=====================================================
Resume parsing/analysis/rewrite always use Gemini (elsewhere). The browser
*apply* agent routes through here so the driving model is configurable:

    APPLY_LLM=openai   → Azure GPT (APPLY_MODEL, e.g. gpt-5.4-mini)
    APPLY_LLM=gemini   → gemini-3.5-flash (the passed-in client)

Flip APPLY_LLM in .env to A/B the two without code changes.
"""

import os
import re
import json
import logging

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

APPLY_LLM   = os.getenv("APPLY_LLM", "gemini").strip().lower()
APPLY_MODEL = os.getenv("APPLY_MODEL", "gpt-5.4-mini").strip()
GEMINI_MODEL_DEFAULT = "gemini-3.5-flash"

_oai = None


def model_label() -> str:
    return f"Azure {APPLY_MODEL}" if APPLY_LLM == "openai" else GEMINI_MODEL_DEFAULT


def _openai_client():
    global _oai
    if _oai is None:
        from openai import OpenAI
        _oai = OpenAI(
            base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_KEY"),
        )
    return _oai


def _strip_json(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t


def _openai_json(prompt: str, image_b64: str = None, model: str = None) -> dict:
    content = [{"type": "text", "text": prompt}]
    if image_b64:  # OpenAI vision format (data URL)
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
    msgs = [{"role": "user", "content": content}]
    client = _openai_client()
    mdl = model or APPLY_MODEL
    try:  # prefer strict JSON mode
        resp = client.chat.completions.create(
            model=mdl, messages=msgs, response_format={"type": "json_object"})
    except Exception:  # some deployments reject response_format — retry plain
        resp = client.chat.completions.create(model=mdl, messages=msgs)
    return json.loads(_strip_json(resp.choices[0].message.content))


def _gemini_json(prompt: str, image_b64: str, gemini_client, gemini_model: str) -> dict:
    from google.genai import types
    parts = [{"text": prompt}]
    if image_b64:
        parts.append({"inline_data": {"mime_type": "image/png", "data": image_b64}})
    resp = gemini_client.models.generate_content(
        model=gemini_model or GEMINI_MODEL_DEFAULT,
        contents=[{"role": "user", "parts": parts}],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
    )
    return json.loads(_strip_json(resp.text or "{}"))


def llm_json(prompt: str, image_b64: str = None,
             gemini_client=None, gemini_model: str = GEMINI_MODEL_DEFAULT) -> dict:
    """Route a JSON-returning LLM call to the configured apply provider."""
    if APPLY_LLM == "openai":
        return _openai_json(prompt, image_b64)
    return _gemini_json(prompt, image_b64, gemini_client, gemini_model)
