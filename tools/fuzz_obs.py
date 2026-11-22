"""Fuzz Bot.act() with randomized, protocol-valid observations (incl. optional fields missing) and report exceptions.
In a real match an exception makes the wrapper repeat the last action silently, so these would be invisible live.
python3 tools/fuzz_obs.py [N]"""
import collections, copy, gzip, json, math, os, random, sys, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as B  # noqa: E402

L = gzip.open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt").read().splitlines()
HELLO = json.loads(L[0])
BASE = [json.loads(l) for l in L[1:] if l.startswith('{"type": "obs"') or '"obs"' in l[:30]]
W = HELLO["config"]["weapons"]
rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 1)


def weapon(kind):
    w = {"kind": kind, "cooldown": rng.choice([0, 0, 0.3, 1.2]), "stats": dict(W[kind])}
    if kind == "gun":
        w.update(ammo=rng.choice([0, 1, 5, 12]), reload=rng.choice([0, 0, 1.1]))
    if kind == "laser":
        w.update(charge=rng.choice([0, 0, 0.2, 0.75, 2.5]))
    return w


def rand_mech(i, me):
    k = rng.random()
    m = {"id": i, "kind": "mech" if i < 100 else "npc",
         "pos": [me[0] + rng.uniform(-250, 250), me[1] + rng.uniform(-250, 250), me[2] + rng.uniform(-5, 40)],
         "vel": [rng.uniform(-20, 20), rng.uniform(-20, 20), rng.uniform(-10, 10)], "aim": [rng.uniform(-3.2, 3.2), 0.1]}
    if rng.random() < 0.8:
        m.update(hp=rng.uniform(1, 2000), max_hp=rng.choice([350, 1400, 1932]))
    if m["kind"] == "mech":
        if rng.random() < 0.8:
            m["cubes"] = rng.randint(0, 25)
        if rng.random() < 0.8:
            m["weapons"] = rng.sample(["gun", "laser", "lightning", "mortar"], rng.randint(0, 2))
        if rng.random() < 0.8:
            m["charging"] = rng.random() < 0.3
    for f in ("vel", "aim"):
        if rng.random() < 0.05:
            m.pop(f)
    return m


