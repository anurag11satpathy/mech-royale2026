"""Feed check/stream.jsonl.gz straight into Bot.act() (no subprocess) to catch exceptions and profile.
Also stress-tests LOS, cover search and worst-case A* on random points.  python3 tools/replay_test.py"""
import gzip, json, math, os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as B  # noqa: E402
from geo import AStar  # noqa: E402

lines = gzip.open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt").read().splitlines()
hello = json.loads(lines[0])
t0 = time.perf_counter()
b = B.Bot(hello)
print(f"init {1000*(time.perf_counter()-t0):.0f} ms")
times, states = [], {}
for ln in lines[1:]:
    m = json.loads(ln)
    if m.get("type") != "obs":
        continue
    t = time.perf_counter()
    a = b._act(m)
    times.append(1000 * (time.perf_counter() - t))
    states[b.state] = states.get(b.state, 0) + 1
    json.dumps(a)
s = sorted(times)
print(f"replay {len(s)} obs: p50 {s[len(s)//2]:.2f} p99 {s[int(len(s)*.99)]:.2f} max {s[-1]:.2f} ms  states {states}")

g = b.geo
rng = random.Random(1)
def rp():
    return rng.uniform(-450, 480), rng.uniform(-280, 330)
t = time.perf_counter(); vis = 0
for _ in range(1000):
    (ax, ay), (bx, by) = rp(), rp()
    vis += g.los(ax, ay, g.ground(ax, ay) + 8, bx, by, g.ground(bx, by) + 4.5)
print(f"LOS (random, avg {sum(1 for _ in [0])}x): {1000*(time.perf_counter()-t)/1000:.3f} ms each, {vis/10:.0f}% clear")
worst = 0; ticks_worst = 0
for _ in range(40):
    a = AStar(g, rp(), rp()); n = 0
    while True:
        t = time.perf_counter(); done = a.step(450); dt = 1000*(time.perf_counter()-t)
        worst = max(worst, dt); n += 1
        if done: break
    ticks_worst = max(ticks_worst, n)
print(f"A* step(450): worst {worst:.2f} ms per tick, worst {ticks_worst} ticks to finish")
b.t = 30; b.zone = json.loads(lines[100])["zone"]
t = time.perf_counter()
for _ in range(50):
    x, y = rp(); b.find_cover(x, y, g.ground(x, y), [(x + 80, y + 20, 0), (x - 60, y + 70, 0), (x, y - 90, 0)])
print(f"find_cover 3 threats: {1000*(time.perf_counter()-t)/50:.2f} ms avg")
