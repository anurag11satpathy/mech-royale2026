"""1v2 / 1v3 squad simulator: pursuing, strafing, lead-shooting gun bots vs our bot.

Enemies (house-bot stand-ins): 1400 HP, gun (45 dmg / 0.24 s, 12-round mag, 2.4 s reload, 252 m/s, 0.6 deg spread),
know where we are (squad vision), path to us with A*, strafe at 70-110 m when they see us, lead with our velocity
from 0.2 s ago, regen 45 HP/s after 5 s. Bullets are real projectiles: they can miss, hit walls, and hit our back
(x1.5). Our side gets real obs (vision cone + 5 s memory, enemy projectiles, damaged/hit/kill events), our torso
slews at 4 rad/s, our gun fires real bullets along the actual aim, our laser is hitscan along the actual aim.
Enemies walk only (no jets), so rooftops are safer here than in real matches.

python3 tools/squad_sim.py [--bot path/to/bot.py] [--trials N]
"""
import argparse, gzip, importlib.util, json, math, os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from geo import AStar  # noqa: E402
from combat_sim import Body  # noqa: E402

lines = gzip.open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt").read().splitlines()
HELLO = json.loads(lines[0])
BASE = json.loads(lines[1])
CFG = HELLO["config"]
GUN = CFG["weapons"]["gun"]
LAS = CFG["weapons"]["laser"]
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
    def __init__(self, g, x, y, mid):
        self.body = Body(g, x, y)
        self.id = mid
        self.hp = 1400.0
        self.ammo, self.reload, self.cd = 12, 0.0, 0.0
        self.last_combat = -99.0
        self.vel = (0.0, 0.0)

    @property
    def pos(self):
        b = self.body
        return b.x, b.y, b.z


def regen(m, t):
    if t - m.last_combat > 5.0 and m.hp > 0:
        m.hp = min(1400.0, m.hp + 45 * 0.1)


def seg_hits_cyl(p0, p1, c, r=2.5, h=9.0):
    """Does segment p0->p1 pass through the vertical cylinder at c (feet)?"""
    (x0, y0, z0), (x1, y1, z1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    u = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((c[0] - x0) * dx + (c[1] - y0) * dy) / L2))
    px, py, pz = x0 + dx * u, y0 + dy * u, z0 + (z1 - z0) * u
    return math.hypot(px - c[0], py - c[1]) < r and c[2] <= pz <= c[2] + h


