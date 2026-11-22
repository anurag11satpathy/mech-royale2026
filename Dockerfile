# Build from this folder: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
# Branch `pacifist`: entrypoint is the storm-win bot (storm.py, STORM_MODE toggle inside); main runs bot.py (v7).
FROM python:3.13.1-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
COPY mechfight.py geo.py bot.py storm.py /bot/
COPY data/map.bin /bot/data/map.bin
RUN python3 -m compileall -q /bot && chmod -R a+rX /bot
ENTRYPOINT ["python3", "-u", "/bot/storm.py"]
