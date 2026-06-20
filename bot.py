"""
mega_bot.py — Private Telegram bot that downloads Mega.nz links and
re-uploads them to Telegram as uncompressed documents (up to 2 GB).

Features:
  • Private: only the configured ALLOWED_USER_ID can interact
  • Daily quota: max DAILY_LIMIT downloads, resets at midnight
  • Aggressive cleanup: local file deleted immediately after upload
  • /start  — welcome message
  • /stats  — remaining quota for today
  • /help   — usage instructions
  • Paste any mega.nz file OR folder link → download all files → upload → cleanup

Dependencies (see requirements.txt):
  pyrogram, mega.py, python-dotenv
"""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

# ── Python 3.10+ compatibility fix ───────────────────────────────────────────
# asyncio.get_event_loop() no longer auto-creates a loop in Python 3.10+.
# Pyrogram 2.0.x calls it at import time inside sync.py, which raises a
# RuntimeError on Python 3.10–3.14. We create and set a loop explicitly
# BEFORE importing Pyrogram so it finds one already in place.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ─────────────────────────────────────────────────────────────────────────────

from dotenv import load_dotenv
from mega import Mega
from pyrogram import Client, filters
from pyrogram.types import Message

# ─────────────────────────────────────────────────────────────────────────────
# 0.  Configuration & logging
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()  # Load variables from a local .env file (ignored in production)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mega_bot")

# ── Required environment variables ───────────────────────────────────────────
API_ID       = int(os.environ["API_ID"])
API_HASH     = os.environ["API_HASH"]
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

# ── Optional / tunable ───────────────────────────────────────────────────────
DAILY_LIMIT   = int(os.environ.get("DAILY_LIMIT", "20"))
DOWNLOAD_DIR  = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/mega_downloads"))
QUOTA_FILE    = Path(os.environ.get("QUOTA_FILE",  "quota.json"))
MEGA_EMAIL    = os.environ.get("MEGA_EMAIL", "")
MEGA_PASSWORD = os.environ.get("MEGA_PASSWORD", "")

# Matches both file and folder mega.nz links
MEGA_LINK_RE = re.compile(
    r"https?://mega\.nz/(?:#|file/|folder/)[^\s]+",
    re.IGNORECASE,
)
# Detects folder vs single-file links
_FOLDER_RE = re.compile(r"mega\.nz/folder/", re.IGNORECASE)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Quota helpers  (persisted to a tiny JSON file)
# ─────────────────────────────────────────────────────────────────────────────

def _load_quota() -> dict:
    """Return the quota dict, resetting it if the date has changed."""
    today = str(date.today())
    if QUOTA_FILE.exists():
        try:
            data = json.loads(QUOTA_FILE.read_text())
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    fresh = {"date": today, "used": 0}
    QUOTA_FILE.write_text(json.dumps(fresh))
    return fresh


def _save_quota(data: dict) -> None:
    QUOTA_FILE.write_text(json.dumps(data))


def quota_remaining() -> int:
    return max(0, DAILY_LIMIT - _load_quota()["used"])


def quota_increment() -> bool:
    data = _load_quota()
    if data["used"] >= DAILY_LIMIT:
        return False
    data["used"] += 1
    _save_quota(data)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Mega download helpers  (run in a thread executor — blocking I/O)
# ─────────────────────────────────────────────────────────────────────────────

def _login_mega():
    """Create and return an authenticated (or anonymous) Mega session."""
    mega = Mega()
    if MEGA_EMAIL and MEGA_PASSWORD:
        log.info("Logging into Mega.nz with credentials …")
        return mega.login(MEGA_EMAIL, MEGA_PASSWORD)
    log.info("Connecting to Mega.nz anonymously …")
    return mega.login()


