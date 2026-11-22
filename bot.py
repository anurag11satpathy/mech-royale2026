"""Mech Royale v1: lead-aiming gunner with duel / heal-in-cover / zone-rotation states.

All logging goes to stderr (mf.log). Reads only data/map.bin (shipped) and writes nothing to disk.
"""
import gc, math, os, random, time
from collections import deque

import mechfight as mf
from geo import Geo, AStar

HERE = os.path.dirname(os.path.abspath(__file__))
PI = math.pi
TAU = 2 * math.pi

UPGRADE_PRIORITY = [
    "armor", "plating", "nanites", "laser_damage", "gun_damage", "laser_charge", "gun_rate",
    "lightning_efficiency", "gyros", "servos", "gun_mag", "capacitor", "jets", "reactor", "lightning_range",
    "sensors", "gun_velocity", "lightning_chain", "mortar_reload", "mortar_velocity", "mortar_splash",
    "laser_pierce",
]
# Upgrade plan from the exact numbers: plating x1.15 EHP/stack (beats armor's ~x1.09), nanites cut a 40->85% heal
# from 19 s to 12 s, drum mag lifts sustained gun DPS 102->134 (x1.31), lens x1.18 laser damage.
UPGRADE_PLAN = ["plating", "nanites", "gun_mag", "laser_damage", "plating", "gun_mag", "laser_damage", "nanites",
                "armor", "gun_damage"]
UPGRADE_REST = ["plating", "laser_damage", "armor", "gun_damage", "laser_charge", "gyros", "gun_rate", "servos",
                "capacitor", "jets", "reactor", "lightning_efficiency", "lightning_range", "lightning_chain",
                "sensors", "gun_velocity", "mortar_reload", "mortar_velocity", "mortar_splash", "laser_pierce"]
WEAPON_PREF = {"laser": 3.0, "lightning": 2.5, "mortar": 1.0, "gun": 0.0}
GHOST_DIST = {"gun": 120, "laser": 150, "mortar": 160, "lightning": 35, "npc": 40}


def lightning_close(lightning, d):
    """With lightning in hand and the target nearly in arc range, brawl instead of peeking."""
    return lightning is not None and d < lightning[1]["stats"]["range"] * 1.3


def r_now_le(zone, t, lim=75.0):
    s, e = zone["shrink_start"], zone["shrink_end"]
    r, nr = zone["radius"], zone["next_radius"]
    if t > s and e > s:
        r = r + (nr - r) * clamp((t - s) / (e - s), 0.0, 1.0)
    return r <= lim


