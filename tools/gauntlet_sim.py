"""Practice-proxy gauntlet WITH farming: 15 house-bot hunters + crates, drones, cubes and upgrades.

Score = hunters killed before we die (house bots never fight each other, so that is our placement points).

Hunters (weakest first): 4 easy (gun, 3x spread, slow reactions), 4 medium (gun 1.6x spread + laser 60% hit),
4 hard (gun + laser 85%), 3 expert (gun 0.8x spread + laser 90%, 1600 HP). One hunts at a time, 2 after 4 kills,
3 after 8, all after 240 s. They path to us with A*, strafe at 70-110 m, jet up to us, lead our lagged velocity,
regen 45 HP/s after 5 s. A dead hunter drops 2 cubes and its laser.

World around our spawn (real-map density within ~400 m): 23 crates (300 HP, a quarter on roofs; 1 cube + 50%
laser/lightning drop), 8 drones (350 HP, stationary, shoot 25 dmg / 1 s / 90 m/s at whoever hit them in the last
10 s or anything within 45 m in sight; 2 cubes), 3 lasers + 2 lightning on the floor.

Our mech: real obs (vision cone + 45 m proximity + 5 s memory for mechs/drones/crates/cubes), torso slew, gun
bullets, hitscan laser, rough lightning. Cubes: +4% damage and max HP each (cap 10, heals the difference).
Upgrade offers at 1, 3, 6, 10, 15, 21 cubes with 3 random eligible choices; modelled effects: plating, armor,
nanites, gun_mag, gun_damage, gun_rate, laser_damage, laser_charge (others are no-ops here).

python3 tools/gauntlet_sim.py [--bot path] [--trials N] [--seed S] [Name=value class overrides...]
"""
import argparse, gzip, importlib.util, json, math, os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from geo import AStar  # noqa: E402
from combat_sim import Body, ring_spot  # noqa: E402

lines = gzip.open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt").read().splitlines()
HELLO = json.loads(lines[0])
BASE = json.loads(lines[1])
CFG = HELLO["config"]
GUN = CFG["weapons"]["gun"]
LAS = CFG["weapons"]["laser"]
LIT = CFG["weapons"]["lightning"]
MOR = CFG["weapons"]["mortar"]
ZCFG = CFG["zone"]
SECONDARIES = ("laser", "lightning", "mortar")   # house bots' second weapon (loot weights 1:1:1)
UPG = {u["id"]: u for u in CFG["upgrades"]}
THRESH = CFG["cubes"]["upgrade_thresholds"]
DT = 0.05
TIERS = [("easy", 3.0, 0.7, False, 0.0, 1400.0)] * 4 + [("medium", 1.6, 0.45, True, 0.6, 1400.0)] * 4 + \
        [("hard", 1.0, 0.3, True, 0.85, 1400.0)] * 4 + [("expert", 0.8, 0.2, True, 0.9, 1600.0)] * 3


# difficulty knobs (calibration): hunter aim spread x, reaction x, laser hit +, HP x, loot fraction
DIFF = {"spread": 1.0, "react": 1.0, "lhit": 0.0, "hp": 1.0, "loot": 1.0}


def WANT(t, kills, left):
    return left if t > 240 else (1 if kills < 4 else 2 if kills < 8 else 3)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def load_bot(path):
    spec = importlib.util.spec_from_file_location("botmod_%d" % abs(hash(path)), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = ROOT
    return mod


def seg_hits_cyl(p0, p1, c, r=2.5, h=9.0):
    (x0, y0, z0), (x1, y1, z1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    u = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((c[0] - x0) * dx + (c[1] - y0) * dy) / L2))
    px, py, pz = x0 + dx * u, y0 + dy * u, z0 + (z1 - z0) * u
    return math.hypot(px - c[0], py - c[1]) < r and c[2] <= pz <= c[2] + h


def dist_word(d):
    return "near" if d < 60 else "mid" if d < 160 else "far"


class Unit:
    def __init__(self, g, x, y, uid, hp):
        self.body = Body(g, x, y)
        self.id, self.hp, self.max_hp = uid, hp, hp
        self.ammo, self.reload, self.cd = 12, 0.0, 0.0
        self.last_combat = -99.0
        self.vel = (0.0, 0.0)
        self.aim = [0.0, 0.0]

    @property
    def pos(self):
        return self.body.x, self.body.y, self.body.z


class Me(Unit):
    def __init__(self, g, x, y):
        super().__init__(g, x, y, 3, 1400.0)
        self.cubes = 0
        self.taken = {}
        self.offers = []
        self.offer_n = 0
        self.thr_i = 0

    def lvl(self, u):
        return self.taken.get(u, 0)

    def dmg_mult(self):
        return 1 + 0.04 * min(self.cubes, 10)

    def calc_max_hp(self):
        return 1400.0 * (1 + 0.04 * min(self.cubes, 10)) * (1.15 ** self.lvl("plating"))

    def armor(self):
        return 0.08 * self.lvl("armor")

    def regen_rate(self):
        return 45.0 * (1.6 ** self.lvl("nanites"))

    def regen_delay(self):
        return 5.0 - 1.5 * self.lvl("nanites")

    def mag(self):
        return 12 + 8 * self.lvl("gun_mag")

    def reload_s(self):
        return GUN["reload"] * (0.8 ** self.lvl("gun_mag"))

    def gun_cd(self):
        return GUN["cooldown"] * (0.87 ** self.lvl("gun_rate"))

    def gun_dmg(self):
        return GUN["damage"] * (1.15 ** self.lvl("gun_damage")) * self.dmg_mult()

    def laser_dmg(self):
        return LAS["damage"] * (1.18 ** self.lvl("laser_damage")) * self.dmg_mult()

    def laser_charge(self):
        return LAS["charge"] * (0.75 ** self.lvl("laser_charge"))