def _parse_folder_url(url: str):
    """
    Parse a public folder URL into (folder_id, folder_key, subfolder_id|None).

    Handles:
      mega.nz/folder/FOLDER_ID#FOLDER_KEY
      mega.nz/folder/FOLDER_ID#FOLDER_KEY/folder/SUBFOLDER_ID
    """
    path = re.split(r"mega\.nz/folder/", url, flags=re.IGNORECASE)[1]
    folder_id, rest = path.split("#", 1)
    parts = rest.split("/folder/", 1)
    folder_key   = parts[0]
    subfolder_id = parts[1] if len(parts) > 1 else None
    return folder_id, folder_key, subfolder_id


def _list_public_folder_files(folder_id: str, folder_key: str, subfolder_id):
    """
    List all files inside a public Mega folder by calling the API directly.
    Returns a list of dicts with decrypted name, key, iv, meta_mac, size, handle.

    mega.py has no built-in method for public folders, so we replicate its
    internal crypto logic here using the same helper functions it uses.
    """
    import requests as _req
    from mega.crypto import (
        base64_to_a32, decrypt_key, decrypt_attr,
        base64_url_decode, str_to_a32,
    )

    # Decode the folder key (base64 → tuple of ints)
    fk = base64_to_a32(folder_key)

    # Fetch all nodes in the public folder recursively
    resp = _req.post(
        "https://g.api.mega.co.nz/cs",
        params={"id": 1, "n": folder_id},
        json=[{"a": "f", "c": 1, "r": 1}],
        timeout=30,
    )
    resp.raise_for_status()
    nodes = resp.json()[0]["f"]

    files = []
    for node in nodes:
        if node.get("t") != 0:          # t==0 means file; skip folders
            continue
        if subfolder_id and node.get("p") != subfolder_id:
            continue                     # filter to requested subfolder only

        # Decrypt the file's node key using the folder key
        raw_key = node["k"].split(":")[-1]   # format is "HANDLE:BASE64KEY"
        enc_key  = str_to_a32(base64_url_decode(raw_key))
        file_key = decrypt_key(enc_key, fk)

        # Derive AES key, IV and MAC from the decrypted key tuple
        k        = (file_key[0] ^ file_key[4], file_key[1] ^ file_key[5],
                    file_key[2] ^ file_key[6], file_key[3] ^ file_key[7])
        iv       = file_key[4:6] + (0, 0)
        meta_mac = file_key[6:8]

        # Decrypt file name from the attributes blob
        attrs = decrypt_attr(base64_url_decode(node["a"]), k)
        if not attrs:
            continue

        files.append({
            "h":        node["h"],
            "k":        k,
            "iv":       iv,
            "meta_mac": meta_mac,
            "name":     attrs.get("n", node["h"]),
            "size":     node.get("s", 0),
        })

    return files


def _download_single_folder_file(meta: dict, folder_id: str,
                                  dest_dir: Path) -> Path:
    """
    Download one file from a public folder, stream-decrypting it on the fly.
    This replicates mega.py's _download_file logic for public folder nodes.
    """
    import requests as _req
    import tempfile, shutil
    from mega.crypto import a32_to_str, base64_url_decode, get_chunks
    from Crypto.Cipher import AES
    from Crypto.Util import Counter

    # Get the CDN download URL for this specific file node
    resp = _req.post(
        "https://g.api.mega.co.nz/cs",
        params={"id": 1, "n": folder_id},
        json=[{"a": "g", "g": 1, "n": meta["h"]}],
        timeout=30,
    )
    resp.raise_for_status()
    file_url = resp.json()[0]["g"]

    k  = meta["k"]
    iv = meta["iv"]

    k_str   = a32_to_str(k)
    counter = Counter.new(128, initial_value=((iv[0] << 32) + iv[1]) << 64)
    aes     = AES.new(k_str, AES.MODE_CTR, counter=counter)

    mac_str       = b'\x00' * 16
    mac_encryptor = AES.new(k_str, AES.MODE_CBC, mac_str)
    iv_str        = a32_to_str([iv[0], iv[1], iv[0], iv[1]])

    stream = _req.get(file_url, stream=True).raw

    with tempfile.NamedTemporaryFile(mode='w+b', prefix='megapy_',
                                     delete=False) as tmp:
        idx = 0
        for _chunk_start, chunk_size in get_chunks(meta["size"]):
            chunk = stream.read(chunk_size)
            chunk = aes.decrypt(chunk)
            tmp.write(chunk)

            encryptor = AES.new(k_str, AES.MODE_CBC, iv_str)
            idx = 0
            for idx in range(0, len(chunk) - 16, 16):
                encryptor.encrypt(chunk[idx:idx + 16])
            idx = idx + 16 if meta["size"] > 16 else 0
            block = chunk[idx:idx + 16]
            if len(block) % 16:
                block += b'\x00' * (16 - len(block) % 16)
            mac_str = mac_encryptor.encrypt(encryptor.encrypt(block))

        tmp_path = tmp.name

    out_path = dest_dir / meta["name"]
    shutil.move(tmp_path, str(out_path))
    log.info("Saved → %s (%.1f MB)", out_path, meta["size"] / 1e6)
    return out_path


