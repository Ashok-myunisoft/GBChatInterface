import os
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent

# --- GoodBooks (GB4) session -------------------------------------------
# Set only on the mcp_server.py subprocess's environment by app.py's
# /chat handler, from the caller's `Login` header. Never read from a
# .env file and never present in the main FastAPI process's own env.
GOODBOOKS_LOGIN_JSON = os.environ.get("GOODBOOKS_LOGIN_JSON", "")
GOODBOOKS_BASE_URL = os.environ.get("GOODBOOKS_BASE_URL", "").rstrip("/")

# --- OpenAI -----------------------------------------------------------------
# This one's fine to keep in .env -- it's a shared company key, not a
# per-user login, so it doesn't have the same "shouldn't be on disk"
# problem the GoodBooks session does.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def require_openai_config():
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing required environment variable: OPENAI_API_KEY.")