def make_zone(rng):
    """Real phase schedule from config; each next circle sits inside the current one."""
    phases = []
    c, r, t0 = list(ZCFG["start_center"]), ZCFG["start_radius"], 0.0
    for ph in ZCFG["phases"]:
        nr = ph["radius"]
        a = rng.uniform(0, 2 * math.pi); off = rng.uniform(0, max(r - nr, 0) * 0.8)
        nc = [c[0] + math.cos(a) * off, c[1] + math.sin(a) * off]
        ss = t0 + ph["hold_s"]
        phases.append({"center": c, "radius": r, "next_center": nc, "next_radius": nr,
                       "shrink_start": ss, "shrink_end": ss + ph["shrink_s"], "dps": ph["dps"]})
        c, r, t0 = nc, nr, ss + ph["shrink_s"]
    return phases


def zone_now(phases, t):
    for ph in phases:
        if t < ph["shrink_end"]:
            break
    f = 0.0 if t <= ph["shrink_start"] else min(1.0, (t - ph["shrink_start"]) / (ph["shrink_end"] - ph["shrink_start"]))
    cx = ph["center"][0] + (ph["next_center"][0] - ph["center"][0]) * f
    cy = ph["center"][1] + (ph["next_center"][1] - ph["center"][1]) * f
    return ph, cx, cy, ph["radius"] + (ph["next_radius"] - ph["radius"]) * f


def mortar_vel(sx, sy, sz, tx, ty, tz, high):
    v, gg = MOR["speed"], MOR["gravity"]
    D = math.hypot(tx - sx, ty - sy); h = tz - sz
    disc = v ** 4 - gg * (gg * D * D + 2 * h * v * v)
    if disc < 0 or D < 1:
        return None
    th = math.atan((v * v + math.sqrt(disc)) / (gg * D)) if high else math.atan((v * v - math.sqrt(disc)) / (gg * D))
    yaw = math.atan2(ty - sy, tx - sx)
    return [v * math.cos(th) * math.cos(yaw), v * math.cos(th) * math.sin(yaw), v * math.sin(th)]