def _mega_download_sync(link: str, dest_dir: Path) -> list:
    """
    Blocking download of a Mega file OR folder link.

    Always returns a LIST of Paths:
      - Single file link  → list with 1 Path
      - Folder link       → list with N Paths (one per file in the folder)
    """
    m = _login_mega()
    log.info("Processing: %s", link)

    if _FOLDER_RE.search(link):
        # ── Folder link ───────────────────────────────────────────────────
        folder_id, folder_key, subfolder_id = _parse_folder_url(link)
        log.info("Folder link detected  folder_id=%s  subfolder=%s",
                 folder_id, subfolder_id)

        files_meta = _list_public_folder_files(folder_id, folder_key,
                                                subfolder_id)
        if not files_meta:
            raise ValueError(
                "No downloadable files found in this folder.\n"
                "The folder may be empty, or the link may be a sub-folder "
                "link — try the root folder link instead.")

        log.info("Found %d file(s) in folder", len(files_meta))
        paths = []
        for meta in files_meta:
            p = _download_single_folder_file(meta, folder_id, dest_dir)
            paths.append(p)
        return paths

    else:
        # ── Single file link ──────────────────────────────────────────────
        downloaded = m.download_url(link, dest_path=str(dest_dir))
        path = Path(downloaded)
        log.info("File download complete → %s (%.1f MB)",
                 path.name, path.stat().st_size / 1e6)
        return [path]


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Pyrogram bot
# ─────────────────────────────────────────────────────────────────────────────

app = Client(
    "mega_bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    # Force the session .db file into the OS temp directory, which is
    # guaranteed writable on every host we've deployed to (local Windows
    # venv, Hugging Face Docker container running as non-root, etc.) —
    # avoids permission errors like "unable to open database file" if the
    # app's working directory isn't writable by the process user.
    # tempfile.gettempdir() resolves correctly on both Windows and Linux.
    workdir=os.environ.get("PYROGRAM_WORKDIR", tempfile.gettempdir()),
)


def is_allowed(message: Message) -> bool:
    """Return True only for messages from the authorised user."""
    return message.from_user is not None and message.from_user.id == ALLOWED_USER_ID


# ── /start ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    if not is_allowed(message):
        return
    await message.reply_text(
        "👋 **Mega.nz Bot** is online and ready!\n\n"
        "Paste any **mega.nz** link — file or folder — and I'll download "
        "everything and send it back to you as uncompressed documents.\n\n"
        f"Daily quota: **{DAILY_LIMIT}** files (resets at midnight).\n"
        "Use /stats to check remaining quota, or /help for more info."
    )


# ── /help ────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message):
    if not is_allowed(message):
        return
    await message.reply_text(
        "**How to use:**\n"
        "1. Copy a `mega.nz` share link (file **or** folder).\n"
        "2. Paste it in this chat.\n"
        "3. I'll download and upload everything back to you — "
        "   no re-compression, full original quality.\n\n"
        "**Commands:**\n"
        "/stats — see today's quota usage\n"
        "/help  — this message\n\n"
        "**Limits:**\n"
        f"• Max **{DAILY_LIMIT}** files per day (folders count per-file)\n"
        "• Up to **2 GB** per individual file (Telegram MTProto limit)\n"
        "• Files deleted from server immediately after upload ✅"
    )