def run_trial(mod, g, rng, n_enemies, loadout):
    b = mod.Bot(HELLO)
    while True:
        x, y = rng.uniform(-300, 350), rng.uniform(-200, 250)
        if g.NAV[g.ncell(x, y)]:
            break
    me = Mech(g, x, y, 3)
    me.aim = [0.0, 0.0]
    base_a = rng.uniform(0, 2 * math.pi)
    foes = []
    for i in range(n_enemies):
        for _ in range(500):
            a = base_a + i * (2 * math.pi / n_enemies) * rng.uniform(0.35, 0.6) + rng.uniform(-0.3, 0.3)
            d = rng.uniform(120, 180)
            ex, ey = x + math.cos(a) * d, y + math.sin(a) * d
            if g.NAV[g.ncell(ex, ey)] == 1:
                foes.append(Mech(g, ex, ey, 5 + i))
                break
    for f in foes:
        f.path, f.path_t, f.pi, f.strafe, f.strafe_t, f.seen_t = None, -99, 0, 1, 0, -99
    zone = {"center": [x, y], "radius": 560, "next_center": [x, y], "next_radius": 380,
            "shrink_start": 999, "shrink_end": 1050, "dps": 20}
    gun = dict(BASE["self"]["weapons"][0]); gun.update(ammo=12, reload=0, cooldown=0)
    laser = {"kind": "laser", "cooldown": 0.0, "charge": 0.0, "stats": LAS} if loadout == "laser" else None
    charge, lcd = 0.0, 0.0
    bullets = []      # [x, y, z, vx, vy, vz, owner, travelled]
    memory = {}       # foe id -> last seen t
    vel_hist = []
    events = []
    kills = 0
    times = []
    t = 0.0
    for k in range(900):  # 90 s
        t = k * 0.1
        mx, my, mz = me.pos
        alive = [f for f in foes if f.hp > 0]
        if not alive:
            return "win", kills, me.hp, t, times
        if me.hp <= 0:
            return "dead", kills, 0, t, times
        # ---- our observation
        mechs = []
        for f in alive:
            fx, fy, fz = f.pos
            d = math.hypot(fx - mx, fy - my)
            ins = d < 45 or (d < 200 and abs(wrap(math.atan2(fy - my, fx - mx) - me.aim[0])) < math.radians(60))
            if ins and (g.los(mx, my, mz + 8, fx, fy, fz + 4.5) or g.los(mx, my, mz + 8, fx, fy, fz + 9)):
                memory[f.id] = t
            if t - memory.get(f.id, -99) <= 5.0:
                mechs.append({"id": f.id, "kind": "mech", "pos": [fx, fy, fz], "vel": [f.vel[0], f.vel[1], 0],
                              "aim": [0, 0], "hp": round(f.hp), "max_hp": 1400, "cubes": 0, "weapons": ["gun"],
                              "charging": False})
        projs = [{"id": i, "kind": "gun", "pos": bl[:3], "vel": bl[3:6]} for i, bl in enumerate(bullets)
                 if bl[6] != me.id and math.hypot(bl[0] - mx, bl[1] - my) < 200]
        if laser:
            laser.update(charge=round(charge, 2), cooldown=max(0.0, round(lcd, 2)))
        gun.update(ammo=me.ammo, reload=round(max(me.reload, 0), 2), cooldown=round(max(me.cd, 0), 2))
        obs = json.loads(json.dumps(BASE))
        obs.update(tick=k * 2, t=round(t, 2), mechs=mechs, projectiles=projs, events=events, sounds=[],
                   pickups=[], cubes=[], crates=[], drops=[], zone=zone, offer=None, alive=1 + len(alive))
        obs["self"].update(pos=[mx, my, mz], vel=[me.vel[0], me.vel[1], 0], aim=[round(me.aim[0], 4), round(me.aim[1], 4)],
                           hp=round(me.hp), max_hp=1400, energy=me.body.energy, in_zone=True,
                           grounded=me.body.grounded, weapons=[gun, laser])
        events = []
        t0 = time.perf_counter()
        a = b._act(obs)
        times.append(1000 * (time.perf_counter() - t0))
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
                    me.reload = GUN["reload"]
            me.cd -= DT
            if me.reload > 0:
                me.reload -= DT
                if me.reload <= 0:
                    me.ammo, me.reload = 12, 0.0
        if a.get("reload") and me.reload <= 0 and me.ammo < 12:
            me.reload = GUN["reload"]
        if laser:
            held = a["fire"][1]
            if held and lcd <= 0:
                charge = min(charge + 0.1, LAS["charge"] + LAS["hold_s"])
            elif charge >= LAS["min_charge"]:
                # hitscan along the actual aim
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
                    dmg = LAS["damage"] * min(1.0, LAS["min_frac"] + (1 - LAS["min_frac"]) * charge / LAS["charge"])
                    f.hp -= dmg; f.last_combat = t; dealt = True
                    events.append({"type": "hit", "target": f.id, "amount": round(dmg), "weapon": "laser"})
                    if f.hp <= 0:
                        kills += 1; events.append({"type": "kill", "target": f.id})
                charge, lcd = 0.0, LAS["cooldown"]
            elif not held:
                charge = 0.0
            lcd -= 0.1
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
            if sees:
                f.seen_t = t
            mvx = mvy = 0.0
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
            f.body.step({"move": [mvx, mvy], "boost": False, "jump": False, "aim": [0, 0]})
            f.vel = ((f.body.x - fox) / 0.1, (f.body.y - foy) / 0.1)
            # shoot: lead our (lagged) velocity
            for sub in range(2):
                if f.reload <= 0 and f.cd <= 0 and f.ammo > 0 and sees and d < GUN["range"] * 0.85 and \
                        g.los(fx, fy, fz + 7, mx, my, mz + 4.5):
                    tt = d / GUN["speed"]
                    px, py = mx + lagged[0] * tt, my + lagged[1] * tt
                    yaw = math.atan2(py - fy, px - fx) + rng.gauss(0, math.radians(GUN["spread_deg"]))
                    hd = math.hypot(px - fx, py - fy)
                    pit = math.atan2(mz + 4.5 - (fz + 7), hd) + rng.gauss(0, math.radians(GUN["spread_deg"]))
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
        # ---- bullets (2 sub-steps)
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
                        brg = math.atan2(-bl[4], -bl[3])   # direction the shot came from
                        rear = abs(wrap(brg - me.aim[0])) > math.pi / 2
                        if rear:
                            dmg *= 1.5
                        me.hp -= dmg; me.last_combat = t
                        events.append({"type": "damaged", "amount": dmg, "weapon": "gun", "by": bl[6],
                                       "bearing": brg, "rear": rear})
                    else:
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
    return "timeout", kills, me.hp, t, times


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=os.path.join(ROOT, "bot.py"))
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--seed", type=int, default=21)
    args = ap.parse_args()
    mod = load_bot(args.bot)
    g = mod.Geo(os.path.join(ROOT, "data", "map.bin"))
    all_t = []
    print(f"bot: {os.path.relpath(args.bot, ROOT) if args.bot.startswith(ROOT) else args.bot}")
    for n in (2, 3):
        for loadout in ("laser", "gun"):
            rng = random.Random(args.seed * 100 + n * 10 + (loadout == "gun"))
            res = [run_trial(mod, g, rng, n, loadout) for _ in range(args.trials)]
            for r in res:
                all_t += r[4]
            wins = sum(1 for r in res if r[0] == "win")
            dead = sum(1 for r in res if r[0] == "dead")
            kills = sum(r[1] for r in res) / len(res)
            surv = [r[3] for r in res if r[0] == "dead"]
            print(f"1v{n} gun+{'laser' if loadout == 'laser' else 'none '}: win {wins:2d}/{len(res)}  died {dead:2d}  "
                  f"avg kills {kills:.2f}/{n}  avg survival when died {sum(surv)/max(len(surv),1):5.1f}s")
    s = sorted(all_t)
    print(f"tick time: p50 {s[len(s)//2]:.2f}  p99 {s[int(len(s)*.99)]:.2f}  max {s[-1]:.2f} ms")


if __name__ == "__main__":
    main()
