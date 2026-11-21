# Build from this folder: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM python:3.13.1-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
# Only the runtime files: stdlib-only bot + precomputed map grid (data/map.bin, ~100 KiB).
COPY mechfight.py geo.py bot.py /bot/
COPY data/map.bin /bot/data/map.bin
# Byte-compile at build time (the runtime disk is read-only), and make it readable for uid 65534.
RUN python3 -m compileall -q /bot && chmod -R a+rX /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