# ── /stats ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("stats"))
async def cmd_stats(client: Client, message: Message):
    if not is_allowed(message):
        return
    data      = _load_quota()
    remaining = max(0, DAILY_LIMIT - data["used"])
    await message.reply_text(
        f"📊 **Daily Quota**\n\n"
        f"• Used today : **{data['used']} / {DAILY_LIMIT}**\n"
        f"• Remaining  : **{remaining}**\n"
        f"• Resets at  : **midnight (server time)**\n"
        f"• Today's date : `{data['date']}`"
    )


# ── Mega link handler ─────────────────────────────────────────────────────────

@app.on_message(filters.text & ~filters.command(["start", "help", "stats"]))
async def handle_message(client: Client, message: Message):
    # ── Privacy check ─────────────────────────────────────────────────────
    if not is_allowed(message):
        log.warning("Ignored message from user %s",
                    message.from_user.id if message.from_user else "unknown")
        return

    # ── Find Mega link ─────────────────────────────────────────────────────
    links = MEGA_LINK_RE.findall(message.text)
    if not links:
        await message.reply_text(
            "⚠️ No Mega.nz link detected.\n"
            "Please paste a valid `mega.nz` URL."
        )
        return

    link = links[0]
    is_folder = bool(_FOLDER_RE.search(link))

    # ── Quota check ───────────────────────────────────────────────────────
    if quota_remaining() == 0:
        await message.reply_text(
            "🚫 **Daily quota exhausted.**\n"
            f"You've used all {DAILY_LIMIT} downloads for today.\n"
            "The counter resets automatically at midnight."
        )
        return

    # ── Start processing ──────────────────────────────────────────────────
    link_type  = "📂 folder" if is_folder else "📄 file"
    status_msg = await message.reply_text(
        f"⏳ Detected {link_type} link. **Downloading from Mega.nz…**\n"
        "(this may take a while for large files)"
    )
    local_files = []

    try:
        # Run blocking download in thread pool so the bot stays responsive
        loop = asyncio.get_event_loop()
        local_files = await loop.run_in_executor(
            None, _mega_download_sync, link, DOWNLOAD_DIR
        )

        total_count = len(local_files)
        await status_msg.edit_text(
            f"✅ Downloaded **{total_count}** file(s). "
            f"⬆️ Uploading to Telegram…"
        )

        # Upload each file individually
        for idx, local_file in enumerate(local_files, 1):
            file_size_mb = local_file.stat().st_size / 1e6

            # Telegram hard limit: 2 GB
            if local_file.stat().st_size > 2 * 1024 ** 3:
                await message.reply_text(
                    f"❌ **{local_file.name}** is larger than 2 GB — "
                    "Telegram cannot accept it even via MTProto. Skipping."
                )
                continue

            # Check and consume quota slot
            if not quota_increment():
                await message.reply_text(
                    "🚫 Daily quota reached mid-folder. "
                    "Remaining files skipped. Try again tomorrow."
                )
                break

            await status_msg.edit_text(
                f"⬆️ Uploading {idx}/{total_count}: "
                f"**{local_file.name}** ({file_size_mb:.1f} MB)…"
            )

            await client.send_document(
                chat_id=message.chat.id,
                document=str(local_file),
                file_name=local_file.name,
                caption=f"📁 `{local_file.name}`\n({file_size_mb:.1f} MB)",
                progress=_upload_progress,
                progress_args=(status_msg, idx, total_count, local_file.name),
            )

        remaining = quota_remaining()
        await status_msg.edit_text(
            f"✅ **All done!** {total_count} file(s) uploaded.\n"
            f"Remaining quota today: **{remaining}** / {DAILY_LIMIT}"
        )

    except Exception as exc:
        log.exception("Error processing link %s", link)
        await status_msg.edit_text(
            f"❌ **Error:** `{type(exc).__name__}: {exc}`\n\n"
            "Please check the link and try again."
        )

    finally:
        # AGGRESSIVE CLEANUP: delete every local file whether upload worked or not
        for local_file in local_files:
            if local_file.exists():
                try:
                    local_file.unlink()
                    log.info("🗑️  Deleted: %s", local_file)
                except OSError as e:
                    log.error("Failed to delete %s: %s", local_file, e)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Upload progress callback