def rand_obs(base, t):
    o = copy.deepcopy(base)
    o["t"], o["tick"] = t, int(t * 20)
    me = o["self"]
    x, y = rng.uniform(-530, 570), rng.uniform(-350, 400)   # includes outside-grid edges
    me["pos"] = [x, y, rng.choice([0, -5, 10, 40, 75])]
    me["hp"] = rng.uniform(1, 2500); me["max_hp"] = rng.choice([1400, 1610, 2600])
    me["energy"] = rng.uniform(0, 130); me["grounded"] = rng.random() < 0.8
    me["cubes"] = rng.randint(0, 30); me["in_zone"] = rng.random() < 0.85
    me["upgrades"] = rng.sample([u["id"] for u in HELLO["config"]["upgrades"]], rng.randint(0, 4))
    s0 = rng.choice(["gun", "gun", "gun", "laser", "lightning", "mortar", None])
    s1 = rng.choice([None, "laser", "lightning", "mortar"])
    if s0 == s1:
        s1 = None
    me["weapons"] = [weapon(s0) if s0 else None, weapon(s1) if s1 else None]
    o["mechs"] = [rand_mech(i, me["pos"]) for i in rng.sample(range(16), rng.randint(0, 6))] + \
                 [rand_mech(100 + i, me["pos"]) for i in range(rng.randint(0, 3))]
    o["projectiles"] = [{"id": i, "kind": rng.choice(["gun", "mortar", "npc"]), "pos": [x + rng.uniform(-80, 80), y + rng.uniform(-80, 80), rng.uniform(0, 30)],
                         "vel": [rng.uniform(-250, 250), rng.uniform(-250, 250), rng.uniform(-30, 30)]} for i in range(rng.randint(0, 6))]
    o["crates"] = [{"id": 1000 + i, "pos": [x + rng.uniform(-200, 200), y + rng.uniform(-200, 200), rng.uniform(-5, 40)], "hp": rng.uniform(1, 300)} for i in range(rng.randint(0, 4))]
    o["cubes"] = [{"id": 5000 + i, "pos": [x + rng.uniform(-100, 100), y + rng.uniform(-100, 100), 0]} for i in range(rng.randint(0, 4))]
    o["pickups"] = [{"id": 8000 + i, "kind": rng.choice(["laser", "lightning", "mortar", "gun"]), "pos": [x + rng.uniform(-6, 6), y + rng.uniform(-6, 6), me["pos"][2]]} for i in range(rng.randint(0, 3))]
    o["sounds"] = [{"kind": rng.choice(["gun", "laser", "laser_charge", "lightning", "mortar", "explosion", "step", "boost", "jet", "death", "crate", "npc_gun", "reload", "drop"]),
                    "bearing": rng.uniform(-3.2, 3.2), "dist": rng.choice(["near", "mid", "far"]), "age": rng.uniform(0, 0.1)} for _ in range(rng.randint(0, 5))]
    o["beams"] = [{"by": rng.randint(0, 15), "kind": rng.choice(["laser", "lightning"]), "path": [[x, y, 7], [x + 10, y, 5]], "age": 0.05} for _ in range(rng.randint(0, 2))]
    ev = []
    for _ in range(rng.randint(0, 5)):
        k = rng.choice(["damaged", "damaged_zone", "hit", "effect", "drop", "kill", "pickup", "upgrade", "took", "dropped", "take_failed"])
        if k == "damaged":
            ev.append({"type": "damaged", "amount": rng.uniform(1, 400), "weapon": rng.choice(["gun", "laser", "lightning", "mortar", "npc"]), "bearing": rng.uniform(-3.2, 3.2), "by": rng.randint(0, 120), "rear": rng.random() < 0.3})
        elif k == "damaged_zone":
            ev.append({"type": "damaged", "amount": 20, "weapon": "zone", "bearing": None, "by": None})
        elif k == "hit":
            ev.append({"type": "hit", "target": rng.randint(0, 1100), "amount": 45, "weapon": "gun"})
        elif k == "effect":
            ev.append({"type": "effect", "effect": rng.choice(["cookoff", "short", "concuss"]), "by": rng.randint(0, 15)})
        elif k == "drop":
            ev.append({"type": "drop", "id": 20000, "cubes": 3})
        elif k == "kill":
            ev.append({"type": "kill", "target": rng.randint(0, 15)})
        elif k == "pickup":
            ev.append({"type": "pickup", "cubes": rng.randint(1, 20)})
        elif k == "upgrade":
            ev.append({"type": "upgrade", "id": "armor"})
        elif k == "took":
            ev.append({"type": "took", "id": 8000, "kind": "laser", "slot": 1})
        elif k == "dropped":
            ev.append({"type": "dropped", "id": 8001, "kind": "gun", "slot": 0})
        else:
            ev.append({"type": "take_failed", "id": 8000, "reason": rng.choice(["gone", "range", "have", "full", "busy"])})
    o["events"] = ev
    r = rng.choice([560, 380, 240, 140, 70, 25, 0])
    nr = max(0, r - rng.choice([0, 45, 140, 25]))
    o["zone"] = {"center": [rng.uniform(-200, 200), rng.uniform(-200, 200)], "radius": r, "next_center": [rng.uniform(-200, 200), rng.uniform(-200, 200)],
                 "next_radius": nr, "shrink_start": t + rng.uniform(-30, 30), "shrink_end": t + rng.uniform(0, 60), "dps": rng.choice([20, 60, 260])}
    o["drops"] = [{"id": 20000 + i, "pos": [x + rng.uniform(-400, 400), y + rng.uniform(-400, 400), rng.uniform(-5, 5)], "radius": 8, "lands_in": rng.choice([0, 0, 5, 20]),
                   "capture_s": 4, "progress": rng.choice([0, 1.5]), "holder": rng.choice([None, None, 3, 7])} for i in range(rng.randint(0, 2))]
    o["offer"] = None if rng.random() < 0.7 else {"id": rng.randint(1, 9), "expires": t + 5, "choices": [{"id": c, "name": c, "desc": ""} for c in rng.sample([u["id"] for u in HELLO["config"]["upgrades"]] + ["mystery_new"], 3)]}
    o["alive"] = rng.randint(1, 16)
    return o


N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
errs = collections.Counter(); samples = {}
b = B.Bot(HELLO)
t = 0.0
for i in range(N):
    if i % 300 == 0:
        b = B.Bot(HELLO); t = 0.0          # fresh bot now and then (state accumulates realistically in between)
    t += 0.1
    o = rand_obs(rng.choice(BASE), round(t, 1))
    try:
        a = b._act(o)
        json.dumps(a)
        assert a["tick"] == o["tick"] and len(a["move"]) == 2 and len(a["aim"]) == 2
        assert all(math.isfinite(v) for v in a["move"] + a["aim"]), "non-finite action"
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)[-1]
        key = f"{type(e).__name__}: {e} @ {os.path.basename(tb.filename)}:{tb.lineno} `{tb.line}`"
        errs[key] += 1
        samples.setdefault(key, i)
print(f"fuzzed {N} observations: {sum(errs.values())} exceptions ({100*sum(errs.values())/N:.2f}%) in {len(errs)} distinct places")
for k, v in errs.most_common():
    print(f"  {v:5d}x  {k}")
