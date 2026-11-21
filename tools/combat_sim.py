"""Scenario checks (no real opponents, crude physics):
  DUEL  one static enemy ~90-150 m away that shoots back (100 dps while it sees us). Runs with peeking on and
        off and reports damage dealt / taken, so we can see whether peeking stalls our DPS.
  HEAL  we are at 35% HP with 2 enemies in sight. Seconds until we break line of sight to both.
  FARM  no enemies, 4 ground + 2 rooftop crates nearby. Crates destroyed, cubes collected in 60 s.
python3 tools/combat_sim.py"""
import gzip, json, math, os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as B  # noqa: E402

lines = gzip.open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt").read().splitlines()
hello = json.loads(lines[0])
base = json.loads(lines[1])
LCFG = dict(hello["config"]["weapons"]["laser"])
GUN_HIT = 0.6
TIMES = []


def free_spot(rng, g, kind=None):
    while True:
        x, y = rng.uniform(-300, 350), rng.uniform(-200, 250)
        v = g.NAV[g.ncell(x, y)]
        if v and (kind is None or v == kind):
            return x, y


def ring_spot(rng, g, x, y, dmin, dmax, roof=False):
    for _ in range(2000):
        a = rng.uniform(0, 2 * math.pi); d = rng.uniform(dmin, dmax)
        ex, ey = x + math.cos(a) * d, y + math.sin(a) * d
        c = g.cell(ex, ey)
        if c < 0:
            continue
        if roof:
            h = g.T[c] - g.G[c]
            if 5 < h < 35 and g.B[c] <= g.G[c] + 1:
                return ex, ey, g.T[c]
        elif g.NAV[g.ncell(ex, ey)] == 1:
            return ex, ey, g.ground(ex, ey)
    return None


class Body:
    """Our mech: horizontal walk/boost with wall sliding, jump + jets, energy."""
    def __init__(self, g, x, y):
        self.g, self.x, self.y = g, x, y
        self.z = g.ground(x, y); self.vz = 0.0; self.energy = 100.0; self.grounded = True
        self.aim = [0.0, 0.0]

    def step(self, a, slow=1.0):
        g = self.g
        # torso slews toward the commanded aim (turn_rate 4 rad/s yaw, pitch_rate 3 rad/s) over 0.1 s
        dy = B.wrap(a["aim"][0] - self.aim[0])
        self.aim[0] = B.wrap(self.aim[0] + max(-0.4, min(0.4, dy)))
        dp = a["aim"][1] - self.aim[1]
        self.aim[1] += max(-0.3, min(0.3, dp))
        mx, my = a["move"]
        sp = (19.5 if (a["boost"] and self.grounded) else 11.7) * slow
        spent = False
        for sub in range(2):
            dt = 0.05
            if a["jump"] and self.grounded and sub == 0:
                self.vz, self.grounded = 11.0, False
            if not self.grounded:
                self.vz -= 22 * dt
                if a["boost"] and self.energy > 0:
                    self.vz = min(10.0, self.vz + 34 * dt); self.energy -= 15 * dt; spent = True
                self.z += self.vz * dt
                surf = g.surface(self.x, self.y, self.z)
                if self.z <= surf:
                    self.z, self.vz, self.grounded = surf, 0.0, True
            elif a["boost"] and math.hypot(mx, my) > 0.1:
                self.energy -= 17 * dt; spent = True
            nx_, ny_ = self.x + mx * sp * dt, self.y + my * sp * dt
            if not g.blocked_at(nx_, ny_, self.z, 1.0):
                self.x, self.y = nx_, ny_
            elif not g.blocked_at(nx_, self.y, self.z, 1.0):
                self.x = nx_
            elif not g.blocked_at(self.x, ny_, self.z, 1.0):
                self.y = ny_
            if self.grounded:
                s_ = g.surface(self.x, self.y, self.z)
                if s_ < self.z - 1.0:
                    self.grounded = False
                else:
                    self.z = s_
        if not spent:
            self.energy = min(100.0, self.energy + 1.8)


def make_obs(k, body, hp, weapons, mechs, crates, cubes, zone, events):
    obs = json.loads(json.dumps(base))
    obs.update(tick=k * 2, t=k * 0.1, mechs=mechs, projectiles=[], events=events, sounds=[], pickups=[],
               cubes=cubes, crates=crates, drops=[], zone=zone, offer=None, alive=16)
    obs["self"].update(pos=[body.x, body.y, body.z], vel=[0, 0, 0], aim=[round(body.aim[0], 4), round(body.aim[1], 4)], hp=hp, max_hp=1400, energy=body.energy,
                       in_zone=True, grounded=body.grounded, weapons=weapons)
    return obs