# ─────────────────────────────────────────────────────────────────────────────

async def _upload_progress(current: int, total: int, status_msg: Message,
                            idx: int, total_count: int, fname: str):
    """Edit the status message every 10% to show upload progress."""
    if total == 0:
        return
    pct = current * 100 // total
    if pct % 10 == 0:
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        try:
            await status_msg.edit_text(
                f"⬆️ Uploading {idx}/{total_count}: **{fname}**\n"
                f"`[{bar}]` {pct}%\n"
                f"{current / 1e6:.1f} MB / {total / 1e6:.1f} MB"
            )
        except Exception:
            pass  # Ignore flood-wait / message-not-modified errors


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Entry point
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# 5.  Entry point
# ─────────────────────────────────────────────────────────────────────────────
#
# Hugging Face Spaces expects a long-running WEB process listening on a port.
# Our actual bot (Pyrogram) has its own internal event loop, so we run it on
# a background thread and give Spaces a minimal Gradio page to satisfy its
# "is this Space alive" health check. This is the platform's intended usage
# pattern — not a workaround — Spaces are designed to host arbitrary
# long-running Python processes behind a small web UI.
#
# Locally (python bot.py on your own machine) this still works identically:
# Gradio just opens http://localhost:7860 instead.

def _run_bot_blocking():
    """Run the Pyrogram bot — this call blocks forever until the bot stops.

    Runs on its own thread, so it needs its own asyncio event loop
    (the main thread's loop is reserved for Gradio).
    """
    asyncio.set_event_loop(asyncio.new_event_loop())
    log.info("Starting Mega Bot — authorised user: %d", ALLOWED_USER_ID)
    log.info("Daily limit: %d | Download dir: %s", DAILY_LIMIT, DOWNLOAD_DIR)
    app.run()


if __name__ == "__main__":
    import threading

    # ── Run the Telegram bot on a background thread ────────────────────────
    bot_thread = threading.Thread(target=_run_bot_blocking, daemon=True)
    bot_thread.start()

    # ── Run a minimal Gradio page on the main thread ────────────────────────
    # This satisfies Hugging Face Spaces' requirement for a listening web
    # process, and gives you a simple status page if you ever open the URL.
    #
    # NOTE: the gradio import and the demo.launch() call are wrapped in
    # SEPARATE try/except blocks. This matters for debugging: if we only had
    # one try/except ImportError around both, any OTHER error thrown by
    # launch() (e.g. a permissions error writing Gradio's cache) would be
    # silently mislabeled as "Gradio not installed" in the logs, hiding the
    # real problem.
    try:
        import gradio as gr
        gradio_available = True
    except ImportError:
        gradio_available = False
        log.warning("gradio package not installed — running bot without web UI.")

    if gradio_available:
        try:
            with gr.Blocks(title="Mega Bot Status") as demo:
                gr.Markdown(
                    "## 🤖 Mega.nz Telegram Bot — Running\n\n"
                    "This Space keeps your private Telegram bot online 24/7.\n"
                    "There is no public interface here — interact with the bot "
                    "directly in Telegram."
                )

            demo.queue().launch(
                server_name="0.0.0.0",
                server_port=int(os.environ.get("PORT", 7860)),
            )
        except Exception:
            # Log the REAL error instead of silently falling through, so
            # future failures are diagnosable from the Space's Logs tab.
            log.exception(
                "Gradio web UI failed to launch — bot continues running "
                "on its background thread regardless."
            )
            bot_thread.join()
    else:
        # Gradio not installed — just keep the main thread alive so the
        # bot thread keeps running.
        bot_thread.join()
