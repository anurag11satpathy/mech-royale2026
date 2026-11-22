"""1v2 / 1v3 squad simulator: pursuing, strafing house-bot stand-ins vs our bot.

Enemies: mixed starting HP (45-100% of 1400), gun (45 dmg / 0.24 s, 12-round mag, 2.4 s reload, 252 m/s, 0.6 deg
spread) and, for 60% of them, a laser (0.75 s charge visible to us as `charging` + aim, 315 dmg hitscan, 1.65 s
cooldown, ~85% hit when they have line of sight at release, auto-fire after a 2 s full hold; half speed while
charging). They know where we are (squad vision), path to us with A*, strafe at 70-110 m when they see us, lead
bullets with our velocity from 0.2 s ago, and regen 45 HP/s after 5 s. Bullets are real projectiles (miss, hit walls,
x1.5 from behind). Our side gets real obs (120-degree vision cone + 45 m proximity + 5 s memory, projectiles,
beams, laser sounds, damaged/hit/kill events, visible pickups); our torso slews at 4 rad/s; our gun fires real
bullets along the actual aim; our laser is hitscan along the actual aim; lightning is modelled roughly.

Scenarios:
  laser  we start with gun + laser; hunters spawn 160-260 m out and set off at 5-15 s
  loot   we start with the gun only; 3 lasers (one on a roof) + 2 lightning pickups nearby; hunters set off at 12-25 s
Enemies need 0.3 s of seeing us before they open fire (reaction time).
Enemies jump + jet toward us when we are above them (within 30 m) or when they are stuck.

python3 tools/squad_sim.py [--bot path/to/bot.py] [--trials N]
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
DT = 0.05


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def load_bot(path):
    spec = importlib.util.spec_from_file_location("botmod_%d" % abs(hash(path)), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = ROOT  # find data/map.bin in the repo
    return mod


class Mech:
    def __init__(self, g, x, y, mid, hp=1400.0):
        self.body = Body(g, x, y)
        self.id = mid
        self.hp = hp
        self.max_hp = max(hp, 1400.0)
        self.ammo, self.reload, self.cd = 12, 0.0, 0.0
        self.last_combat = -99.0
        self.vel = (0.0, 0.0)
        self.aim = [0.0, 0.0]

    @property
    def pos(self):
        b = self.body
        return b.x, b.y, b.z


def regen(m, t):
    if t - m.last_combat > 5.0 and m.hp > 0:
        m.hp = min(getattr(m, "max_hp", 1400.0), m.hp + 45 * 0.1)


def seg_hits_cyl(p0, p1, c, r=2.5, h=9.0):
    (x0, y0, z0), (x1, y1, z1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    u = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((c[0] - x0) * dx + (c[1] - y0) * dy) / L2))
    px, py, pz = x0 + dx * u, y0 + dy * u, z0 + (z1 - z0) * u
    return math.hypot(px - c[0], py - c[1]) < r and c[2] <= pz <= c[2] + h


# gauntlet tiers (easy -> expert): spread multiplier, reaction s, laser carrier?, laser hit prob, hp
TIERS = [("easy", 3.0, 0.7, False, 0.0, 1400.0)] * 4 + [("medium", 1.6, 0.45, True, 0.6, 1400.0)] * 4 + \
        [("hard", 1.0, 0.3, True, 0.85, 1400.0)] * 4 + [("expert", 0.8, 0.2, True, 0.9, 1600.0)] * 3


OUR_DMG = 1.0   # our damage multiplier (cubes x upgrades) for scaling experiments
OUR_HP = 1400.0  # our max HP (cubes, plating)
OUR_MAG = 12     # our gun magazine (drum mag upgrade: 20)


def WANT(t, kills, left):
    """Hunters active at once: one early, more as we thin them out, all of them late."""
    return left if t > 240 else (1 if kills < 4 else 2 if kills < 8 else 3)


def dist_word(d):
    return "near" if d < 60 else "mid" if d < 160 else "far"


def run_trial(mod, g, rng, n_enemies, scenario, max_t=120.0):
    b = mod.Bot(HELLO)
    while True:
        x, y = rng.uniform(-300, 350), rng.uniform(-200, 250)
        if g.NAV[g.ncell(x, y)] == 1:
            break
    me = Mech(g, x, y, 3, hp=OUR_HP)
    me.ammo = OUR_MAG
    base_a = rng.uniform(0, 2 * math.pi)
    dmin, dmax = 160, 260
    foes = []
    for i in range(n_enemies):
        for _ in range(500):
            a = base_a + i * (2 * math.pi / n_enemies) * rng.uniform(0.35, 0.6) + rng.uniform(-0.3, 0.3)
            d = rng.uniform(dmin, dmax)
            ex, ey = x + math.cos(a) * d, y + math.sin(a) * d
            if g.NAV[g.ncell(ex, ey)] == 1:
                f = Mech(g, ex, ey, 5 + i, hp=1400.0 * rng.uniform(0.45, 1.0))
                f.has_laser = rng.random() < 0.6
                f.lcd, f.lcharge = 0.0, 0.0
                f.path, f.path_t, f.pi, f.strafe, f.strafe_t = None, -99, 0, 1, 0
                f.hunt_t = rng.uniform(12, 25) if scenario == "loot" else rng.uniform(5, 15)
                f.seen_since = None
                foes.append(f)
                break
    def spawn(tier, i):
        name, spm, react_s, hl, lhit, hp0 = tier
        for _ in range(500):
            a = rng.uniform(0, 2 * math.pi); d = rng.uniform(180, 260)
            ex, ey = me.body.x + math.cos(a) * d, me.body.y + math.sin(a) * d
            if -480 < ex < 520 and -300 < ey < 360 and g.NAV[g.ncell(ex, ey)] == 1:
                f = Mech(g, ex, ey, 20 + i, hp=hp0)
                f.has_laser, f.lhit, f.spm, f.react_s = hl, lhit, spm, react_s
                f.lcd, f.lcharge = 0.0, 0.0
                f.path, f.path_t, f.pi, f.strafe, f.strafe_t = None, -99, 0, 1, 0
                f.hunt_t, f.seen_since = 0.0, None
                return f
        return None
    queue = list(TIERS) if scenario == "gauntlet" else []
    spawned = 0
    if scenario == "gauntlet":
        foes = []
    for f in foes:
        f.lhit, f.spm, f.react_s = 0.85, 1.0, 0.3
    pickups = []
    if scenario in ("loot", "gauntlet"):
        kinds = ["laser", "laser", "lightning", "lightning"]
        for i, kd in enumerate(kinds):
            p = ring_spot(rng, g, x, y, 40, 240)
            if p:
                pickups.append({"id": 8000 + i, "kind": kd, "pos": list(p)})
        p = ring_spot(rng, g, x, y, 40, 200, roof=True)
        if p:
            pickups.append({"id": 8010, "kind": "laser", "pos": list(p)})
    zone = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380,
            "shrink_start": 999, "shrink_end": 1050, "dps": 20}
    gun = dict(BASE["self"]["weapons"][0]); gun.update(ammo=12, reload=0, cooldown=0)
    slot1 = None          # {"kind", "cooldown", "charge", "stats"}
    if scenario == "laser":
        slot1 = {"kind": "laser", "cooldown": 0.0, "charge": 0.0, "stats": LAS}
    if scenario == "gauntlet":
        max_t = 300.0
    laser_t = 0.0 if slot1 else None
    charge = 0.0
    bullets = []
    memory = {}
    vel_hist = []
    events, beams, sounds = [], [], []
    kills = 0
    times = []
    t = 0.0
    for k in range(int(max_t * 10)):
        t = k * 0.1
        mx, my, mz = me.pos
        if scenario == "gauntlet":
            # one hunter early, more as we thin them out, all of them late; weakest first
            want = WANT(t, kills, len(queue))
            if t >= 5.0:
                while queue and sum(1 for f in foes if f.hp > 0) < want:
                    f = spawn(queue.pop(0), spawned)
                    spawned += 1
                    if f:
                        foes.append(f)
        alive = [f for f in foes if f.hp > 0]
        has_laser = slot1 is not None and slot1["kind"] == "laser"
        if scenario == "gauntlet" and t < 5.0:
            alive = alive or []
        elif not alive and not (scenario == "gauntlet" and queue):
            return "win", kills, me.hp, t, times, has_laser, laser_t
        if me.hp <= 0:
            return "dead", kills, 0, t, times, has_laser, laser_t
        # ---- our observation
        mechs = []
        for f in alive:
            fx, fy, fz = f.pos
            d = math.hypot(fx - mx, fy - my)
            ins = d < 45 or (d < 200 and abs(wrap(math.atan2(fy - my, fx - mx) - me.aim[0])) < math.radians(60))
            vis_now = ins and (g.los(mx, my, mz + 8, fx, fy, fz + 4.5) or g.los(mx, my, mz + 8, fx, fy, fz + 9))
            if vis_now:
                memory[f.id] = t
            if t - memory.get(f.id, -99) <= 5.0:
                mechs.append({"id": f.id, "kind": "mech", "pos": [fx, fy, fz], "vel": [f.vel[0], f.vel[1], 0],
                              "aim": [round(f.aim[0], 3), 0.0], "hp": round(f.hp), "max_hp": 1400, "cubes": 0,
                              "weapons": ["gun", "laser"] if f.has_laser else ["gun"],
                              "charging": bool(f.lcharge > 0 and vis_now)})
        projs = [{"id": i, "kind": "gun", "pos": bl[:3], "vel": bl[3:6]} for i, bl in enumerate(bullets)
                 if bl[6] != me.id and math.hypot(bl[0] - mx, bl[1] - my) < 200]
        vis_pk = [p for p in pickups if math.hypot(p["pos"][0] - mx, p["pos"][1] - my) < 200 and
                  g.los(mx, my, mz + 8, p["pos"][0], p["pos"][1], p["pos"][2] + 0.5)]
        if slot1:
            slot1["charge"] = round(charge, 2) if slot1["kind"] == "laser" else 0
            slot1["cooldown"] = max(0.0, round(slot1["cooldown"], 2))
        gun.update(ammo=me.ammo, reload=round(max(me.reload, 0), 2), cooldown=round(max(me.cd, 0), 2))
        obs = json.loads(json.dumps(BASE))
        obs.update(tick=k * 2, t=round(t, 2), mechs=mechs, projectiles=projs, events=events, sounds=sounds,
                   beams=beams, pickups=vis_pk, cubes=[], crates=[], drops=[], zone=zone, offer=None,
                   alive=1 + len(alive))
        obs["self"].update(pos=[mx, my, mz], vel=[me.vel[0], me.vel[1], 0], aim=[round(me.aim[0], 4), round(me.aim[1], 4)],
                           hp=round(me.hp), max_hp=round(me.max_hp), energy=me.body.energy, in_zone=True,
                           grounded=me.body.grounded, weapons=[gun, slot1])
        events, beams, sounds = [], [], []
        t0 = time.perf_counter()
        a = b._act(obs)
        times.append(1000 * (time.perf_counter() - t0))
        # ---- take / drop
        tk = a.get("take")
        if tk is not None:
            p = next((q for q in pickups if q["id"] == tk), None)
            if p and math.hypot(p["pos"][0] - mx, p["pos"][1] - my) <= 4.0 and abs(p["pos"][2] - mz) <= 4.0:
                if slot1 is None or a.get("drop") == 1:
                    if slot1 is not None:
                        pickups.append({"id": 8100 + k, "kind": slot1["kind"], "pos": [mx, my, mz]})
                    stats = LAS if p["kind"] == "laser" else LIT
                    slot1 = {"kind": p["kind"], "cooldown": max(1.0, stats.get("cooldown", 1)), "charge": 0.0, "stats": stats}
                    pickups.remove(p)
                    charge = 0.0
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
                bullets.append([mx + d3[0] * 3, my + d3[1] * 3, mz + 7 + d3[2] * 3, d3[0] * v, d3[1] * v, d3[2] * v, me.id, 0.0])
                me.ammo -= 1; me.cd = GUN["cooldown"]
                if me.ammo == 0:
                    me.reload = GUN["reload"] * (0.8 if OUR_MAG > 12 else 1.0)
            me.cd -= DT
            if me.reload > 0:
                me.reload -= DT
                if me.reload <= 0:
                    me.ammo, me.reload = OUR_MAG, 0.0
        if a.get("reload") and me.reload <= 0 and me.ammo < OUR_MAG:
            me.reload = GUN["reload"]
        if slot1 and slot1["kind"] == "laser":
            held = a["fire"][1]
            if held and slot1["cooldown"] <= 0:
                charge = min(charge + 0.1, LAS["charge"] + LAS["hold_s"])
            elif charge >= LAS["min_charge"]:
                yaw, pit = me.aim
                d3 = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                ox, oy, oz = mx, my, mz + 7
                best = None
                for f in alive:
                    fx, fy, fz = f.pos
                    s_ = (fx - ox) * d3[0] + (fy - oy) * d3[1]
                    if s_ <= 0 or s_ > LAS["range"]:
                        continue
                    hz = oz + d3[2] * s_ / max(math.cos(pit), 1e-3)
                    if seg_hits_cyl((ox, oy, oz), (ox + d3[0] * s_ * 1.02, oy + d3[1] * s_ * 1.02, hz), (fx, fy, fz)) \
                            and g.los(ox, oy, oz, fx, fy, min(max(hz, fz), fz + 9)):
                        if best is None or s_ < best[0]:
                            best = (s_, f)
                if best:
                    f = best[1]
                    dmg = OUR_DMG * LAS["damage"] * min(1.0, LAS["min_frac"] + (1 - LAS["min_frac"]) * charge / LAS["charge"])
                    f.hp -= dmg; f.last_combat = t; dealt = True
                    events.append({"type": "hit", "target": f.id, "amount": round(dmg), "weapon": "laser"})
                    if f.hp <= 0:
                        kills += 1; events.append({"type": "kill", "target": f.id})
                charge, slot1["cooldown"] = 0.0, LAS["cooldown"]
            elif not held:
                charge = 0.0
            slot1["cooldown"] -= 0.1
        elif slot1 and slot1["kind"] == "lightning":
            held = a["fire"][1]
            slot1["cooldown"] -= 0.1
            if held and slot1["cooldown"] <= 0 and me.body.energy > LIT["energy"]:
                tg = None
                for f in alive:
                    fx, fy, fz = f.pos
                    d = math.hypot(fx - mx, fy - my)
                    if d < LIT["range"] and abs(wrap(math.atan2(fy - my, fx - mx) - me.aim[0])) < math.radians(LIT["cone_deg"] / 2) \
                            and g.los(mx, my, mz + 7, fx, fy, fz + 4.5) and (tg is None or d < tg[0]):
                        tg = (d, f)
                me.body.energy -= LIT["energy"] * 0.4
                if tg:
                    f = tg[1]
                    f.hp -= LIT["damage"] * 0.4; f.last_combat = t; dealt = True
                    if f.hp <= 0:
                        kills += 1; events.append({"type": "kill", "target": f.id})
        ox, oy = me.body.x, me.body.y
        me.body.step(a, 0.5 if charge > 0 else 1.0)
        me.vel = ((me.body.x - ox) / 0.1, (me.body.y - oy) / 0.1)
        vel_hist.append(me.vel)
        # ---- enemies
        mx, my, mz = me.pos
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
            react = f.seen_since is not None and t - f.seen_since >= f.react_s   # reaction time before opening fire
            mvx = mvy = 0.0
            if t >= f.hunt_t or sees:
                if sees and d < 125:
                    if t > f.strafe_t:
                        f.strafe = rng.choice((-1, 1)); f.strafe_t = t + rng.uniform(0.6, 1.6)
                    ux, uy = (mx - fx) / d, (my - fy) / d
                    radial = max(-1.0, min(1.0, (d - 90) / 40))
                    mvx, mvy = ux * radial - uy * f.strafe * 0.8, uy * radial + ux * f.strafe * 0.8
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
            # house bots jet too (the recorded one rose 18 m): climb toward us when we're above them, or when stuck
            climb = (mz > fz + 3.0 and d < 30.0) or (math.hypot(*f.vel) < 1.0 and (mvx or mvy) and t >= f.hunt_t)
            if climb and (mvx or mvy or mz > fz + 3.0):
                if not (mvx or mvy):
                    mvx, mvy = (mx - fx) / (d or 1), (my - fy) / (d or 1)
                f.body.step({"move": [mvx, mvy], "boost": f.body.energy > 1, "jump": f.body.grounded, "aim": [0, 0]})
            else:
                f.body.step({"move": [mvx, mvy], "boost": False, "jump": False, "aim": [0, 0]}, 0.5 if f.lcharge > 0 else 1.0)
            f.vel = ((f.body.x - fox) / 0.1, (f.body.y - foy) / 0.1)
            # laser: charge when it sees us, release once charged and it can shoot (auto-fire after 2 s full hold)
            if f.has_laser:
                f.lcd -= 0.1
                if f.lcharge > 0:
                    f.lcharge += 0.1
                    if (f.lcharge >= LAS["charge"] and can_shoot) or f.lcharge >= LAS["charge"] + LAS["hold_s"]:
                        if can_shoot and rng.random() < f.lhit and d < LAS["range"]:
                            rear = abs(wrap((brg_to_me + math.pi) - me.aim[0])) > math.pi / 2
                            dmg = LAS["damage"] * (1.5 if rear else 1.0)
                            me.hp -= dmg; me.last_combat = t
                            events.append({"type": "damaged", "amount": dmg, "weapon": "laser", "by": f.id,
                                           "bearing": brg_to_me + math.pi, "rear": rear})
                        if f.id in memory and t - memory[f.id] < 0.2:
                            beams.append({"by": f.id, "kind": "laser", "path": [[fx, fy, fz + 7], [mx, my, mz + 4.5]], "age": 0.05})
                        if d < 380:
                            sounds.append({"kind": "laser", "bearing": wrap(brg_to_me + math.pi), "dist": dist_word(d), "age": 0.05})
                        f.lcharge, f.lcd = 0.0, LAS["cooldown"]
                elif f.lcd <= 0 and sees and react and d < LAS["range"]:
                    f.lcharge = 0.1
            # gun: lead our lagged velocity
            for sub in range(2):
                if f.reload <= 0 and f.cd <= 0 and f.ammo > 0 and can_shoot and react and d < GUN["range"] * 0.85:
                    tt = d / GUN["speed"]
                    px, py = mx + lagged[0] * tt, my + lagged[1] * tt
                    yaw = math.atan2(py - fy, px - fx) + rng.gauss(0, math.radians(GUN["spread_deg"] * f.spm))
                    hd = math.hypot(px - fx, py - fy)
                    pit = math.atan2(mz + 4.5 - (fz + 7), hd) + rng.gauss(0, math.radians(GUN["spread_deg"] * f.spm))
                    v = GUN["speed"]
                    d3 = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                    bullets.append([fx + d3[0] * 3, fy + d3[1] * 3, fz + 7 + d3[2] * 3, d3[0] * v, d3[1] * v, d3[2] * v, f.id, 0.0])
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
            for bl in bullets:
                p0 = (bl[0], bl[1], bl[2])
                p1 = (bl[0] + bl[3] * DT, bl[1] + bl[4] * DT, bl[2] + bl[5] * DT)
                bl[7] += GUN["speed"] * DT
                hit = None
                targets = [me] if bl[6] != me.id else [f for f in foes if f.hp > 0]
                for tg in targets:
                    if seg_hits_cyl(p0, p1, tg.pos):
                        hit = tg
                        break
                if hit is not None and g.los(p0[0], p0[1], p0[2], hit.pos[0], hit.pos[1], hit.pos[2] + 4.5):
                    dmg = GUN["damage"]
                    if hit is me:
                        brg = math.atan2(-bl[4], -bl[3])
                        rear = abs(wrap(brg - me.aim[0])) > math.pi / 2
                        if rear:
                            dmg *= 1.5
                        me.hp -= dmg; me.last_combat = t
                        events.append({"type": "damaged", "amount": dmg, "weapon": "gun", "by": bl[6],
                                       "bearing": brg, "rear": rear})
                    else:
                        dmg *= OUR_DMG
                        hit.hp -= dmg; hit.last_combat = t; dealt = True
                        events.append({"type": "hit", "target": hit.id, "amount": dmg, "weapon": "gun"})
                        if hit.hp <= 0:
                            kills += 1; events.append({"type": "kill", "target": hit.id})
                    continue
                if bl[7] > GUN["range"] or not g.los(p0[0], p0[1], p0[2], p1[0], p1[1], p1[2]):
                    continue
                bl[0], bl[1], bl[2] = p1
                keep.append(bl)
            bullets = keep
        if dealt:
            me.last_combat = t
        regen(me, t)
        for f in foes:
            regen(f, t)
    return "timeout", kills, me.hp, t, times, slot1 is not None and slot1["kind"] == "laser", laser_t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=os.path.join(ROOT, "bot.py"))
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--only", default="")
    ap.add_argument("--no-hide", action="store_true")
    args = ap.parse_args()
    mod = load_bot(args.bot)
    if args.no_hide:
        mod.Bot.find_hide = lambda *a, **k: None
    g = mod.Geo(os.path.join(ROOT, "data", "map.bin"))
    all_t = []
    print(f"bot: {args.bot if not args.bot.startswith(ROOT) else os.path.relpath(args.bot, ROOT)}")
    if args.only in ("", "gauntlet"):
        rng = random.Random(args.seed * 100 + 99)
        res = [run_trial(mod, g, rng, 0, "gauntlet") for _ in range(args.trials)]
        for r in res:
            all_t += r[4]
        ks = sorted(r[1] for r in res)
        surv = sum(r[3] for r in res) / len(res)
        print(f"GAUNTLET (practice proxy: score = kills before death, max 15): mean {sum(ks)/len(ks):.2f}  "
              f"median {ks[len(ks)//2]}  best {ks[-1]}  avg survival {surv:.0f}s  holds laser {sum(1 for r in res if r[5])}/{len(res)}")
        if args.only == "gauntlet":
            s_ = sorted(all_t)
            print(f"tick time: p50 {s_[len(s_)//2]:.2f}  p99 {s_[int(len(s_)*.99)]:.2f}  max {s_[-1]:.2f} ms")
            return
    for n in (2, 3):
        for sc in ("loot", "laser"):
            if args.only and args.only != sc:
                continue
            rng = random.Random(args.seed * 100 + n * 10 + (sc == "laser"))
            res = [run_trial(mod, g, rng, n, sc) for _ in range(args.trials)]
            for r in res:
                all_t += r[4]
            wins = sum(1 for r in res if r[0] == "win")
            dead = sum(1 for r in res if r[0] == "dead")
            kills = sum(r[1] for r in res) / len(res)
            hl = sum(1 for r in res if r[5])
            lt = [r[6] for r in res if r[6] is not None]
            extra = f"  holds laser {hl:2d}/{len(res)}" + (f" (got it at {sum(lt)/len(lt):4.1f}s avg)" if sc == "loot" and lt else "")
            print(f"1v{n} {sc:5s}: win {wins:2d}/{len(res)}  died {dead:2d}  avg kills {kills:.2f}/{n}{extra}")
    s = sorted(all_t)
    print(f"tick time: p50 {s[len(s)//2]:.2f}  p99 {s[int(len(s)*.99)]:.2f}  max {s[-1]:.2f} ms")


if __name__ == "__main__":
    main()