def act(b, obs):
    t0 = time.perf_counter()
    a = b._act(obs)
    TIMES.append(1000 * (time.perf_counter() - t0))
    return a


def duel(seed, peek_on, enemy_hp, trials=12):
    rng = random.Random(seed)
    out = []
    orig = B.Bot.peek_move
    if not peek_on:
        B.Bot.peek_move = lambda *a, **k: None
    try:
        for _ in range(trials):
            b = B.Bot(hello); g = b.geo
            x, y = free_spot(rng, g, 3)
            e = ring_spot(rng, g, x, y, 90, 150)
            if e is None:
                continue
            ex, ey, ez = e
            body = Body(g, x, y)
            zone = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380,
                    "shrink_start": 999, "shrink_end": 1050, "dps": 20}
            laser = {"kind": "laser", "cooldown": 0.0, "charge": 0.0, "stats": LCFG}
            gun = dict(base["self"]["weapons"][0]); gun.update(ammo=12, reload=0, cooldown=0)
            charge = lcd = 0.0
            ehp, hp = float(enemy_hp), 1400.0
            dealt = taken = 0.0
            kill_t = None
            for k in range(300):
                seen = g.los(ex, ey, ez + 8, body.x, body.y, body.z + 8.8)
                mechs = [{"id": 5, "kind": "mech", "pos": [ex, ey, ez], "vel": [0, 0, 0], "aim": [0, 0],
                          "hp": max(ehp, 1), "max_hp": 1400, "cubes": 0, "weapons": ["gun"], "charging": False}]
                events = []
                if seen and k % 2 == 0:   # ~150 dps while it can see us (house-bot gun)
                    events.append({"type": "damaged", "amount": 30, "weapon": "gun", "by": 5,
                                   "bearing": math.atan2(ey - body.y, ex - body.x)})
                    hp -= 30; taken += 30
                laser.update(charge=round(charge, 2), cooldown=max(0.0, round(lcd, 2)))
                a = act(b, make_obs(k, body, hp, [gun, laser], mechs, [], [], zone, events))
                vis_shot = g.los(body.x, body.y, body.z + 7, ex, ey, ez + 4.5)
                # gun: one shot per 0.24 s while held, 12-round mag, 2.4 s reload
                if a["fire"][0] and gun["reload"] <= 0 and gun["ammo"] > 0:
                    n = 0.1 / 0.24
                    gun["ammo"] -= 1 if rng.random() < n * 2 else 0
                    if vis_shot:
                        dealt += 45 * GUN_HIT * n; ehp -= 45 * GUN_HIT * n
                    if gun["ammo"] <= 0:
                        gun["reload"] = 2.4
                if gun["reload"] > 0:
                    gun["reload"] -= 0.1
                    if gun["reload"] <= 0:
                        gun.update(reload=0, ammo=12)
                if a.get("reload") and gun["reload"] <= 0 and gun["ammo"] < 12:
                    gun["reload"] = 2.4
                held = a["fire"][1]
                if held and lcd <= 0:
                    charge = min(charge + 0.1, LCFG["charge"] + LCFG["hold_s"])
                elif charge >= LCFG["min_charge"]:
                    if vis_shot:
                        dmg = 315 * min(1.0, 0.35 + 0.65 * charge / LCFG["charge"])
                        dealt += dmg; ehp -= dmg
                    charge, lcd = 0.0, LCFG["cooldown"]
                elif not held:
                    charge = 0.0
                lcd -= 0.1
                body.step(a, 0.5 if charge > 0 else 1.0)
                if ehp <= 0 and kill_t is None:
                    kill_t = k * 0.1
                    break
                if hp <= 0:
                    break
            out.append((dealt, taken, kill_t))
    finally:
        B.Bot.peek_move = orig
    return out


def heal(seed, trials=20):
    rng = random.Random(seed)
    res = []
    for _ in range(trials):
        b = B.Bot(hello); g = b.geo
        x, y = free_spot(rng, g, 3)
        ens = [ring_spot(rng, g, x, y, 90, 150) for _ in range(2)]
        if None in ens:
            continue
        body = Body(g, x, y)
        zone = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380,
                "shrink_start": 999, "shrink_end": 1050, "dps": 20}
        gun = dict(base["self"]["weapons"][0]); gun.update(ammo=12, reload=0, cooldown=0)
        hidden_at = None
        for k in range(300):
            mechs = [{"id": 5 + i, "kind": "mech", "pos": [ex, ey, ez], "vel": [0, 0, 0], "aim": [0, 0], "hp": 1400,
                      "max_hp": 1400, "cubes": 0, "weapons": ["gun"], "charging": False} for i, (ex, ey, ez) in enumerate(ens)]
            seen = [g.los(ex, ey, ez + 8, body.x, body.y, body.z + 8.8) for ex, ey, ez in ens]
            events = [{"type": "damaged", "amount": 45, "weapon": "gun", "by": 5 + i,
                       "bearing": math.atan2(ens[i][1] - body.y, ens[i][0] - body.x)}
                      for i, s_ in enumerate(seen) if s_ and k % 3 == 0]
            a = act(b, make_obs(k, body, 490, [gun, None], mechs, [], [], zone, events))
            body.step(a)
            if hidden_at is None and not any(seen):
                hidden_at = k * 0.1
        res.append(hidden_at)
    return res