def wrap(a):
    return (a + PI) % TAU - PI


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Bot:
    name = "anurag-v5"
    # tunables (squad_sim gauntlet experiments)
    USE_HIDE = False     # hide from aimed laser charges: built, neutral-to-negative in squad_sim gauntlet -> off
    R_LASER = 130.0      # preferred duel range with a laser
    HEAL_EXIT = 0.85     # leave HEAL at this HP fraction
    PREHEAL = 0.0        # with no enemy in sight, go heal (cover + stand still) below this HP fraction
    HEAL_BASE = 0.42     # HEAL below this HP fraction
    HEAL_MULTI = 0.55    # ... or below this when 2+ see / hit us
    HEAL_FIRE = False    # keep shooting while retreating (regen is impossible while a hunter chases anyway)
    USE_KITE = False     # split-kite vs 2+ bunched pursuers: built, 3.94 vs 4.16 kills in squad_sim gauntlet -> off
    KITE_GAP = 50.0      # required lead of the nearest pursuer over the second (m)

    def __init__(self, hello):
        self.cfg = cfg = hello["config"]
        self.geo = Geo(os.path.join(HERE, "data", "map.bin"))
        self.rng = random.Random(hello.get("seed", 0))
        self.me_id = hello.get("slot")
        self.reach = cfg["loot"]["pickup_radius"]
        self.cube_reach = cfg["cubes"]["pickup_radius"]
        self.gun_cfg = cfg["weapons"]["gun"]
        self.state = "ROAM"
        self.target_id = None
        self.strafe_sign = 1
        self.strafe_until = 0.0
        self.steer_side = 1
        self.dmg_log = deque()      # (t, amount, by, bearing, weapon)
        self.last_combat_t = -99.0  # last time we dealt or took damage
        self.ghosts = []            # (t, x, y) guesses for attackers we cannot see
        self.items = {}             # id -> [kind, x, y, z, t_seen]  pickups ("p:"), cubes ("c:")
        self.planner = None
        self.path = None
        self.path_goal = None
        self.path_i = 0
        self.path_t = -99.0
        self.cover = None
        self.cover_t = -99.0
        self.cover_full = False
        self.hist = deque()         # (t, x, y, wanted_move)
        self.stuck_until = 0.0
        self.unstick = (0.0, 0.0)
        self.climb_until = 0.0
        self.climb_dir = (0.0, 0.0)
        self.stuck_count = 0
        self.boosting = False
        self.look_bearing = None
        self.look_until = 0.0
        self.last_los_t = -99.0
        self.laser_hold_t = None
        self.laser_fired_t = -99.0
        self.peek = None
        self.peek_search_t = -99.0
        self.peek_active = False
        self.crates = {}            # id -> [x, y, z, hp, t_seen]
        self.npc_mem = {}           # id -> [x, y, z, t_seen]
        self.farm = None            # (kind, id) we are working on in ROAM
        self.taken = {}             # upgrade id -> stacks we picked
        self.last_offer = None
        self.hide = None            # active laser-charge hide {spot, eid, start, until}
        self.peek_window_until = 0.0
        self.end_hold = False
        self.jump_req = False
        self.kiting = False
        self.times = []
        self.n = 0
        # The map grids are big lists the GC would otherwise rescan on every full collection (~2 ms+).
        gc.collect()
        gc.freeze()

    # ------------------------------------------------------------------ helpers
    def zone_at(self, z, t):
        c, r, nc, nr = z["center"], z["radius"], z["next_center"], z["next_radius"]
        s, e = z["shrink_start"], z["shrink_end"]
        if t <= s or e <= s:
            return c[0], c[1], r
        f = clamp((t - s) / (e - s), 0.0, 1.0)
        return c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f, r + (nr - r) * f

    def zone_ok(self, px, py, margin=12.0, ahead=8.0):
        cx, cy, r = self.zone_at(self.zone, self.t + ahead)
        return math.hypot(px - cx, py - cy) < r - margin

    def zone_anchor(self, x, y):
        z = self.zone
        nx, ny = z["next_center"]
        nr = z["next_radius"]
        d = math.hypot(x - nx, y - ny)
        keep = (0.3 if r_now_le(self.zone, self.t) else 0.55) * nr
        if d <= keep:
            return x, y, d
        return nx + (x - nx) / d * keep, ny + (y - ny) / d * keep, d

    # ------------------------------------------------------------------ movement
    def steer(self, x, y, z, vx, vy):
        m = math.hypot(vx, vy)
        if m < 1e-3:
            return 0.0, 0.0
        m = min(m, 1.0)
        a = math.atan2(vy, vx)
        g = self.geo
        s = self.steer_side
        for da in (0.0, 0.35, 0.7, 1.05, 1.4, 1.8, 2.3, 2.8):
            for sgn in ((s, -s) if da else (1,)):
                ang = a + da * sgn
                c, sn = math.cos(ang), math.sin(ang)
                if not g.blocked_at(x + c * 3.2, y + sn * 3.2, z, 3.2) and \
                        not g.blocked_at(x + c * 6.0, y + sn * 6.0, z, 6.0):
                    if da:
                        self.steer_side = sgn
                    return c * m, sn * m
        return -vx, -vy

    def pick_upgrade(self, choices):
        """Follow UPGRADE_PLAN in order (counting stacks we already took), then UPGRADE_REST."""
        def rank(u):
            need = self.taken.get(u, 0) + 1
            seen = 0
            for i, p in enumerate(UPGRADE_PLAN):
                if p == u:
                    seen += 1
                    if seen == need:
                        return i
            return 100 + (UPGRADE_REST.index(u) if u in UPGRADE_REST else 99)
        return min(range(len(choices)), key=lambda i: rank(choices[i]))

    def find_hide(self, x, y, z, shooter, others):
        """Nearest spot within 10 m that breaks line of sight to `shooter` (and, if possible, the others)."""
        g = self.geo
        sx, sy, sz = shooter
        best, bs = None, 1e18
        off = math.atan2(y - sy, x - sx)
        for r in (3.5, 6.5, 10.0):
            for k in range(10):
                a = off + k * TAU / 10
                px, py = x + math.cos(a) * r, y + math.sin(a) * r
                if not g.NAV[g.ncell(px, py)] or g.blocked_at(px, py, z, r):
                    continue
                pz = g.surface(px, py, z)
                if g.los(sx, sy, sz + 8.0, px, py, pz + 8.8):
                    continue
                sc = r + (0.0 if self.hidden_from(px, py, pz, others[:3]) else 6.0)
                if sc < bs and (r < 5 or g.walk_clear(x, y, z, px, py)):
                    bs, best = sc, (px, py)
            if best is not None:
                break
        return best

    def duel_override(self, x, y, z, me, target, enemies, threats, laser, endgame, finishable, winning, under_fire):
        """Movement that pre-empts normal duelling: hide from a laser charge, grab a laser, end-game hold.
        Returns (mvx, mvy, boost, label) or None."""
        t = self.t
        g = self.geo
        # --- 2. hide from an enemy laser charge aimed at us; come back out when their charge drops
        if self.hide is not None:
            hd = self.hide
            em = next((e for e in enemies if e[0]["id"] == hd["eid"]), None)
            fired = any(b.get("by") == hd["eid"] for b in self.obs.get("beams", ())) or \
                any(s_["kind"] == "laser" and s_.get("age", 1) < t - hd["start"] + 0.05 for s_ in self.obs.get("sounds", ()))
            dropped = em is not None and em[2] and not em[0].get("charging")
            if t - hd["start"] > 0.3 and (fired or dropped or t > hd["until"] or em is None):
                self.hide = None
                self.peek_window_until = t + 1.4   # their laser is on cooldown: our window
            else:
                hx, hy = hd["spot"]
                dd = math.hypot(hx - x, hy - y)
                if dd < 0.8:
                    return 0.0, 0.0, False, "HIDE-in"
                vx, vy = self.nav_to(x, y, z, hx, hy)
                return vx, vy, True, "HIDE"
        if t > self.peek_window_until and self.USE_HIDE:
            for e in enemies:
                em, d, vis, _ = e
                if not (vis and em.get("charging") and d < 300):
                    continue
                brg = math.atan2(y - em["pos"][1], x - em["pos"][0])
                if abs(wrap(em.get("aim", [9.0, 0.0])[0] - brg)) < 0.15:
                    others = [q for q in threats if abs(q[0] - em["pos"][0]) > 1 or abs(q[1] - em["pos"][1]) > 1]
                    spot = self.find_hide(x, y, z, em["pos"], others)
                    if spot is not None:
                        self.hide = {"spot": spot, "eid": em["id"], "start": t, "until": t + 2.3}
                        vx, vy = self.nav_to(x, y, z, spot[0], spot[1])
                        return vx, vy, True, "HIDE"
                    break
        # --- 1. laser first: grab a nearby laser mid-fight if the target isn't pinning us
        if laser is None and t < 200:
            m, d, vis, _ = target
            reach = 300.0 if t < 120 else 90.0
            if not (vis and d < 90) and not finishable:
                best = None
                for k, it in self.items.items():
                    if it[0] != "laser":
                        continue
                    dd = math.hypot(it[1] - x, it[2] - y)
                    if dd < reach and it[3] - z < 45 and self.zone_ok(it[1], it[2], 10.0, dd / 11.0 + 5) and \
                            (best is None or dd < best[0]):
                        best = (dd, it)
                if best:
                    it = best[1]
                    vx, vy, jn = self.go_to(x, y, z, it[1], it[2], it[3], me)
                    self.jump_req = self.jump_req or jn
                    return vx, vy, best[0] > 20, "GRAB-laser"
        # --- split-kite: never take a straight 1v2; run until the nearest pursuer is well ahead of the next
        self.kiting = False
        if self.USE_KITE and not finishable and not endgame:
            near = sorted((e for e in enemies if e[1] < 260), key=lambda e: e[1])
            if len(near) >= 2:
                e1, e2 = near[0], near[1]
                bunched = (e2[1] - e1[1]) < self.KITE_GAP or (e2[2] and e2[1] < 200)
                if bunched:
                    self.kiting = True
                    cx = (e1[0]["pos"][0] + e2[0]["pos"][0]) / 2 - x
                    cy = (e1[0]["pos"][1] + e2[0]["pos"][1]) / 2 - y
                    base = math.atan2(-cy, -cx)            # straight away from the pair
                    for da in (0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5):
                        a = base + da
                        gx, gy = x + math.cos(a) * 60, y + math.sin(a) * 60
                        if self.zone_ok(gx, gy, 15.0, 6.0) and g.NAV[g.ncell(gx, gy)]:
                            vx, vy = self.nav_to(x, y, z, gx, gy)
                            return vx, vy, me["energy"] > 25, "KITE"
                    self.kiting = False
        # --- 5. end-game: hold cover near the next circle's center, no even trades
        if endgame and not (finishable or winning):
            self.end_hold = True
            nx_, ny_ = self.zone["next_center"]
            nr = self.zone["next_radius"]
            dn = math.hypot(nx_ - x, ny_ - y)
            if dn > max(0.3 * nr, 8.0):
                vx, vy = self.nav_to(x, y, z, nx_, ny_)
                return vx, vy, dn > 40, "END-move"
            if threats and (self.cover is None or t - self.cover_t > 1.0):
                self.cover = self.find_cover(x, y, z, threats)
                self.cover_t = t
            if self.cover and math.hypot(self.cover[0] - nx_, self.cover[1] - ny_) < max(0.45 * nr, 12.0):
                cd = math.hypot(self.cover[0] - x, self.cover[1] - y)
                if cd > 0.8:
                    vx, vy = self.nav_to(x, y, z, self.cover[0], self.cover[1])
                    return vx, vy, cd > 10, "END-cover"
            return 0.0, 0.0, False, "END-hold"
        return None

    def start_climb(self, t, ux, uy, dur=2.5):
        self.climb_start = t
        self.climb_until = t + dur
        self.climb_dir = (ux, uy)

    def go_to(self, x, y, z, gx, gy, gz, me):
        """Walk to a goal; if it is on a roof we can reach, jump + jet onto it from close range.
        Returns (mvx, mvy, climb) where climb asks the caller to jump now."""
        dz = gz - z
        hd = math.hypot(gx - x, gy - y)
        if dz > 3.0 and hd < 16.0:
            if me["grounded"] and self.t >= self.climb_until:
                if me["energy"] >= 1.6 * dz + 12:
                    ux, uy = (gx - x) / (hd or 1), (gy - y) / (hd or 1)
                    self.start_climb(self.t, ux, uy, 1.2 + dz / 7.0)
                    return ux, uy, True
                return 0.0, 0.0, False   # wait for energy at the foot of the building
            return (gx - x) / (hd or 1), (gy - y) / (hd or 1), False
        return self.nav_to(x, y, z, gx, gy) + (False,)

    def rise_ahead(self, x, y, z, ux, uy):
        g = self.geo
        top = z
        for d in (3.0, 5.0, 7.0):
            c = g.cell(x + ux * d, y + uy * d)
            if c < 0:
                return 99.0
            h = g.G[c]
            if g.T[c] > z + 1.2 and g.B[c] < z + 9.0:
                h = max(h, g.T[c])
            top = max(top, h)
        return top - z

    def terrain_ahead(self, x, y, z, ux, uy):
        g = self.geo
        for d in (3.0, 5.0):
            c = g.cell(x + ux * d, y + uy * d)
            if c >= 0 and g.T[c] > z + 1.2 and g.B[c] < z + 9.0:
                return False
        return True

    def nav_to(self, x, y, z, gx, gy):
        g = self.geo
        dx, dy = gx - x, gy - y
        d = math.hypot(dx, dy)
        if d < 3.0:
            return dx / 3.0, dy / 3.0
        on_roof = z > g.ground(x, y) + 3.0
        if on_roof or (d < 140 and g.walk_clear(x, y, z, gx, gy)):
            self.path = None
            return dx / d, dy / d
        t = self.t
        need = (self.path is None or self.path_goal is None
                or math.hypot(self.path_goal[0] - gx, self.path_goal[1] - gy) > 12 or t - self.path_t > 5)
        if need and self.planner is None:
            self.planner = AStar(g, (x, y), (gx, gy))
            self.planner_goal = (gx, gy)
        if self.planner is not None:
            if self.planner.step(0 if self.heavy else 450):
                if self.planner.path:
                    self.path = self.planner.path
                    self.path_i = 0
                    self.path_goal = self.planner_goal
                    self.path_t = t
                self.planner = None
        if not self.path:
            return dx / d, dy / d
        p = self.path
        while self.path_i < len(p) - 1 and math.hypot(p[self.path_i][0] - x, p[self.path_i][1] - y) < 5.0:
            self.path_i += 1
        # if we drifted far from the path, replan
        if math.hypot(p[self.path_i][0] - x, p[self.path_i][1] - y) > 30:
            self.path_t = -99.0
        wx, wy = p[self.path_i]
        for k in range(min(len(p) - 1, self.path_i + 5), self.path_i, -1):
            if g.walk_clear(x, y, z, p[k][0], p[k][1]):
                wx, wy = p[k]
                break
        ddx, ddy = wx - x, wy - y
        dd = math.hypot(ddx, ddy) or 1.0
        return ddx / dd, ddy / dd

    def dodge(self, x, y, z):
        """Sidestep incoming bullets and away from shell landing points."""
        vx = vy = 0.0
        cz = z + 4.5
        grav = self.cfg["weapons"]["mortar"]["gravity"]
        for p in self.obs.get("projectiles", ()):
            px, py, pz = p["pos"]
            ux, uy, uz = p["vel"]
            if p["kind"] == "mortar":
                # time to fall to our height
                a, b, c = -0.5 * grav, uz, pz - z
                disc = b * b - 4 * a * c
                if disc < 0:
                    continue
                tl = (-b - math.sqrt(disc)) / (2 * a)
                if tl <= 0 or tl > 3.0:
                    continue
                lx, ly = px + ux * tl, py + uy * tl
                dd = math.hypot(x - lx, y - ly)
                if dd < 11:
                    k = (11 - dd) / 11 + 0.5
                    vx += (x - lx) / (dd or 1) * k
                    vy += (y - ly) / (dd or 1) * k
                continue
            rx, ry, rz = x - px, y - py, cz - pz
            v2 = ux * ux + uy * uy + uz * uz
            if v2 < 1:
                continue
            tc = (rx * ux + ry * uy + rz * uz) / v2
            if tc <= 0 or tc > 1.0:
                continue
            cx, cy = rx - ux * tc, ry - uy * tc
            miss = math.hypot(cx, cy)
            if miss < 4.0:
                # move perpendicular to the bullet, on the side we already are
                hv = math.hypot(ux, uy) or 1.0
                perx, pery = -uy / hv, ux / hv
                sgn = 1.0 if (cx * perx + cy * pery) >= 0 else -1.0
                vx += perx * sgn * 0.8
                vy += pery * sgn * 0.8
        return vx, vy

    # ------------------------------------------------------------------ cover
    def hidden_from(self, px, py, pz, threats):
        los = self.geo.los
        for tx, ty, tz in threats:
            if los(tx, ty, tz + 8.0, px, py, pz + 8.8):
                return False
        return True

    def hidden_torso(self, px, py, pz, threats):
        los = self.geo.los
        for tx, ty, tz in threats:
            if los(tx, ty, tz + 8.0, px, py, pz + 5.0):
                return False
        return True

    def find_cover(self, x, y, z, threats):
        """Nearest spot (behind a building or terrain ridge) that breaks line of sight to every threat.
        Spots hugging walls are allowed (that is where cover is); routes that run toward threats cost more."""
        g = self.geo
        threats = sorted(threats, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)[:4]
        if not threats:
            return None
        cx = sum(q[0] for q in threats) / len(threats) - x
        cy = sum(q[1] for q in threats) / len(threats) - y
        cd = math.hypot(cx, cy) or 1.0
        cx, cy = cx / cd, cy / cd
        best, bs = None, 1e18
        partial, ps = None, 1e18
        off = self.rng.random() * TAU / 16
        found_r = None
        self.heavy = True
        checks = 0
        for r in (6.0, 12.0, 20.0, 30.0, 42.0, 56.0, 75.0, 100.0):
            if (found_r is not None and r > found_r * 1.6) or checks > 44:
                break
            for k in range(16):
                a = off + k * (TAU / 16)
                ux, uy = math.cos(a), math.sin(a)
                toward = ux * cx + uy * cy          # >0: running at the threats
                if r > 56.0 and toward > 0.3:
                    continue                        # long runs only away from the threats
                px, py = x + ux * r, y + uy * r
                if not g.NAV[g.ncell(px, py)]:
                    continue
                if not self.zone_ok(px, py, 10.0, 10.0):
                    continue
                sc = r * (1.0 + 1.5 * max(0.0, toward))
                if sc >= bs:
                    continue
                gz = g.ground(px, py)
                checks += 1
                if self.hidden_from(px, py, gz, threats):
                    if r > 14 and not g.walk_clear(x, y, z, px, py):
                        sc += r * 0.4               # needs a detour
                    if sc < bs:
                        bs, best = sc, (px, py)
                        if found_r is None:
                            found_r = r
                elif best is None and sc < ps:
                    checks += 1
                    if self.hidden_torso(px, py, gz, threats):
                        ps, partial = sc, (px, py)
        self.cover_full = best is not None
        return best or partial

    def find_peek(self, x, y, z, tx, ty, tz, tch, threats, max_range):
        """Pair a cover spot C (hidden from all threats) with a firing spot P (sees the target) a few meters away."""
        g = self.geo
        if z > g.ground(x, y) + 3.0:
            return None  # roofs: no peek logic in v2
        los = g.los
        self.heavy = True
        teye = tz + 8.0
        others = [q for q in threats if abs(q[0] - tx) > 1 or abs(q[1] - ty) > 1]
        base = math.atan2(ty - y, tx - x)
        pts = [(x, y)]
        for r, n in ((5.0, 8), (10.0, 10), (16.0, 10), (23.0, 12)):
            for k in range(n):
                a = base + (k + (0.5 if r in (10.0, 23.0) else 0.0)) * TAU / n
                pts.append((x + math.cos(a) * r, y + math.sin(a) * r))
        hid, vis = [], []
        for px, py in pts:
            if not g.NAV[g.ncell(px, py)] or not self.zone_ok(px, py, 10.0, 8.0):
                continue
            dt = math.hypot(px - tx, py - ty)
            if dt < 45.0:
                continue
            gz = g.ground(px, py)
            if los(tx, ty, teye, px, py, gz + 8.8):
                if dt < max_range and los(px, py, gz + 7.0, tx, ty, tch):
                    vis.append((px, py))
            elif self.hidden_from(px, py, gz, others[:3]):
                hid.append((px, py))
        best, bs = None, 1e18
        for cxp, cyp in hid:
            dc = math.hypot(cxp - x, cyp - y)
            for pxp, pyp in vis:
                dcp = math.hypot(cxp - pxp, cyp - pyp)
                if dcp < 2.0 or dcp > 10.5:
                    continue
                s = dc + 0.7 * dcp
                if s < bs and g.walk_clear(cxp, cyp, g.ground(cxp, cyp), pxp, pyp):
                    bs, best = s, ((cxp, cyp), (pxp, pyp))
        return best

    def peek_move(self, x, y, z, me, target, threats, laser, gun):
        """Peek cycle: wait (and charge the laser) at C, step out to P to shoot, step back to C."""
        t = self.t
        m, d, vis, ch = target
        tx, ty, tz = m["pos"]
        pk = self.peek
        need = (pk is None or pk["tid"] != m["id"] or math.hypot(tx - pk["tx"], ty - pk["ty"]) > 15
                or t - pk["t"] > 5.0)
        if not need and pk["phase"] == "cover" and t - pk.get("chk", 0) > 0.3:
            pk["chk"] = t
            cxp, cyp = pk["C"]
            if not self.hidden_from(cxp, cyp, self.geo.ground(cxp, cyp), threats[:4]):
                need = True
        if need:
            if t - self.peek_search_t < 0.8:
                return None if pk is None or pk["tid"] != m["id"] else self._peek_vec(x, y, z, pk)
            self.peek_search_t = t
            rng_max = (laser[1]["stats"]["range"] if laser else gun[1]["stats"]["range"] if gun else 200) * 0.85
            pair = self.find_peek(x, y, z, tx, ty, tz, ch, threats[:4], rng_max)
            if not pair:
                self.peek = None
                return None
            keep = pk is not None and pk["tid"] == m["id"]
            self.peek = pk = {"C": pair[0], "P": pair[1], "tid": m["id"], "tx": tx, "ty": ty, "t": t,
                              "phase": pk["phase"] if keep else "cover", "pt": pk["pt"] if keep else t,
                              "hp0": pk["hp0"] if keep else me["hp"], "chk": t}
        # phase transitions
        hp = me["hp"]
        if pk["phase"] == "cover":
            at_c = math.hypot(pk["C"][0] - x, pk["C"][1] - y) < 3.0
            ready = False
            if laser:
                lw = laser[1]
                ready = (lw.get("charge") or 0) > 0
            if not ready and gun:
                gw = gun[1]
                gun_ok = gw.get("reload", 0) <= 0 and gw.get("ammo", 0) >= 0.75 * gw["stats"]["mag"]
                ready = gun_ok and (not laser or laser[1].get("cooldown", 0) > 0.9)
            if at_c and ready:
                pk["phase"], pk["pt"], pk["hp0"] = "out", t, hp
                self.laser_fired_t = -99.0
        else:
            took = pk["hp0"] - hp
            fired = self.laser_fired_t > pk["pt"] and t - self.laser_fired_t > 0.45
            gun_dry = gun is None or gun[1].get("ammo", 0) <= 1 or gun[1].get("reload", 0) > 0
            charging = laser is not None and (laser[1].get("charge") or 0) > 0
            if t - pk["pt"] > 3.0 or took > 230 or (fired and (gun_dry or t - self.laser_fired_t > 1.2)) or \
                    (not laser and gun_dry) or (laser and not charging and not fired and gun_dry):
                pk["phase"], pk["pt"] = "cover", t
        return self._peek_vec(x, y, z, pk)

    def _peek_vec(self, x, y, z, pk):
        gx, gy = pk["C"] if pk["phase"] == "cover" else pk["P"]
        if math.hypot(gx - x, gy - y) < 0.7:
            return 0.0, 0.0
        return self.nav_to(x, y, z, gx, gy)

    # ------------------------------------------------------------------ aiming
    def lead(self, mx, my, mz, tx, ty, tz, tvx, tvy, tvz, speed):
        px, py, pz = tx, ty, tz
        tt = 0.0
        for _ in range(3):
            d = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - mz) ** 2)
            tt = d / speed + 0.05   # +half a decision of latency
            px, py, pz = tx + tvx * tt, ty + tvy * tt, tz + tvz * tt * 0.5
        yaw = math.atan2(py - my, px - mx)
        pitch = math.atan2(pz - mz, math.hypot(px - mx, py - my))
        return yaw, pitch, (px, py, pz)

    def mortar_solution(self, mx, my, mz, tx, ty, tz, high):
        w = self.cfg["weapons"]["mortar"]
        v, g = w["speed"], w["gravity"]
        D = math.hypot(tx - mx, ty - my)
        h = tz - mz
        disc = v ** 4 - g * (g * D * D + 2 * h * v * v)
        if disc < 0 or D < 1:
            return None
        sq = math.sqrt(disc)
        th = math.atan((v * v + sq) / (g * D)) if high else math.atan((v * v - sq) / (g * D))
        return math.atan2(ty - my, tx - mx), th, D / (v * math.cos(th))

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        try:
            return self._act(obs)
        finally:
            dt = (time.perf_counter() - t0) * 1000
            self.times.append(dt)
            self.n += 1
            if self.n % 600 == 0:
                s = sorted(self.times)
                mf.log(f"[timing] n={len(s)} p50={s[len(s)//2]:.2f} p99={s[int(len(s)*0.99)]:.2f} max={s[-1]:.2f} ms")
                self.times = []

    def _act(self, obs):
        self.obs = obs
        self.heavy = False   # set when a cover/peek search ran this tick; A* then waits a tick
        t = self.t = obs["t"]
        self.zone = obs["zone"]
        me = obs["self"]
        x, y, z = me["pos"]
        hp, mhp = me["hp"], me["max_hp"]
        st = me["stats"]
        eye_z = z + st.get("eye_height", 8.0)
        muz_z = z + st.get("muzzle_height", 7.0)
        g = self.geo
        act = {"tick": obs["tick"]}

        # ---------------- offers
        off = obs.get("offer")
        if off:
            ch = [c["id"] for c in off["choices"]]
            act["pick"] = self.pick_upgrade(ch)
            if off.get("id") != self.last_offer:
                self.last_offer = off.get("id")
                u = ch[act["pick"]]
                self.taken[u] = self.taken.get(u, 0) + 1

        # ---------------- events
        for ev in obs.get("events", ()):
            et = ev.get("type")
            if et == "damaged":
                self.dmg_log.append((t, ev["amount"], ev.get("by"), ev.get("bearing"), ev.get("weapon")))
                if ev.get("by") is not None:
                    self.last_combat_t = t
                    b = ev.get("bearing")
                    if b is not None:
                        gd = GHOST_DIST.get(ev.get("weapon"), 100)
                        self.ghosts.append((t, x + math.cos(b) * gd, y + math.sin(b) * gd))
                        self.look_bearing, self.look_until = b, t + 1.5
            elif et == "hit":
                self.last_combat_t = t
            elif et == "take_failed":
                self.items.pop("p:%s" % ev.get("id"), None)
            elif et == "took":
                self.items.pop("p:%s" % ev.get("id"), None)
            elif et == "kill" and ev.get("target") == self.target_id:
                self.target_id = None
        while self.dmg_log and t - self.dmg_log[0][0] > 4.0:
            self.dmg_log.popleft()
        self.ghosts = [gh for gh in self.ghosts if t - gh[0] < 5.0]
        dps_in = sum(d[1] for d in self.dmg_log if t - d[0] < 3.0) / 3.0
        under_fire = any(t - d[0] < 1.0 and d[2] is not None for d in self.dmg_log)

        # ---------------- world: items memory
        for p in obs.get("pickups", ()):
            self.items["p:%d" % p["id"]] = [p["kind"], p["pos"][0], p["pos"][1], p["pos"][2], t]
        for c in obs.get("cubes", ()):
            self.items["c:%d" % c["id"]] = ["cube", c["pos"][0], c["pos"][1], c["pos"][2], t]
        seen_ids = {"p:%d" % p["id"] for p in obs.get("pickups", ())} | {"c:%d" % c["id"] for c in obs.get("cubes", ())}
        for k in list(self.items):
            it = self.items[k]
            dd = math.hypot(it[1] - x, it[2] - y)
            if k not in seen_ids and (dd < 25 or t - it[4] > 90):
                del self.items[k]

        # ---------------- world: crates and drones (they only show while seen; remember them)
        seen_cr = set()
        for c in obs.get("crates", ()):
            self.crates[c["id"]] = [c["pos"][0], c["pos"][1], c["pos"][2], c["hp"], t]
            seen_cr.add(c["id"])
        for cid in list(self.crates):
            if cid in seen_cr:
                continue
            cx_, cy_, cz_, _, ts = self.crates[cid]
            dd = math.hypot(cx_ - x, cy_ - y)
            if t - ts > 150 or (dd < 38 and g.los(x, y, eye_z, cx_, cy_, cz_ + 0.8)):
                del self.crates[cid]   # destroyed (or someone else took it)
        seen_np = set()
        for m in obs.get("mechs", ()):
            if m["kind"] == "npc":
                self.npc_mem[m["id"]] = [m["pos"][0], m["pos"][1], m["pos"][2], t]
                seen_np.add(m["id"])
        for nid in list(self.npc_mem):
            if nid not in seen_np:
                nx_, ny_, _, ts = self.npc_mem[nid]
                if t - ts > 60 or math.hypot(nx_ - x, ny_ - y) < 30:
                    del self.npc_mem[nid]

        # ---------------- enemies
        enemies = []
        npcs = []
        for m in obs.get("mechs", ()):
            mx, my, mz = m["pos"]
            d = math.hypot(mx - x, my - y)
            if m["kind"] == "mech":
                ch = mz + 4.5 * (1 + 0.05 * m.get("cubes", 0))
                vis = g.los(x, y, eye_z, mx, my, ch)
                enemies.append((m, d, vis, ch))
            else:
                npcs.append((m, d, mz + 1.75))
        n_los = sum(1 for e in enemies if e[2] and e[1] < 200)
        n_close = sum(1 for e in enemies if e[1] < 120)
        if n_los:
            self.last_los_t = t
        threats = [(e[0]["pos"][0], e[0]["pos"][1], e[0]["pos"][2]) for e in enemies if e[1] < 260]
        threats += [(gx, gy, g.ground(gx, gy)) for (_, gx, gy) in self.ghosts]
        threats.sort(key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)

        # ---------------- weapons
        w0 = me["weapons"][0]
        w1 = me["weapons"][1] if len(me["weapons"]) > 1 else None
        slots = {}
        for i, w in enumerate(me["weapons"]):
            if w:
                slots[w["kind"]] = (i, w)
        gun = slots.get("gun")
        laser = slots.get("laser")
        lightning = slots.get("lightning")
        mortar = slots.get("mortar")

        # ---------------- target selection
        target = None
        cur = None
        best = 1e18
        attackers = {d_[2] for d_ in self.dmg_log if t - d_[0] < 2.0 and d_[2] is not None}
        any_vis = any(e[2] for e in enemies)
        for e in enemies:
            m, d, vis, ch = e
            sc = d + m.get("hp", 1400) * 0.06 - (70 if vis else 0)
            if vis and m["id"] in attackers:
                sc -= 120          # whoever is shooting us right now
            if not vis and any_vis:
                sc += 200          # never turn toward a hidden enemy while a visible one can shoot us
            if m["id"] == self.target_id:
                cur = e
                sc -= 25
            if sc < best:
                best, target = sc, e
        if target:
            self.target_id = target[0]["id"]

        # ---------------- state machine
        frac = hp / mhp
        # race numbers: time for us to kill the target vs time for incoming fire to kill us
        ttk = ttd = 1e9
        finishable = winning = losing = False
        if target:
            cubes = me.get("cubes", 0)
            my_dps = 95.0 * (1 + 0.04 * min(cubes, 10)) + (90.0 if laser else 0.0) + \
                (110.0 if lightning and target[1] < 45 else 0.0)
            thp = target[0].get("hp", 1400)
            ttk = thp / my_dps
            ttd = hp / dps_in if dps_in > 0 else 1e9
            finishable = target[2] and target[1] < 170 and ttk <= 2.0 and ttd > 1.1 * ttk
            # winning the trade: they are clearly lower than us, or we out-race real incoming fire
            winning = thp / max(target[0].get("max_hp", 1400), 1) < frac - 0.15 or \
                (dps_in > 0 and ttk < 0.6 * ttd)
            losing = ttd < 1.25 * ttk
        self.finishable = finishable
        zr_now = self.zone_at(self.zone, t)[2]
        endgame = zr_now <= 75.0
        self.end_hold = False
        if self.state == "HEAL":
            if frac >= self.HEAL_EXIT or (not threats and frac >= max(0.7, self.PREHEAL) and t - self.last_combat_t > 8):
                self.state = "ROAM"
            if finishable and n_los <= 2:
                self.state = "DUEL"   # never walk away from a kill we can close in ~2 s
        if self.state != "HEAL":
            n_hit_by = len({d[2] for d in self.dmg_log if t - d[0] < 3.0 and d[2] is not None})
            heal = (frac < self.HEAL_BASE
                    or (frac < self.HEAL_MULTI and (n_los >= 2 or n_hit_by >= 2))
                    or (frac < 0.65 and n_los >= 3)
                    or (frac < 0.50 and losing)
                    or ttd < 2.5)
            if self.PREHEAL and frac < self.PREHEAL and n_los == 0 and not finishable:
                heal = True        # no one in sight: top up before the next hunter arrives
            if endgame and frac < 0.8:
                heal = True        # full HP is the win condition in the final circles
            if finishable and n_los <= 2:
                heal = False
            if heal and me["in_zone"]:
                self.state = "HEAL"
                self.cover = None
            elif target and (target[1] < 230 or target[2]):
                self.state = "DUEL"
            else:
                self.state = "ROAM"

        # ---------------- movement intent
        self.peek_active = False
        if self.state != "DUEL":
            self.peek = None
        mvx = mvy = 0.0
        want_boost = False
        jump = False
        aim_yaw = me["aim"][0]
        aim_pitch = 0.0
        aim_set = False
        ax_, ay_, nd = self.zone_anchor(x, y)
        zcx, zcy, zr = self.zone_at(self.zone, t)
        zmargin = zr - math.hypot(x - zcx, y - zcy)
        zone_urgent = (not me["in_zone"]) or zmargin < 15 or not self.zone_ok(x, y, 5.0, 12.0)
        label = self.state

        if zone_urgent:
            mvx, mvy = self.nav_to(x, y, z, ax_, ay_)
            want_boost = True
            label = "ZONE"
        elif self.state == "HEAL":
            if threats and (self.cover is None or t - self.cover_t > (0.35 if self.cover_full else 1.0)):
                if self.cover is None or not self.hidden_from(self.cover[0], self.cover[1],
                                                               g.ground(*self.cover), threats):
                    self.cover = self.find_cover(x, y, z, threats)
                self.cover_t = t
            if self.cover:
                cdist = math.hypot(self.cover[0] - x, self.cover[1] - y)
                me_hidden = bool(threats) and self.hidden_from(x, y, z, threats[:4])
                if me_hidden and cdist < 6.0:
                    label = "HEAL-hidden"          # stand still: silent, regenerating
                elif cdist > 0.6:
                    mvx, mvy = self.nav_to(x, y, z, self.cover[0], self.cover[1])
                    want_boost = cdist > 8
                else:
                    self.cover = None              # on the spot but still seen: search again
            elif threats:
                # no cover: run away from the threat centroid, toward the zone
                cx = sum(tq[0] for tq in threats) / len(threats)
                cy = sum(tq[1] for tq in threats) / len(threats)
                ux, uy = x - cx, y - cy
                ud = math.hypot(ux, uy) or 1.0
                zx, zy = ax_ - x, ay_ - y
                zd = math.hypot(zx, zy) or 1.0
                mvx, mvy = ux / ud + 0.5 * zx / zd, uy / ud + 0.5 * zy / zd
                want_boost = True
        elif self.state == "DUEL" and target and (ovr := self.duel_override(
                x, y, z, me, target, enemies, threats, laser, endgame, finishable, winning, under_fire)) is not None:
            mvx, mvy, want_boost, label = ovr
            jump = jump or self.jump_req
            self.jump_req = False
        elif self.state == "DUEL" and target:
            m, d, vis, ch = target
            tx, ty, tz = m["pos"]
            tw = m.get("weapons", [])
            R = 105.0
            if "lightning" in tw:
                R = 95.0
            if lightning and m.get("hp", 1400) < 0.6 * m.get("max_hp", 1400) and frac > 0.6:
                R = 32.0
            if laser and not lightning:
                R = self.R_LASER
            if finishable:
                R = min(R, 70.0)   # close in and keep firing
                label = "FINISH"
            reloading = gun and gun[1].get("reload", 0) > 0
            pv = None
            if d > 50 and frac > 0.35 and n_los <= 2 and (laser or gun) and not lightning_close(lightning, d) \
                    and not finishable and not winning:
                pv = self.peek_move(x, y, z, me, target, threats, laser, gun)
            if pv is not None:
                self.peek_active = True
                mvx, mvy = pv
                label = "PEEK-" + self.peek["phase"]
                want_boost = self.peek["phase"] == "cover" and math.hypot(
                    self.peek["C"][0] - x, self.peek["C"][1] - y) > 10
            elif vis or d < 60:
                if t > self.strafe_until:
                    self.strafe_sign = self.rng.choice((-1, 1))
                    self.strafe_until = t + self.rng.uniform(0.7, 2.0)
                ux, uy = (tx - x) / (d or 1), (ty - y) / (d or 1)
                radial = clamp((d - R) / 60.0, -1.0, 1.0)
                strafe = 0.85
                if m.get("charging"):
                    strafe = 1.0  # their laser is up: juke hard
                if reloading and not (laser or lightning or mortar) and d > 45:
                    # no peek pair: still duck out of sight while reloading
                    if self.cover is None or t - self.cover_t > 1.0:
                        self.cover = self.find_cover(x, y, z, threats)
                        self.cover_t = t
                    if self.cover:
                        mvx, mvy = self.nav_to(x, y, z, self.cover[0], self.cover[1])
                        strafe = 0.0
                        radial = 0.0
                if strafe or radial:
                    mvx = ux * radial - uy * self.strafe_sign * strafe
                    mvy = uy * radial + ux * self.strafe_sign * strafe
                want_boost = me["energy"] > 70 or (self.boosting and me["energy"] > 30)
            else:
                # no line of sight: hunt only if it is alone and we are healthy
                others = sum(1 for e in enemies if e is not target and
                             math.hypot(e[0]["pos"][0] - tx, e[0]["pos"][1] - ty) < 150)
                if frac > 0.6 and others == 0 and self.zone_ok(tx, ty, 0.0, 5.0):
                    mvx, mvy = self.nav_to(x, y, z, tx, ty)
                else:
                    if nd > 0.55 * self.zone["next_radius"] + 5:
                        mvx, mvy = self.nav_to(x, y, z, ax_, ay_)
        else:
            # ROAM: route to the best-value loot (weapons, cubes, crates, drones, drops), else hold the zone anchor
            slot1_kind = w1["kind"] if w1 else None
            pressured = any(e[2] and e[1] < 160 or e[1] < 90 for e in enemies) or t - self.last_combat_t < 4
            calm = not pressured and frac > 0.5 and not endgame
            best_v, goal = 0.0, None   # goal = (x, y, z, kind, id)
            for k, it in self.items.items():
                kind, ix, iy, iz = it[0], it[1], it[2], it[3]
                dd = math.hypot(ix - x, iy - y)
                need_laser = kind == "laser" and laser is None
                lim = 300 if (need_laser and t < 120) else 230
                if dd > lim or not self.zone_ok(ix, iy, 15.0, dd / 11.0 + 5):
                    continue
                if iz - z > 45:      # beyond one jet climb
                    continue
                if kind == "cube":
                    v = 4.0
                else:
                    if kind in slots or WEAPON_PREF.get(kind, 0) <= WEAPON_PREF.get(slot1_kind, -1):
                        continue
                    v = (40.0 if t < 120 else 14.0) if need_laser else 6.0
                v /= (1.0 + dd / 45.0)
                if v > best_v:
                    best_v, goal = v, (ix, iy, iz, kind, k)
            if calm:
                for cid, (cx_, cy_, cz_, chp, _) in self.crates.items():
                    dd = math.hypot(cx_ - x, cy_ - y)
                    if dd > 200 or cz_ - z > 45 or not self.zone_ok(cx_, cy_, 15.0, dd / 11.0 + 5):
                        continue
                    roof = cz_ > g.ground(cx_, cy_) + 3.0
                    v = (2.4 if roof else 3.0) * (1.2 if chp < 150 else 1.0) / (1.0 + dd / 45.0)
                    if v > best_v:
                        best_v, goal = v, (cx_, cy_, cz_, "crate", cid)
                if frac > 0.7:
                    for nid, (nx_, ny_, nz_, _) in self.npc_mem.items():
                        dd = math.hypot(nx_ - x, ny_ - y)
                        if dd > 180 or not self.zone_ok(nx_, ny_, 15.0, dd / 11.0 + 5):
                            continue
                        v = 3.2 / (1.0 + dd / 45.0)
                        if v > best_v:
                            best_v, goal = v, (nx_, ny_, nz_, "npc", nid)
                for dr in obs.get("drops", ()):
                    dx_, dy_, dz_ = dr["pos"]
                    dd = math.hypot(dx_ - x, dy_ - y)
                    if dd < 170 and dr["lands_in"] < dd / 11.0 + 6 and self.zone_ok(dx_, dy_, 5.0, 10.0):
                        v = 8.0 / (1.0 + dd / 45.0)
                        if v > best_v:
                            best_v, goal = v, (dx_, dy_, dz_, "drop", dr["id"])
            self.farm = None
            if goal is not None:
                gx, gy, gz, kind, gid = goal
                label = "LOOT-" + kind
                dd = math.hypot(gx - x, gy - y)
                if kind in ("crate", "npc"):
                    aim_h = gz + (0.8 if kind == "crate" else 1.75)
                    if dd < (90 if kind == "crate" else 85) and g.los(x, y, muz_z, gx, gy, aim_h):
                        self.farm = (kind, gid, gx, gy, gz, aim_h)   # shoot it from here
                        if kind == "npc" and dd < 50:   # keep outside its aggro/leash ring
                            mvx, mvy = (x - gx) / (dd or 1), (y - gy) / (dd or 1)
                    else:
                        mvx, mvy, jump_now = self.go_to(x, y, z, gx, gy, gz, me)
                        jump = jump or jump_now
                else:
                    mvx, mvy, jump_now = self.go_to(x, y, z, gx, gy, gz, me)
                    jump = jump or jump_now
                want_boost = want_boost or (dd > 70 and me["energy"] > 70)
            else:
                ax2, ay2 = ax_, ay_
                if nd >= 0.55 * self.zone["next_radius"] + 2:
                    mvx, mvy = self.nav_to(x, y, z, ax2, ay2)
                    want_boost = nd > 90 and me["energy"] > 60
                # else: already in a good spot: stand still (silent)

        # ---------------- zone bias & dodge
        if not zone_urgent:
            px, py = x + mvx * 25, y + mvy * 25
            if not self.zone_ok(px, py, 10.0, 5.0):
                cx, cy, _ = self.zone_at(self.zone, t + 5)
                ux, uy = cx - x, cy - y
                ud = math.hypot(ux, uy) or 1
                mvx += ux / ud * 0.8
                mvy += uy / ud * 0.8
        dvx, dvy = self.dodge(x, y, z)
        mvx += dvx
        mvy += dvy

        # ---------------- stuck detection
        wanted = math.hypot(mvx, mvy) > 0.5
        self.hist.append((t, x, y, wanted, math.atan2(mvy, mvx)))
        # no automatic climbing while someone can shoot us (a jetting mech is a free target)
        exposed_fight = n_los > 0 and self.state == "DUEL" and not zone_urgent
        while self.hist and t - self.hist[0][0] > 1.2:
            self.hist.popleft()
        climbing = False
        if t < self.climb_until:
            # jump, then jet (boost in the air) toward the ledge until we are over it
            climbing = True
            mvx, mvy = self.climb_dir
            jump = me["grounded"] and t - self.climb_start < 0.15
            if me["grounded"] and t - self.climb_start > 0.6:
                self.climb_until = 0.0  # landed: done
        elif t < self.stuck_until:
            mvx, mvy = self.unstick
            jump = me["grounded"]
            want_boost = True
        else:
            m_ = math.hypot(mvx, mvy)
            if m_ > 0.5 and me["grounded"] and not exposed_fight:
                ux, uy = mvx / m_, mvy / m_
                rise = self.rise_ahead(x, y, z, ux, uy)
                # terrain bank (not a building) right ahead: hop it instead of sliding along it
                if 1.2 < rise < 14.0 and me["energy"] > 35 and self.terrain_ahead(x, y, z, ux, uy):
                    self.start_climb(t, ux, uy)
                    climbing, jump = True, True
                    mvx, mvy = ux, uy
            cur_dir = math.atan2(mvy, mvx)
            # stuck = pushing the same way for 1.2 s without moving (strafe flip-flops are not stuck)
            if not climbing and wanted and len(self.hist) >= 10 and \
                    all(h[3] and (self.state == "ROAM" or abs(wrap(h[4] - cur_dir)) < 1.2) for h in self.hist) and \
                    math.hypot(self.hist[0][1] - x, self.hist[0][2] - y) < 2.0:
                self.stuck_count += 1
                self.path_t = -99.0
                self.hist.clear()
                ux, uy = mvx / (m_ or 1), mvy / (m_ or 1)
                rise = self.rise_ahead(x, y, z, ux, uy)
                if self.stuck_count % 2 == 1 and me["energy"] > 35 and rise < 55 and not exposed_fight:
                    self.start_climb(t, ux, uy)
                    climbing, jump = True, True
                    mvx, mvy = ux, uy
                else:
                    a = math.atan2(mvy, mvx) + self.rng.choice((-1, 1)) * self.rng.uniform(1.2, 2.2)
                    self.unstick = (math.cos(a), math.sin(a))
                    self.stuck_until = t + 0.8
                    jump = me["grounded"]

        if not climbing:
            mvx, mvy = self.steer(x, y, z, mvx, mvy)

        # ---------------- boost hysteresis
        if want_boost and me["energy"] > 30:
            self.boosting = True
        if not want_boost or me["energy"] < 15:
            self.boosting = False
        if climbing:
            self.boosting = me["energy"] > 1

        # ---------------- aim & fire
        fire0 = fire1 = False
        reload_ = False
        aim_tgt = None  # (yaw, pitch, dist, kind)
        engage = None
        if target and self.state != "ROAM":
            engage = (target[0], target[1], target[2], target[3])
        elif self.state == "ROAM" and self.farm is not None and not zone_urgent:
            kind, fid, fx, fy, fz, fh = self.farm
            fd = math.hypot(fx - x, fy - y)
            if kind == "npc":
                nm = next((n[0] for n in npcs if n[0]["id"] == fid), None)
                if nm is not None:
                    engage = (nm, fd, True, fh)
            else:
                engage = ({"id": fid, "pos": [fx, fy, fz], "vel": [0, 0, 0]}, fd, True, fh)
        elif self.state == "ROAM" and not enemies and frac > 0.55 and not zone_urgent:
            # farm drones and crates for cubes
            cand = [(n[1], n[0], n[2]) for n in npcs if n[1] < 110]
            for c in obs.get("crates", ()):
                cd = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                if cd < 90:
                    cand.append((cd, {"id": c["id"], "pos": c["pos"], "vel": [0, 0, 0]}, c["pos"][2] + 0.8))
            cand.sort(key=lambda c: c[0])
            for cd, cm, cz in cand[:3]:
                if g.los(x, y, muz_z, cm["pos"][0], cm["pos"][1], cz):
                    engage = (cm, cd, True, cz)
                    break

        if engage:
            m, d, vis, ch = engage
            tx, ty, tz = m["pos"]
            tvx, tvy, tvz = m.get("vel", (0, 0, 0))
            spd = gun[1]["stats"]["speed"] if gun else self.gun_cfg["speed"]
            yaw, pitch, lp = self.lead(x, y, muz_z, tx, ty, ch, tvx, tvy, tvz, spd)
            aim_yaw, aim_pitch, aim_set = yaw, pitch, True
            los_shot = g.los(x, y, muz_z, lp[0], lp[1], lp[2]) and g.los(x, y, muz_z, tx, ty, ch)
            ay, ap = me["aim"]
            hd = max(math.hypot(lp[0] - x, lp[1] - y), 1.0)
            rad = 2.5 if m.get("id", 0) < 100 else 1.8
            on_target = abs(wrap(ay - yaw)) * hd < rad * 0.8 + 0.4 and abs(ap - pitch) * hd < 3.0
            in_heal = (self.state == "HEAL" or self.end_hold) and not under_fire and d > 50 and not finishable \
                and not (self.HEAL_FIRE and vis)

            # laser: charge when it can see, release at full charge on target
            if laser and not in_heal:
                li, lw = laser
                charge = lw.get("charge", 0) or 0
                full = lw["stats"]["charge"]
                direct_yaw = math.atan2(ty + tvy * 0.1 - y, tx + tvx * 0.1 - x)
                direct_pitch = math.atan2(ch - muz_z, max(d, 1.0))
                if charge > 0:
                    aim_yaw, aim_pitch = direct_yaw, direct_pitch
                    on_l = abs(wrap(ay - direct_yaw)) * max(d, 1) < rad * 0.7 and \
                        abs(ap - direct_pitch) * max(d, 1) < 3.0
                    hold = charge < full * 0.98 or not (on_l and los_shot)
                    if self.laser_hold_t is not None and t - self.laser_hold_t > full + 1.7:
                        hold = False  # release before the forced auto-fire
                        if not los_shot:
                            self.peek = None  # that firing spot didn't see the target: pick another
                            self.peek_active = False
                    if li == 1:
                        fire1 = hold
                    else:
                        fire0 = hold
                    if not hold:
                        self.laser_hold_t = None
                        self.laser_fired_t = t
                elif lw.get("cooldown", 0) <= 0 and d < lw["stats"]["range"] * 0.95 and not self.kiting and (
                        los_shot or self.hide is not None or (self.peek_active and self.peek["phase"] == "cover"
                                     and math.hypot(self.peek["C"][0] - x, self.peek["C"][1] - y) < 4.0)):
                    self.laser_hold_t = t
                    if li == 1:
                        fire1 = True
                    else:
                        fire0 = True

            # lightning: auto-targets nearest in its cone
            if lightning and not in_heal:
                li, lw = lightning
                ls = lw["stats"]
                if d < ls["range"] * 0.92:
                    aim_yaw = math.atan2(ty - y, tx - x)
                    aim_pitch = math.atan2(ch - muz_z, max(d, 1.0))
                    if abs(wrap(ay - aim_yaw)) < math.radians(ls["cone_deg"]) * 0.4 and vis and \
                            me["energy"] > ls["energy"] + 8 and lw.get("cooldown", 0) <= 0:
                        if li == 1:
                            fire1 = True
                        else:
                            fire0 = True

            # mortar: lob when no line of sight, or while the gun reloads
            if mortar and not in_heal and m.get("id", 0) < 100:
                mi, mw = mortar
                reloading = gun and gun[1].get("reload", 0) > 0
                if mw.get("cooldown", 0) <= 0 and 35 < d < 220 and (reloading or not los_shot):
                    sol = self.mortar_solution(x, y, muz_z + 2.5, tx, ty, tz, high=not los_shot)
                    if sol:
                        myaw, mpitch, ft = sol
                        sol2 = self.mortar_solution(x, y, muz_z + 2.5, tx + tvx * ft, ty + tvy * ft, tz,
                                                    high=not los_shot)
                        if sol2:
                            myaw, mpitch, _ = sol2
                        aim_yaw, aim_pitch = myaw, mpitch
                        if abs(wrap(ay - myaw)) < 0.03 and abs(ap - mpitch) < 0.03:
                            if mi == 1:
                                fire1 = True
                            else:
                                fire0 = True

            # gun
            if gun and not in_heal:
                gi, gw = gun
                gs = gw["stats"]
                if gw.get("ammo", 1) > 0 and gw.get("reload", 0) <= 0 and los_shot and on_target \
                        and d < gs["range"] * (0.8 if m.get("id", 0) < 100 else 0.6):
                    if gi == 0:
                        fire0 = True
                    else:
                        fire1 = True
        else:
            self.laser_hold_t = None

        if gun:
            gw = gun[1]
            if self.peek_active and self.peek["phase"] == "cover" and gw.get("reload", 0) <= 0 and \
                    gw.get("ammo", 12) < gw["stats"]["mag"] and not fire0 and not fire1:
                reload_ = True
            if gw.get("reload", 0) <= 0:
                if gw.get("ammo", 1) == 0:
                    reload_ = True
                elif gw.get("ammo", 12) < gw["stats"]["mag"] * 0.5 and t - self.last_los_t > 1.5 and not engage:
                    reload_ = True

        if not aim_set:
            # face threats (front armor), else a recent sound/hit, else sweep along movement
            if self.state == "HEAL" and threats:
                tx, ty, _ = min(threats, key=lambda q: math.hypot(q[0] - x, q[1] - y))
                aim_yaw = math.atan2(ty - y, tx - x)
            elif t < self.look_until and self.look_bearing is not None:
                aim_yaw = self.look_bearing
            else:
                for s in obs.get("sounds", ()):
                    if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost") \
                            and s["dist"] != "far":
                        self.look_bearing, self.look_until = s["bearing"], t + 1.2
                        aim_yaw = s["bearing"]
                        break
                else:
                    base = math.atan2(mvy, mvx) if math.hypot(mvx, mvy) > 0.3 else \
                        math.atan2(self.zone["next_center"][1] - y, self.zone["next_center"][0] - x)
                    aim_yaw = base + 1.0 * math.sin(t * 0.9)
            aim_pitch = 0.0

        # ---------------- loot pickup
        for p in obs.get("pickups", ()):
            px, py, pz = p["pos"]
            if math.hypot(px - x, py - y) <= self.reach - 0.3 and abs(pz - z) < 3.8:
                k = p["kind"]
                if k in slots or k == "gun":
                    continue
                if w1 is None:
                    act["take"] = p["id"]
                    break
                if w1["kind"] != "laser" and WEAPON_PREF.get(k, 0) > WEAPON_PREF.get(w1["kind"], 0) and not (w1["kind"] == "laser" and (w1.get("charge") or 0) > 0):
                    act["take"] = p["id"]
                    act["drop"] = 1
                    break

        vis_sh = [e for e in enemies if e[2] and e[1] < 230]
        if vis_sh:
            att = [e for e in vis_sh if e[0]["id"] in attackers]
            e = min(att or vis_sh, key=lambda q: q[1])
            brg = math.atan2(e[0]["pos"][1] - y, e[0]["pos"][0] - x)
            if abs(wrap(aim_yaw - brg)) > 1.35:      # outside the 180-degree front arc (with margin)
                aim_yaw = brg
                aim_pitch = math.atan2(e[3] - muz_z, max(e[1], 1.0))

        act["move"] = [round(mvx, 3), round(mvy, 3)]
        act["boost"] = self.boosting
        act["jump"] = bool(jump)
        act["aim"] = [round(wrap(aim_yaw), 4), round(clamp(aim_pitch, st.get("min_pitch", -0.7), st.get("max_pitch", 1.3)), 4)]
        act["fire"] = [bool(fire0), bool(fire1)]
        if reload_:
            act["reload"] = True
        mf.draw.label(f"{label} hp{hp:.0f} e{len(enemies)}")
        return act


if __name__ == "__main__":
    mf.run(Bot)
