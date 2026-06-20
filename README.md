---
title: Mega Bot
emoji: 🤖
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Mega.nz Telegram Bot

Private bot — downloads Mega.nz links and re-uploads them to Telegram
as uncompressed files. Only responds to the configured `ALLOWED_USER_ID`.

This Space runs via a custom Dockerfile (needed to resolve a dependency
conflict between `mega.py` and modern Python's `asyncio` — see Dockerfile
comments for details). There is no public interface — interact with the
bot directly in Telegram.
