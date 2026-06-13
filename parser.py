"""
parser.py — Resume file parser using google-genai (NEW SDK)
============================================================
Install: uv pip install google-genai python-dotenv
Supports: PDF, DOCX, PNG, JPG
"""

import os
import time
from pathlib import Path
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY not found. Please set it in your .env file.")

# 90s HTTP timeout — Gemini occasionally stalls for minutes; better to fail
# fast and fall back than hang the caller indefinitely.
client = genai.Client(api_key=api_key, http_options={"timeout": 90_000})
MODEL = "gemini-3.5-flash"

MIME_MAP = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
}

PARSE_PROMPT = """You are an expert resume parser.
Extract the complete text content from this resume file.
Preserve the original structure: sections, bullet points, dates, and formatting.
Return ONLY the extracted text — no commentary, no explanations, no markdown fences."""


# Models to try in order — fallback if one is overloaded
FALLBACK_MODELS = [
    "gemini-3.5-flash",
]

def parse_resume_with_gemini(file_path: str) -> str:
    path = Path(file_path)

    if not path.exists():
        return f"Error: File not found at '{file_path}'"

    suffix = path.suffix.lower()
    mime_type = MIME_MAP.get(suffix)
    if not mime_type:
        return f"Error: Unsupported file type '{suffix}'. Supported: {', '.join(MIME_MAP.keys())}"

    print(f"  Uploading {path.name} to Gemini File API...")

    uploaded_file = None
    try:
        uploaded_file = client.files.upload(
            file=file_path,
            config=types.UploadFileConfig(mime_type=mime_type)
        )

        _wait_for_file_ready(uploaded_file)
        print("  Extracting text...")

        last_error = None
        for model in FALLBACK_MODELS:
            try:
                print(f"  Trying model: {model}")
                response = client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_uri(
                            file_uri=uploaded_file.uri,
                            mime_type=mime_type,
                        ),
                        PARSE_PROMPT
                    ],
                    config=types.GenerateContentConfig(temperature=0.0)
                )
                print(f"  ✅ Parsed with {model}")
                return response.text.strip()

            except Exception as e:
                err_str = str(e)
                # 503 = overloaded, 429 = rate limit — both worth retrying
                if "503" in err_str or "429" in err_str or "overloaded" in err_str.lower():
                    print(f"  ⚠ {model} unavailable ({err_str[:60]}), trying next...")
                    last_error = e
                    time.sleep(2)  # brief pause before next attempt
                    continue
                else:
                    # Different error — don't retry
                    raise

        return f"Error: All models failed. Last error: {last_error}"

    except Exception as e:
        return f"Error: Failed to parse resume — {e}"

    finally:
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
                print("  Cleaned up uploaded file.")
            except Exception:
                pass


def _wait_for_file_ready(uploaded_file, timeout: int = 30):
    start = time.time()
    while True:
        file_info = client.files.get(name=uploaded_file.name)
        state = str(file_info.state).upper()
        if "ACTIVE" in state:
            return
        if "FAILED" in state:
            raise RuntimeError(f"File processing failed: {file_info.name}")
        if time.time() - start > timeout:
            raise TimeoutError(f"File not ready after {timeout}s")
        time.sleep(1)


if __name__ == "__main__":
    import sys
    file_to_test = sys.argv[1] if len(sys.argv) > 1 else "resumes/sample_resume.pdf"
    print(f"\n--- Testing parser on: {file_to_test} ---\n")
    result = parse_resume_with_gemini(file_to_test)
    if result.startswith("Error:"):
        print(f"❌ {result}")
        sys.exit(1)
    print("✅ Extracted text:\n")
    print(result)
    print(f"\n--- Done ({len(result)} chars) ---")