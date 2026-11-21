# Builders Buildathon 2026 — Mech Royale (Anurag)

## Project Overview
- **Goal:** Code the AI for a mech in a 16-way 3D battle royale on the OSU campus. Last mech standing wins.
- **Metric (Higher is Better):** Placement points = mean of `(16 - place)` across all matches (15 for 1st, 0 for 16th). **Placement counts, NOT kills** — survival and positioning beat aggressive trading.
- **Scores to Beat:** Baseline is 1.15. Top leaderboard is ~6.3+.
- **Submission Limit:** 4 submissions/hour. Deadline: Sunday, Nov 22, 2026 at 11:00 AM ET.

## Strict Runtime & Submission Rules (Never Violate)
- **Protocol:** Speak `mechfight/1` JSON lines on `stdin`/`stdout` (see `protocol.md`). All debug/log prints MUST go to `stderr`.
- **Latency:** 10 ticks/sec — **strict 50 ms limit** per observation. Late answers are skipped (mech stops firing and freezes after 1 s). Aim for <10 ms average so variance never causes timeouts.
- **Hardware Limits (Run):** 1 CPU, 512 MB RAM, read-only map at `/arena`, NO network, NO root (`uid 65534` — install packages system-wide in Dockerfile), NO disk writes outside `/tmp` (64 MB).
- **Build & Repo Limits:** `linux/amd64`, max 10 min build, pin all package versions, public GitHub repo max 20 MB total, `Dockerfile` at repo root.
- **Local Verification:** Always build the Docker image and pass `check/check.py` before committing or submitting.