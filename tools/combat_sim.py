"""Scenario checks for v2 (no real opponents, crude physics):
  PEEK: we hold a laser, one static enemy ~120 m away. Count peek phase changes, laser shots, shots with LOS.
  HEAL: we are at 35% HP with 2 enemies in sight. Measure how fast we break line of sight to both.
python3 tools/combat_sim.py"""
import gzip, json, math, os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as B  # noqa: E402

lines = gzip.open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt").read().splitlines()
hello = json.loads(lines[0])
base = json.loads(lines[1])
LCFG = dict(hello["config"]["weapons"]["laser"])
rng = random.Random(3)


def free_spot(g, kind=None):
    while True:
        x, y = rng.uniform(-300, 350), rng.uniform(-200, 250)
        v = g.NAV[g.ncell(x, y)]
        if v and (kind is None or v == kind):
            return x, y


def enemy_at(g, x, y, dmin, dmax):
    for _ in range(400):
        a = rng.uniform(0, 2 * math.pi); d = rng.uniform(dmin, dmax)
        ex, ey = x + math.cos(a) * d, y + math.sin(a) * d
        if g.NAV[g.ncell(ex, ey)] == 1:
            return ex, ey
    return None


def run(scenario, trials=10):
    stats = []
    times = []
    for _ in range(trials):
        b = B.Bot(hello); g = b.geo
        x, y = free_spot(g, 3)
        z = g.ground(x, y)
        n_en = 1 if scenario == "PEEK" else 2
        ens = []
        for i in range(n_en):
            e = enemy_at(g, x, y, 90, 150)
            if e is None:
                break
            ens.append(e)
        if len(ens) < n_en:
            continue
        zone = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380,
                "shrink_start": 999, "shrink_end": 1050, "dps": 20}
        hp = 1400 if scenario == "PEEK" else 490
        laser = {"kind": "laser", "cooldown": 0.0, "charge": 0.0, "stats": LCFG}
        charge = 0.0; lcd = 0.0
        shots = shots_los = 0
        phases = []
        hidden_at = None
        for k in range(300):  # 30 s
            t = k * 0.1
            mechs = []
            for i, (ex, ey) in enumerate(ens):
                ez = g.ground(ex, ey)
                mechs.append({"id": 5 + i, "kind": "mech", "pos": [ex, ey, ez], "vel": [0, 0, 0], "aim": [0, 0],
                              "hp": 1400, "max_hp": 1400, "cubes": 0, "weapons": ["gun"], "charging": False})
            seen = [g.los(ex, ey, g.ground(ex, ey) + 8, x, y, z + 8.8) for ex, ey in ens]
            events = []
            if scenario == "HEAL" and any(seen) and k % 3 == 0:
                for i, s_ in enumerate(seen):
                    if s_:
                        events.append({"type": "damaged", "amount": 45, "weapon": "gun", "by": 5 + i,
                                       "bearing": math.atan2(ens[i][1] - y, ens[i][0] - x)})
            laser.update(charge=round(charge, 2), cooldown=max(0.0, round(lcd, 2)))
            obs = json.loads(json.dumps(base))
            obs.update(tick=k * 2, t=t, mechs=mechs, projectiles=[], events=events, sounds=[], pickups=[],
                       cubes=[], crates=[], drops=[], zone=zone, offer=None, alive=16)
            obs["self"].update(pos=[x, y, z], vel=[0, 0, 0], hp=hp, max_hp=1400, energy=100, in_zone=True,
                               grounded=True, weapons=[obs["self"]["weapons"][0], laser if scenario == "PEEK" else None])
            obs["self"]["weapons"][0].update(ammo=12, reload=0, cooldown=0)
            t0 = time.perf_counter()
            a = b._act(obs)
            times.append(1000 * (time.perf_counter() - t0))
            if b.peek_active:
                ph = b.peek["phase"]
                if not phases or phases[-1] != ph:
                    phases.append(ph)
            # laser model
            if scenario == "PEEK":
                held = a["fire"][1]
                if held and lcd <= 0:
                    charge = min(charge + 0.1, LCFG["charge"] + LCFG["hold_s"])
                elif charge >= LCFG["min_charge"]:
                    shots += 1
                    ex, ey = ens[0]
                    if g.los(x, y, z + 7, ex, ey, g.ground(ex, ey) + 4.5):
                        shots_los += 1
                    charge, lcd = 0.0, LCFG["cooldown"]
                elif not held:
                    charge = 0.0
                lcd -= 0.1
            # movement (half speed while charging)
            mx, my = a["move"]
            sp = (19.5 if a["boost"] else 11.7) * (0.5 if charge > 0 else 1.0)
            nx_, ny_ = x + mx * sp * 0.1, y + my * sp * 0.1
            if not g.blocked_at(nx_, ny_, z, 1.0):
                x, y = nx_, ny_
            elif not g.blocked_at(nx_, y, z, 1.0):
                x = nx_
            elif not g.blocked_at(x, ny_, z, 1.0):
                y = ny_
            z = g.surface(x, y, z)
            if scenario == "HEAL" and hidden_at is None and not any(
                    g.los(ex, ey, g.ground(ex, ey) + 8, x, y, z + 8.8) for ex, ey in ens):
                hidden_at = t
        if scenario == "PEEK":
            stats.append((len(phases), shots, shots_los))
        else:
            stats.append(hidden_at)
    return stats, times


for sc in ("PEEK", "HEAL"):
    st, tm = run(sc)
    s = sorted(tm)
    print(f"{sc}: p50 {s[len(s)//2]:.2f} p99 {s[int(len(s)*.99)]:.2f} max {s[-1]:.2f} ms")
    if sc == "PEEK":
        for ph, sh, sl in st:
            print(f"  peek phase changes {ph:3d}  laser shots {sh:2d}  with LOS {sl:2d}")
    else:
        print("  seconds to break LOS from both:", [None if h is None else round(h, 1) for h in st])
