# Dockerfile for Hugging Face Spaces (sdk: docker)
#
# WHY THIS EXISTS:
# mega.py==1.0.8 hard-pins tenacity<6.0.0 in its package metadata. That old
# tenacity version uses asyncio.coroutine, which was REMOVED in Python 3.11+
# and crashes the bot on import. A normal `pip install -r requirements.txt`
# cannot satisfy both "tenacity<6.0.0" (from mega.py) and "tenacity==8.5.0"
# (which we need) at the same time — pip's resolver correctly rejects it.
#
# THE FIX: install tenacity 8.5.0 FIRST, then install mega.py with --no-deps
# so it never re-pulls the old, broken tenacity. mega.py only imports
# `retry`, `wait_exponential`, `retry_if_exception_type` from tenacity —
# all three exist unchanged in tenacity 8.x, so this is safe.

FROM python:3.11-slim

# Hugging Face Spaces convention: run as a non-root user named "user"
RUN useradd -m -u 1000 user
WORKDIR /app

# System packages needed to build tgcrypto (C extension) and pycryptodome
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── Step 1: install everything EXCEPT mega.py and tenacity ──────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir \
    pyrogram==2.0.106 \
    tgcrypto==1.2.5 \
    python-dotenv==1.0.1 \
    gradio==4.44.0 \
    requests \
    pycryptodome

# ── Step 2: install the modern, compatible tenacity ─────────────────────────
RUN pip install --no-cache-dir tenacity==8.5.0

# ── Step 3: install mega.py WITHOUT letting it downgrade tenacity ──────────
RUN pip install --no-cache-dir --no-deps mega.py==1.0.8

# ── Copy the bot code ────────────────────────────────────────────────────────
COPY --chown=user . /app

USER user
ENV PATH="/home/user/.local/bin:$PATH"

# Hugging Face Spaces (Docker SDK) expects the app to listen on this port
EXPOSE 7860
ENV PORT=7860

CMD ["python", "bot.py"]