def run_trial(mod, g, rng, max_t=420.0, zone_on=True, secondaries=SECONDARIES):
    b = mod.Bot(HELLO)
    zphases = make_zone(rng)
    while True:
        x, y = rng.uniform(-250, 300), rng.uniform(-150, 200)
        if g.NAV[g.ncell(x, y)] == 1 and (not zone_on or math.hypot(x - zphases[0]["next_center"][0],
                                                                     y - zphases[0]["next_center"][1]) < 420):
            break
    me = Me(g, x, y)
    # ---- world
    crates, npcs, pickups, cubes = [], [], [], []
    for i in range(int(round(23 * DIFF["loot"]))):
        p = ring_spot(rng, g, x, y, 25, 400, roof=(i % 4 == 0))
        if p:
            crates.append({"id": 1000 + i, "pos": list(p), "hp": 300.0})
    for i in range(int(round(8 * DIFF["loot"]))):
        p = ring_spot(rng, g, x, y, 50, 400)
        if p:
            npcs.append({"id": 100 + i, "pos": list(p), "hp": 350.0, "cd": 0.0, "aggro": -99.0})
    for i, kd in enumerate(["laser", "laser", "lightning", "lightning"]):
        p = ring_spot(rng, g, x, y, 40, 240)
        if p:
            pickups.append({"id": 8000 + i, "kind": kd, "pos": list(p)})
    p = ring_spot(rng, g, x, y, 40, 200, roof=True)
    if p:
        pickups.append({"id": 8010, "kind": "laser", "pos": list(p)})
    ids = {"cube": 5000, "pk": 8100}

    def drop_cubes(pos, n):
        for _ in range(n):
            cubes.append({"id": ids["cube"], "pos": [pos[0] + rng.uniform(-1.5, 1.5), pos[1] + rng.uniform(-1.5, 1.5), pos[2]]})
            ids["cube"] += 1

    def drop_pickup(pos, kind):
        pickups.append({"id": ids["pk"], "kind": kind, "pos": list(pos)})
        ids["pk"] += 1

    queue = list(TIERS)
    foes, spawned = [], 0

    def spawn(tier):
        name, spm, react_s, hl, lhit, hp0 = tier
        spm, react_s = spm * DIFF["spread"], react_s * DIFF["react"]
        lhit, hp0 = min(0.97, lhit + DIFF["lhit"]), hp0 * DIFF["hp"]
        for _ in range(500):
            a = rng.uniform(0, 2 * math.pi); d = rng.uniform(180, 260)
            ex, ey = me.body.x + math.cos(a) * d, me.body.y + math.sin(a) * d
            if -480 < ex < 520 and -300 < ey < 360 and g.NAV[g.ncell(ex, ey)] == 1:
                f = Unit(g, ex, ey, 20 + spawned, hp0)
                f.sec = rng.choice(secondaries) if hl else None
                f.tier, f.has_laser, f.lhit, f.spm, f.react_s = name, f.sec == "laser", lhit, spm, react_s
                f.energy, f.mcd = 100.0, rng.uniform(0, 2.6)
                f.lcd, f.lcharge, f.seen_since = 0.0, 0.0, None
                f.path, f.path_t, f.pi, f.strafe, f.strafe_t = None, -99, 0, 1, 0
                return f
        return None

    zone = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380,
            "shrink_start": 999, "shrink_end": 1050, "dps": 20}
    in_zone = True
    gun = dict(BASE["self"]["weapons"][0]); gun.update(ammo=12, reload=0, cooldown=0)
    gun["stats"] = dict(gun["stats"])
    slot1 = None
    charge = 0.0
    bullets = []          # [x, y, z, vx, vy, vz, owner, travelled, dmg]
    memory = {}
    vel_hist = []
    events, beams, sounds = [], [], []
    kills, times = 0, []
    laser_t = None
    cubes_at_4th = None
    short_until = -99.0
    shells = []            # [x, y, z, vx, vy, vz, owner]
    zone_deaths = 0
    t = 0.0

    def our_hit(tid, dmg, weapon):
        nonlocal kills
        for f in foes:
            if f.id == tid and f.hp > 0:
                f.hp -= dmg; f.last_combat = t
                events.append({"type": "hit", "target": tid, "amount": round(dmg), "weapon": weapon})
                if f.hp <= 0:
                    kills += 1
                    events.append({"type": "kill", "target": tid})
                    drop_cubes(f.pos, 2)
                    if f.sec:
                        drop_pickup(f.pos, f.sec)
                return
        for n in npcs:
            if n["id"] == tid and n["hp"] > 0:
                n["hp"] -= dmg; n["aggro"] = t
                events.append({"type": "hit", "target": tid, "amount": round(dmg), "weapon": weapon})
                if n["hp"] <= 0:
                    drop_cubes(n["pos"], 2)
                return
        for c in crates:
            if c["id"] == tid and c["hp"] > 0:
                c["hp"] -= dmg
                events.append({"type": "hit", "target": tid, "amount": round(dmg), "weapon": weapon})
                if c["hp"] <= 0:
                    drop_cubes(c["pos"], 1)
                    if rng.random() < 0.5:
                        drop_pickup(c["pos"], rng.choice(["laser", "lightning"]))
                return

    def our_targets():
        out = [(f.id, f.pos, 2.5, 9.0) for f in foes if f.hp > 0]
        out += [(n["id"], n["pos"], 1.8, 3.5) for n in npcs if n["hp"] > 0]
        out += [(c["id"], c["pos"], 1.4, 2.4) for c in crates if c["hp"] > 0]
        return out

    for k in range(int(max_t * 10)):
        t = k * 0.1
        want = WANT(t, kills, len(queue))
        if t >= 5.0:
            while queue and sum(1 for f in foes if f.hp > 0) < want:
                f = spawn(queue.pop(0)); spawned += 1
                if f:
                    foes.append(f)
        alive = [f for f in foes if f.hp > 0]
        if kills >= 4 and cubes_at_4th is None:
            cubes_at_4th = me.cubes
        if not alive and not queue:
            return dict(res="win", kills=kills, t=t, cubes=me.cubes, laser=slot1 is not None and slot1["kind"] == "laser",
                        laser_t=laser_t, c4=cubes_at_4th, times=times, ups=dict(me.taken), zd=zone_deaths,
                        score=sum(1 for f in foes if f.hp <= 0))
        if me.hp <= 0:
            return dict(res="dead", kills=kills, t=t, cubes=me.cubes, laser=slot1 is not None and slot1["kind"] == "laser",
                        laser_t=laser_t, c4=cubes_at_4th, times=times, ups=dict(me.taken), zd=zone_deaths,
                        score=sum(1 for f in foes if f.hp <= 0))
        mx, my, mz = me.pos

        def seen_by_me(px, py, pz, h):
            d = math.hypot(px - mx, py - my)
            ins = d < 45 or (d < 200 and abs(wrap(math.atan2(py - my, px - mx) - me.aim[0])) < math.radians(60))
            return ins and (g.los(mx, my, mz + 8, px, py, pz + h * 0.5) or g.los(mx, my, mz + 8, px, py, pz + h))
        # ---- observation
        mechs = []
        for f in alive:
            fx, fy, fz = f.pos
            vis_now = seen_by_me(fx, fy, fz, 9.0)
            if vis_now:
                memory[f.id] = t
            if t - memory.get(f.id, -99) <= 5.0:
                mechs.append({"id": f.id, "kind": "mech", "pos": [fx, fy, fz], "vel": [f.vel[0], f.vel[1], 0],
                              "aim": [round(f.aim[0], 3), 0.0], "hp": round(f.hp), "max_hp": round(f.max_hp), "cubes": 0,
                              "weapons": ["gun", f.sec] if f.sec else ["gun"],
                              "charging": bool(f.lcharge > 0 and vis_now)})
        for n in npcs:
            if n["hp"] > 0:
                if seen_by_me(*n["pos"], 3.5):
                    memory[n["id"]] = t
                if t - memory.get(n["id"], -99) <= 5.0:
                    mechs.append({"id": n["id"], "kind": "npc", "pos": list(n["pos"]), "vel": [0, 0, 0], "aim": [0, 0],
                                  "hp": round(n["hp"]), "max_hp": 350})
        vis_cr = [{"id": c["id"], "pos": c["pos"], "hp": round(c["hp"])} for c in crates
                  if c["hp"] > 0 and seen_by_me(*c["pos"], 2.4)]
        vis_cu = [cu for cu in cubes if seen_by_me(*cu["pos"], 0.6)]
        vis_pk = [p for p in pickups if seen_by_me(*p["pos"], 0.6)]
        projs = [{"id": i, "kind": "gun", "pos": bl[:3], "vel": bl[3:6]} for i, bl in enumerate(bullets)
                 if bl[6] != me.id and math.hypot(bl[0] - mx, bl[1] - my) < 200]
        projs += [{"id": 900 + i, "kind": "mortar", "pos": sh[:3], "vel": sh[3:6]} for i, sh in enumerate(shells)
                  if math.hypot(sh[0] - mx, sh[1] - my) < 200]
        if zone_on:
            zph, zcx, zcy, zr = zone_now(zphases, t)
            zone = {k_: zph[k_] for k_ in ("center", "radius", "next_center", "next_radius", "shrink_start", "shrink_end", "dps")}
            in_zone = math.hypot(mx - zcx, my - zcy) <= zr
        if slot1:
            slot1["charge"] = round(charge, 2) if slot1["kind"] == "laser" else 0
            slot1["cooldown"] = max(0.0, round(slot1["cooldown"], 2))
            if slot1["kind"] == "laser":
                slot1["stats"] = dict(LAS, damage=round(me.laser_dmg() / me.dmg_mult(), 1), charge=me.laser_charge())
        gun.update(ammo=me.ammo, reload=round(max(me.reload, 0), 2), cooldown=round(max(me.cd, 0), 2))
        gun["stats"].update(mag=me.mag(), reload=me.reload_s(), cooldown=me.gun_cd())
        offer = None
        if me.offers:
            o = me.offers[0]
            offer = {"id": o["id"], "expires": o["exp"], "choices": [{"id": c, "name": c, "desc": ""} for c in o["ch"]]}
        obs = json.loads(json.dumps(BASE))
        obs.update(tick=k * 2, t=round(t, 2), mechs=mechs, projectiles=projs, events=events, sounds=sounds,
                   beams=beams, pickups=vis_pk, cubes=vis_cu, crates=vis_cr, drops=[], zone=zone, offer=offer,
                   alive=1 + len(alive) + len(queue))
        obs["self"].update(pos=[mx, my, mz], vel=[me.vel[0], me.vel[1], 0], aim=[round(me.aim[0], 4), round(me.aim[1], 4)],
                           hp=round(me.hp), max_hp=round(me.max_hp), energy=me.body.energy, in_zone=in_zone,
                           grounded=me.body.grounded, weapons=[gun, slot1], cubes=me.cubes,
                           upgrades=[u for u, n_ in me.taken.items() for _ in range(n_)])
        events, beams, sounds = [], [], []
        t0 = time.perf_counter()
        a = b._act(obs)
        times.append(1000 * (time.perf_counter() - t0))
        # ---- offers
        if me.offers:
            o = me.offers[0]
            pick = a.get("pick")
            if pick is not None or t >= o["exp"]:
                u = o["ch"][pick if isinstance(pick, int) and 0 <= pick < len(o["ch"]) else 0]
                me.taken[u] = me.taken.get(u, 0) + 1
                old = me.max_hp
                me.max_hp = me.calc_max_hp(); me.hp += me.max_hp - old
                events.append({"type": "upgrade", "id": u})
                me.offers.pop(0)
        # ---- take
        tk = a.get("take")
        if tk is not None:
            p = next((q for q in pickups if q["id"] == tk), None)
            if p and math.hypot(p["pos"][0] - mx, p["pos"][1] - my) <= 4.0 and abs(p["pos"][2] - mz) <= 4.0:
                if slot1 is None or a.get("drop") == 1:
                    if slot1 is not None:
                        drop_pickup([mx, my, mz], slot1["kind"])
                    st = LAS if p["kind"] == "laser" else LIT
                    slot1 = {"kind": p["kind"], "cooldown": max(1.0, st.get("cooldown", 1)), "charge": 0.0, "stats": st}
                    pickups.remove(p); charge = 0.0
                    events.append({"type": "took", "id": p["id"], "kind": p["kind"], "slot": 1})
                    if p["kind"] == "laser" and laser_t is None:
                        laser_t = t
        # ---- our torso + weapons
        me.aim[0] = wrap(me.aim[0] + max(-0.4, min(0.4, wrap(a["aim"][0] - me.aim[0]))))
        me.aim[1] += max(-0.3, min(0.3, a["aim"][1] - me.aim[1]))
        dealt = False
        for sub in range(2):
            if a["fire"][0] and me.reload <= 0 and me.cd <= 0 and me.ammo > 0:
                yaw = me.aim[0] + rng.gauss(0, math.radians(GUN["spread_deg"]))
                pit = me.aim[1] + rng.gauss(0, math.radians(GUN["spread_deg"]))
                v = GUN["speed"]
                d3 = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                bullets.append([mx + d3[0] * 3, my + d3[1] * 3, mz + 7 + d3[2] * 3, d3[0] * v, d3[1] * v, d3[2] * v,
                                me.id, 0.0, me.gun_dmg()])
                me.ammo -= 1; me.cd = me.gun_cd()
                if me.ammo == 0:
                    me.reload = me.reload_s()
            me.cd -= DT
            if me.reload > 0:
                me.reload -= DT
                if me.reload <= 0:
                    me.ammo, me.reload = me.mag(), 0.0
        if a.get("reload") and me.reload <= 0 and me.ammo < me.mag():
            me.reload = me.reload_s()
        if slot1 and slot1["kind"] == "laser":
            held = a["fire"][1]
            full = me.laser_charge()
            if held and slot1["cooldown"] <= 0 and t >= short_until:
                charge = min(charge + 0.1, full + LAS["hold_s"])
            elif charge >= LAS["min_charge"]:
                yaw, pit = me.aim
                d3 = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                ox, oy, oz = mx, my, mz + 7
                best = None
                for tid, (tx, ty, tz), rr, hh in our_targets():
                    s_ = (tx - ox) * d3[0] + (ty - oy) * d3[1]
                    if s_ <= 0 or s_ > LAS["range"]:
                        continue
                    hz = oz + d3[2] * s_ / max(math.cos(pit), 1e-3)
                    if seg_hits_cyl((ox, oy, oz), (ox + d3[0] * s_ * 1.02, oy + d3[1] * s_ * 1.02, hz), (tx, ty, tz), rr, hh) \
                            and g.los(ox, oy, oz, tx, ty, min(max(hz, tz), tz + hh)):
                        if best is None or s_ < best[0]:
                            best = (s_, tid)
                if best:
                    dmg = me.laser_dmg() * min(1.0, LAS["min_frac"] + (1 - LAS["min_frac"]) * charge / full)
                    if any(f.id == best[1] and f.sec == "mortar" for f in foes):
                        dmg *= 1 + LAS["cookoff"]
                    our_hit(best[1], dmg, "laser"); dealt = True
                charge, slot1["cooldown"] = 0.0, LAS["cooldown"]
            elif not held:
                charge = 0.0
            slot1["cooldown"] -= 0.1
        elif slot1 and slot1["kind"] == "lightning":
            held = a["fire"][1]
            slot1["cooldown"] -= 0.1
            if held and slot1["cooldown"] <= 0 and me.body.energy > LIT["energy"]:
                me.body.energy -= LIT["energy"] * 0.4
                tg = None
                for tid, (tx, ty, tz), rr, hh in our_targets():
                    d = math.hypot(tx - mx, ty - my)
                    if d < LIT["range"] and abs(wrap(math.atan2(ty - my, tx - mx) - me.aim[0])) < math.radians(LIT["cone_deg"] / 2) \
                            and g.los(mx, my, mz + 7, tx, ty, tz + hh * 0.5) and (tg is None or d < tg[0]):
                        tg = (d, tid)
                if tg:
                    our_hit(tg[1], LIT["damage"] * 0.4 * me.dmg_mult(), "lightning"); dealt = True
        ox_, oy_ = me.body.x, me.body.y
        me.body.step(a, 0.5 if charge > 0 else 1.0)
        me.vel = ((me.body.x - ox_) / 0.1, (me.body.y - oy_) / 0.1)
        vel_hist.append(me.vel)
        mx, my, mz = me.pos
        # ---- cubes
        for cu in list(cubes):
            if math.hypot(cu["pos"][0] - mx, cu["pos"][1] - my) < 3.5 and abs(cu["pos"][2] - mz) < 4.0:
                cubes.remove(cu)
                me.cubes += 1
                old = me.max_hp
                me.max_hp = me.calc_max_hp(); me.hp += me.max_hp - old
                events.append({"type": "pickup", "cubes": me.cubes})
                while me.thr_i < len(THRESH) and me.cubes >= THRESH[me.thr_i]:
                    me.thr_i += 1
                    elig = [u for u, d_ in UPG.items() if me.lvl(u) < d_["max"] and
                            (not d_.get("requires") or d_["requires"] == "gun" or (slot1 and d_["requires"] == slot1["kind"]))]
                    me.offer_n += 1
                    me.offers.append({"id": me.offer_n, "exp": t + 5.0, "ch": rng.sample(elig, 3)})
        # ---- drones
        for n in npcs:
            if n["hp"] <= 0:
                continue
            nx_, ny_, nz_ = n["pos"]
            d = math.hypot(mx - nx_, my - ny_)
            n["cd"] -= 0.1
            angry = t - n["aggro"] < 10.0 and d < 90
            if (angry or d < 45) and n["cd"] <= 0 and g.los(nx_, ny_, nz_ + 3, mx, my, mz + 4.5):
                yaw = math.atan2(my - ny_, mx - nx_) + rng.gauss(0, math.radians(2.5))
                pit = math.atan2(mz + 4.5 - (nz_ + 3), max(d, 1)) + rng.gauss(0, math.radians(2.5))
                v = 90.0
                d3 = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                bullets.append([nx_ + d3[0] * 2, ny_ + d3[1] * 2, nz_ + 3, d3[0] * v, d3[1] * v, d3[2] * v, n["id"], 0.0, 25.0])
                n["cd"] = 1.0
        # ---- hunters
        lagged = vel_hist[-3] if len(vel_hist) >= 3 else (0.0, 0.0)
        for f in alive:
            fx, fy, fz = f.pos
            d = math.hypot(mx - fx, my - fy)
            sees = d < 200 and (g.los(fx, fy, fz + 8, mx, my, mz + 4.5) or g.los(fx, fy, fz + 8, mx, my, mz + 9))
            can_shoot = sees and g.los(fx, fy, fz + 7, mx, my, mz + 4.5)
            brg_to_me = math.atan2(my - fy, mx - fx)
            if sees or f.lcharge > 0:
                f.aim[0] = brg_to_me
            f.seen_since = (f.seen_since if f.seen_since is not None else t) if sees else None
            react = f.seen_since is not None and t - f.seen_since >= f.react_s
            mvx = mvy = 0.0
            if sees and d < 125:
                if t > f.strafe_t:
                    f.strafe = rng.choice((-1, 1)); f.strafe_t = t + rng.uniform(0.6, 1.6)
                ux, uy = (mx - fx) / d, (my - fy) / d
                pref = 30.0 if f.sec == "lightning" else 120.0 if f.sec == "mortar" else 90.0
                radial = max(-1.0, min(1.0, (d - pref) / 40))
                mvx, mvy = ux * radial - uy * f.strafe * 0.8, uy * radial + ux * f.strafe * 0.8
            elif f.sec == "lightning" and d < 125:
                dd = d or 1
                mvx, mvy = (mx - fx) / dd, (my - fy) / dd          # rush straight in
            else:
                if f.path is None or t - f.path_t > 3.0:
                    s_ = AStar(g, (fx, fy), (mx, my))
                    while not s_.step(5000):
                        pass
                    f.path, f.path_t, f.pi = s_.path, t, 0
                if f.path:
                    while f.pi < len(f.path) - 1 and math.hypot(f.path[f.pi][0] - fx, f.path[f.pi][1] - fy) < 5:
                        f.pi += 1
                    wx, wy = f.path[f.pi]
                    dd = math.hypot(wx - fx, wy - fy) or 1
                    mvx, mvy = (wx - fx) / dd, (wy - fy) / dd
            fox, foy = f.body.x, f.body.y
            climb = (mz > fz + 3.0 and d < 30.0) or (math.hypot(*f.vel) < 1.0 and (mvx or mvy))
            if climb:
                if not (mvx or mvy):
                    mvx, mvy = (mx - fx) / (d or 1), (my - fy) / (d or 1)
                f.body.step({"move": [mvx, mvy], "boost": f.body.energy > 1, "jump": f.body.grounded, "aim": [0, 0]})
            else:
                f.body.step({"move": [mvx, mvy], "boost": False, "jump": False, "aim": [0, 0]}, 0.5 if f.lcharge > 0 else 1.0)
            f.vel = ((f.body.x - fox) / 0.1, (f.body.y - foy) / 0.1)
            if f.has_laser:
                f.lcd -= 0.1
                if f.lcharge > 0:
                    f.lcharge += 0.1
                    if (f.lcharge >= LAS["charge"] and can_shoot) or f.lcharge >= LAS["charge"] + LAS["hold_s"]:
                        if can_shoot and rng.random() < f.lhit and d < LAS["range"]:
                            rear = abs(wrap((brg_to_me + math.pi) - me.aim[0])) > math.pi / 2
                            dmg = LAS["damage"] * (1.5 if rear else 1.0) * (1 - me.armor())
                            me.hp -= dmg; me.last_combat = t
                            events.append({"type": "damaged", "amount": dmg, "weapon": "laser", "by": f.id,
                                           "bearing": brg_to_me + math.pi, "rear": rear})
                        if t - memory.get(f.id, -99) < 0.2:
                            beams.append({"by": f.id, "kind": "laser", "path": [[fx, fy, fz + 7], [mx, my, mz + 4.5]], "age": 0.05})
                        if d < 380:
                            sounds.append({"kind": "laser", "bearing": wrap(brg_to_me + math.pi), "dist": dist_word(d), "age": 0.05})
                        f.lcharge, f.lcd = 0.0, LAS["cooldown"]
                elif f.lcd <= 0 and sees and react and d < LAS["range"]:
                    f.lcharge = 0.1
            if f.sec == "lightning":
                if d < LIT["range"] and can_shoot and react and f.energy > LIT["energy"]:
                    rear = abs(wrap((brg_to_me + math.pi) - me.aim[0])) > math.pi / 2
                    dmg = LIT["damage"] * 0.4 * (1.5 if rear else 1.0) * (1 - me.armor())
                    me.hp -= dmg; me.last_combat = t; f.energy -= LIT["energy"] * 0.4
                    events.append({"type": "damaged", "amount": dmg, "weapon": "lightning", "by": f.id,
                                   "bearing": brg_to_me + math.pi, "rear": rear})
                    if slot1 and slot1["kind"] == "laser":
                        if charge > 0:
                            events.append({"type": "effect", "effect": "short", "by": f.id})
                        charge, short_until = 0.0, t + LIT["short_s"]
                    if d < 160:
                        sounds.append({"kind": "lightning", "bearing": wrap(brg_to_me + math.pi), "dist": dist_word(d), "age": 0.05})
                else:
                    f.energy = min(100.0, f.energy + 1.8)
            elif f.sec == "mortar":
                f.mcd -= 0.1
                if f.mcd <= 0 and 40 < d < 220 and t >= 5.0:
                    lx, ly = mx + lagged[0] * 2.5 + rng.gauss(0, 4), my + lagged[1] * 2.5 + rng.gauss(0, 4)
                    vel = mortar_vel(fx, fy, fz + 9.5, lx, ly, g.ground(lx, ly), high=not can_shoot)
                    if vel:
                        shells.append([fx, fy, fz + 9.5, vel[0], vel[1], vel[2], f.id])
                        f.mcd = MOR["cooldown"]
                        if d < 420:
                            sounds.append({"kind": "mortar", "bearing": wrap(brg_to_me + math.pi), "dist": dist_word(d), "age": 0.05})
            for sub in range(2):
                if f.reload <= 0 and f.cd <= 0 and f.ammo > 0 and can_shoot and react and d < GUN["range"] * 0.85:
                    tt = d / GUN["speed"]
                    px, py = mx + lagged[0] * tt, my + lagged[1] * tt
                    yaw = math.atan2(py - fy, px - fx) + rng.gauss(0, math.radians(GUN["spread_deg"] * f.spm))
                    hd = math.hypot(px - fx, py - fy)
                    pit = math.atan2(mz + 4.5 - (fz + 7), hd) + rng.gauss(0, math.radians(GUN["spread_deg"] * f.spm))
                    v = GUN["speed"]
                    d3 = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                    bullets.append([fx + d3[0] * 3, fy + d3[1] * 3, fz + 7 + d3[2] * 3, d3[0] * v, d3[1] * v, d3[2] * v, f.id, 0.0, 45.0])
                    f.ammo -= 1; f.cd = GUN["cooldown"]
                    if f.ammo == 0:
                        f.reload = GUN["reload"]
                f.cd -= DT
                if f.reload > 0:
                    f.reload -= DT
                    if f.reload <= 0:
                        f.ammo, f.reload = 12, 0.0
        # ---- bullets
        for sub in range(2):
            keep = []
            tgts = None
            for bl in bullets:
                p0 = (bl[0], bl[1], bl[2])
                p1 = (bl[0] + bl[3] * DT, bl[1] + bl[4] * DT, bl[2] + bl[5] * DT)
                bl[7] += math.sqrt(bl[3] ** 2 + bl[4] ** 2 + bl[5] ** 2) * DT
                if bl[6] == me.id:
                    if tgts is None:
                        tgts = our_targets()
                    hit = None
                    for tid, tp, rr, hh in tgts:
                        if seg_hits_cyl(p0, p1, tp, rr, hh) and g.los(p0[0], p0[1], p0[2], tp[0], tp[1], tp[2] + hh * 0.5):
                            hit = tid
                            break
                    if hit is not None:
                        our_hit(hit, bl[8], "gun"); dealt = True
                        tgts = None
                        continue
                elif seg_hits_cyl(p0, p1, me.pos) and g.los(p0[0], p0[1], p0[2], mx, my, mz + 4.5):
                    brg = math.atan2(-bl[4], -bl[3])
                    rear = abs(wrap(brg - me.aim[0])) > math.pi / 2
                    dmg = bl[8] * (1.5 if rear else 1.0) * (1 - me.armor())
                    me.hp -= dmg; me.last_combat = t
                    events.append({"type": "damaged", "amount": dmg, "weapon": "npc" if bl[6] >= 100 else "gun",
                                   "by": bl[6], "bearing": brg, "rear": rear})
                    continue
                if bl[7] > GUN["range"] or not g.los(p0[0], p0[1], p0[2], p1[0], p1[1], p1[2]):
                    continue
                bl[0], bl[1], bl[2] = p1
                keep.append(bl)
            bullets = keep
        # ---- mortar shells
        keep = []
        for sh in shells:
            boom = None
            for sub in range(2):
                p0 = (sh[0], sh[1], sh[2])
                sh[5] -= MOR["gravity"] * DT
                p1 = (sh[0] + sh[3] * DT, sh[1] + sh[4] * DT, sh[2] + sh[5] * DT)
                if p1[2] <= g.surface(p1[0], p1[1], p1[2] + 1) or not g.los(p0[0], p0[1], p0[2], p1[0], p1[1], p1[2]) \
                        or seg_hits_cyl(p0, p1, me.pos):
                    boom = p1 if g.los(p0[0], p0[1], p0[2], p1[0], p1[1], p1[2]) else p0
                    break
                sh[0], sh[1], sh[2] = p1
            if boom is None:
                keep.append(sh)
                continue
            dx_, dy_ = mx - boom[0], my - boom[1]
            hd = math.hypot(dx_, dy_)
            dist3 = math.hypot(hd, (mz + 4.5) - boom[2])
            if hd < 2.5 and mz - 1 <= boom[2] <= mz + 10:
                dmg, fall = MOR["damage"], 1.0
            elif dist3 <= MOR["splash_radius"] and g.los(boom[0], boom[1], boom[2] + 0.5, mx, my, mz + 4.5):
                fall = 1 - (1 - MOR["splash_edge"]) * dist3 / MOR["splash_radius"]
                dmg = MOR["splash"] * fall
            else:
                dmg = 0.0
            if dmg > 0:
                brg = math.atan2(boom[1] - my, boom[0] - mx)
                rear = abs(wrap(brg - me.aim[0])) > math.pi / 2
                dmg *= (1.5 if rear else 1.0) * (1 - me.armor())
                me.hp -= dmg; me.last_combat = t
                me.body.energy = max(0.0, me.body.energy - MOR["drain"] * fall)
                events.append({"type": "damaged", "amount": dmg, "weapon": "mortar", "by": sh[6], "bearing": brg, "rear": rear})
            sounds.append({"kind": "explosion", "bearing": math.atan2(boom[1] - my, boom[0] - mx), "dist": dist_word(hd), "age": 0.05})
        shells = keep
        # ---- zone
        if zone_on:
            zph, zcx, zcy, zr = zone_now(zphases, t)
            out_me = math.hypot(mx - zcx, my - zcy) > zr
            if out_me:
                me.hp -= zph["dps"] * 0.1
                me.last_combat = t
                events.append({"type": "damaged", "amount": zph["dps"] * 0.1, "weapon": "zone", "by": None, "bearing": None})
            for f in foes:
                if f.hp > 0 and math.hypot(f.body.x - zcx, f.body.y - zcy) > zr:
                    f.hp -= zph["dps"] * 0.1; f.last_combat = t
                    if f.hp <= 0:
                        zone_deaths += 1
        if dealt:
            me.last_combat = t
        if t - me.last_combat > me.regen_delay():
            me.hp = min(me.max_hp, me.hp + me.regen_rate() * 0.1)
        for f in foes:
            if f.hp > 0 and t - f.last_combat > 5.0:
                f.hp = min(f.max_hp, f.hp + 4.5)
    return dict(res="timeout", kills=kills, t=t, cubes=me.cubes, laser=slot1 is not None and slot1["kind"] == "laser",
                laser_t=laser_t, c4=cubes_at_4th, times=times, ups=dict(me.taken), zd=zone_deaths,
                        score=sum(1 for f in foes if f.hp <= 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=os.path.join(ROOT, "bot.py"))
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--seed", type=int, default=31)
    ap.add_argument("--name", default="")
    ap.add_argument("--no-zone", action="store_true")
    ap.add_argument("--laser-only", action="store_true", help="hunters' secondary is always a laser (pre-v7 sim)")
    for k_ in DIFF:
        ap.add_argument("--" + k_, type=float, default=None)
    ap.add_argument("--cal", action="store_true",
                    help="live-calibrated preset: laser hunters, spread x0.8, reaction x0.8, laser hit +0.1, HP x1.4 "
                         "(fits our 6 live scores: RMSE ~0.8-1.1, corr ~0.75-0.9)")
    ap.add_argument("overrides", nargs="*")
    args = ap.parse_args()
    if args.cal:
        args.laser_only = True
        DIFF.update(spread=0.8, react=0.8, lhit=0.1, hp=1.4)
    for k_ in DIFF:
        if getattr(args, k_) is not None:
            DIFF[k_] = getattr(args, k_)
    mod = load_bot(args.bot)
    for kv in args.overrides:
        k, v = kv.split("=")
        setattr(mod.Bot, k, type(getattr(mod.Bot, k))(eval(v)))
    g = mod.Geo(os.path.join(ROOT, "data", "map.bin"))
    rng = random.Random(args.seed)
    kw = {"zone_on": not args.no_zone, "secondaries": ("laser",) if args.laser_only else SECONDARIES}
    if args.no_zone:
        kw["max_t"] = 300.0
    res = [run_trial(mod, g, rng, **kw) for _ in range(args.trials)]
    ks = sorted(r["kills"] for r in res)
    sc = sorted(r["score"] for r in res)
    n = len(res)
    c4 = [r["c4"] for r in res if r["c4"] is not None]
    allt = sorted(x for r in res for x in r["times"])
    label = args.name or os.path.basename(args.bot)
    print(f"{label:24s} SCORE {sum(sc)/n:.2f} (kills {sum(ks)/n:.2f} + zone {sum(r['zd'] for r in res)/n:.2f}) median {sc[n//2]} | cubes {sum(r['cubes'] for r in res)/n:4.1f}"
          f" (at 4th kill {sum(c4)/max(len(c4),1):4.1f}) | laser {sum(r['laser'] for r in res)}/{n} | surv {sum(r['t'] for r in res)/n:4.0f}s"
          f" | tick p99 {allt[int(len(allt)*.99)]:.2f} max {allt[-1]:.1f} ms")


if __name__ == "__main__":
    main()