def farm(seed, trials=10):
    rng = random.Random(seed)
    res = []
    for _ in range(trials):
        b = B.Bot(hello); g = b.geo
        x, y = free_spot(rng, g, 1)
        crates = []
        for i in range(4):
            p = ring_spot(rng, g, x, y, 30, 140)
            if p:
                crates.append({"id": 1000 + i, "pos": list(p), "hp": 300.0, "roof": False})
        for i in range(2):
            p = ring_spot(rng, g, x, y, 30, 140, roof=True)
            if p:
                crates.append({"id": 1010 + i, "pos": list(p), "hp": 300.0, "roof": True})
        body = Body(g, x, y)
        zone = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380,
                "shrink_start": 999, "shrink_end": 1050, "dps": 20}
        gun = dict(base["self"]["weapons"][0]); gun.update(ammo=12, reload=0, cooldown=0)
        cubes, got = [], 0
        for k in range(600):
            eye = body.z + 8
            vis_cr = [{"id": c["id"], "pos": c["pos"], "hp": round(c["hp"])} for c in crates if c["hp"] > 0 and
                      math.hypot(c["pos"][0] - body.x, c["pos"][1] - body.y) < 200 and
                      g.los(body.x, body.y, eye, c["pos"][0], c["pos"][1], c["pos"][2] + 0.8)]
            vis_cu = [cu for cu in cubes if math.hypot(cu["pos"][0] - body.x, cu["pos"][1] - body.y) < 200]
            a = act(b, make_obs(k, body, 1400, [gun, None], [], vis_cr, vis_cu, zone, []))
            if a["fire"][0]:
                for c in crates:
                    if c["hp"] > 0 and g.los(body.x, body.y, body.z + 7, c["pos"][0], c["pos"][1], c["pos"][2] + 0.8):
                        ay = math.atan2(c["pos"][1] - body.y, c["pos"][0] - body.x)
                        if abs(B.wrap(ay - a["aim"][0])) < 0.05:
                            c["hp"] -= 45 * 0.8 * 0.1 / 0.24
                            if c["hp"] <= 0:
                                cubes.append({"id": 5000 + c["id"], "pos": list(c["pos"])})
                            break
            body.step(a)
            for cu in list(cubes):
                if math.hypot(cu["pos"][0] - body.x, cu["pos"][1] - body.y) < 3.5 and abs(cu["pos"][2] - body.z) < 4:
                    cubes.remove(cu); got += 1
        dead = [c for c in crates if c["hp"] <= 0]
        res.append((len(crates), len(dead), sum(1 for c in crates if c["roof"]), sum(1 for c in dead if c["roof"]), got))
    return res


if __name__ == "__main__":
    for ehp in (1400, 300):
        for on in (True, False):
            r = duel(11, on, ehp)
            d = sum(v[0] for v in r) / len(r); tk = sum(v[1] for v in r) / len(r)
            kills = [v[2] for v in r if v[2] is not None]
            kt = f"{sum(kills)/len(kills):.1f}s" if kills else "-"
            print(f"DUEL enemy_hp={ehp:4d} peek={'on ' if on else 'off'}: dealt {d:6.0f}  taken {tk:6.0f}  "
                  f"ratio {d/max(tk,1):.2f}  kills {len(kills)}/{len(r)} avg time-to-kill {kt}")
    h = heal(3)
    ok = [v for v in h if v is not None]
    print(f"HEAL: hidden in {len(ok)}/{len(h)} trials, median {sorted(ok)[len(ok)//2]:.1f}s, worst {max(ok):.1f}s")
    f = farm(5)
    tot = [sum(v[i] for v in f) for i in range(5)]
    print(f"FARM (60 s): crates destroyed {tot[1]}/{tot[0]} (rooftop {tot[3]}/{tot[2]}), cubes collected {tot[4]}")
    s = sorted(TIMES)
    print(f"tick time over all scenarios: p50 {s[len(s)//2]:.2f}  p99 {s[int(len(s)*.99)]:.2f}  max {s[-1]:.2f} ms")
