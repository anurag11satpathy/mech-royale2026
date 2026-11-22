# Build from this folder: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM python:3.13.1-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# numpy (pinned, prebuilt manylinux wheel) for vectorised LOS: tactical grid + danger-weighted A*. System-wide install.
RUN pip install --no-cache-dir numpy==2.1.3
WORKDIR /bot
COPY mechfight.py geo.py bot.py /bot/
COPY data/map.bin /bot/data/map.bin
RUN python3 -m compileall -q /bot && chmod -R a+rX /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
