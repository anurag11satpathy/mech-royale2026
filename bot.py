"""Mech Royale v1: lead-aiming gunner with duel / heal-in-cover / zone-rotation states.

All logging goes to stderr (mf.log). Reads only data/map.bin (shipped) and writes nothing to disk.
"""
import math, os, random, time
from collections import deque

import mechfight as mf
from geo import Geo, AStar

HERE = os.path.dirname(os.path.abspath(__file__))
PI = math.pi
TAU = 2 * math.pi

UPGRADE_PRIORITY = [
    "armor", "plating", "nanites", "gyros", "laser_charge", "laser_damage", "lightning_efficiency",
    "gun_damage", "gun_rate", "servos", "gun_mag", "capacitor", "jets", "reactor", "lightning_range",
    "sensors", "gun_velocity", "lightning_chain", "mortar_reload", "mortar_velocity", "mortar_splash",
    "laser_pierce",
]
WEAPON_PREF = {"laser": 3.0, "lightning": 2.5, "mortar": 1.0, "gun": 0.0}
GHOST_DIST = {"gun": 120, "laser": 150, "mortar": 160, "lightning": 35, "npc": 40}


def wrap(a):
    return (a + PI) % TAU - PI


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Bot:
    name = "anurag-v1"

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
        self.times = []
        self.n = 0

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
        keep = 0.55 * nr
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

    def start_climb(self, t, ux, uy):
        self.climb_start = t
        self.climb_until = t + 2.5
        self.climb_dir = (ux, uy)

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
            if self.planner.step(450):
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

    def find_cover(self, x, y, z, threats):
        g = self.geo
        best, bs = None, 1e18
        off = self.rng.random() * TAU
        for r in (9.0, 18.0, 30.0, 45.0):
            for k in range(12):
                a = off + k * (TAU / 12)
                px, py = x + math.cos(a) * r, y + math.sin(a) * r
                c = g.ncell(px, py)
                if g.NAV[c] != 1:   # free and not hugging a wall corner
                    continue
                if not self.zone_ok(px, py, 10.0, 10.0):
                    continue
                gz = g.ground(px, py)
                if not self.hidden_from(px, py, gz, threats):
                    continue
                sc = r
                for tx, ty, _ in threats:
                    td = math.hypot(px - tx, py - ty)
                    sc -= min(td, 200.0) * 0.25
                if sc < bs:
                    bs, best = sc, (px, py)
            if best is not None and r >= 18.0:
                break
        return best

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
            pr = {u: i for i, u in enumerate(UPGRADE_PRIORITY)}
            act["pick"] = min(range(len(ch)), key=lambda i: pr.get(ch[i], 99))

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
        for e in enemies:
            m, d, vis, ch = e
            sc = d + m.get("hp", 1400) * 0.06 - (70 if vis else 0)
            if m["id"] == self.target_id:
                cur = e
                sc *= 0.75
            if sc < best:
                best, target = sc, e
        if target:
            self.target_id = target[0]["id"]

        # ---------------- state machine
        frac = hp / mhp
        if self.state == "HEAL":
            if frac >= 0.9 or (not threats and frac >= 0.75 and t - self.last_combat_t > 10):
                self.state = "ROAM"
            # opportunistic finish: lone, nearly dead target in sight
            if target and target[2] and target[0].get("hp", 1400) < 0.15 * target[0].get("max_hp", 1400) \
                    and n_los == 1 and target[1] < 120 and hp > 2.5 * target[0].get("hp", 1400):
                self.state = "DUEL"
        if self.state != "HEAL":
            heal = (frac < 0.40 or (frac < 0.62 and n_los >= 2) or (frac < 0.75 and n_los >= 3)
                    or (frac < 0.55 and dps_in > 160))
            if heal and me["in_zone"]:
                self.state = "HEAL"
                self.cover = None
            elif target and (target[1] < 230 or target[2]):
                self.state = "DUEL"
            else:
                self.state = "ROAM"

        # ---------------- movement intent
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
            if threats and (self.cover is None or t - self.cover_t > 0.6):
                if self.cover is None or not self.hidden_from(self.cover[0], self.cover[1],
                                                               g.ground(*self.cover), threats):
                    self.cover = self.find_cover(x, y, z, threats)
                self.cover_t = t
            if self.cover:
                cdist = math.hypot(self.cover[0] - x, self.cover[1] - y)
                if cdist > 2.0:
                    mvx, mvy = self.nav_to(x, y, z, self.cover[0], self.cover[1])
                    want_boost = cdist > 8
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
                R = 130.0
            reloading = gun and gun[1].get("reload", 0) > 0
            if vis or d < 60:
                if t > self.strafe_until:
                    self.strafe_sign = self.rng.choice((-1, 1))
                    self.strafe_until = t + self.rng.uniform(0.7, 2.0)
                ux, uy = (tx - x) / (d or 1), (ty - y) / (d or 1)
                radial = clamp((d - R) / 60.0, -1.0, 1.0)
                strafe = 0.85
                if m.get("charging"):
                    strafe = 1.0  # their laser is up: juke hard
                if reloading and not (laser or lightning or mortar):
                    # peek cycle: duck out of sight while reloading
                    if self.cover is None or t - self.cover_t > 1.0:
                        self.cover = self.find_cover(x, y, z, threats[:3])
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
            # ROAM: loot, cubes, supply drop, farm, else hold near the zone anchor
            goal = None
            slot1_kind = w1["kind"] if w1 else None
            best_v = 0.0
            danger = bool(enemies) or t - self.last_combat_t < 4
            for k, it in self.items.items():
                kind, ix, iy, iz = it[0], it[1], it[2], it[3]
                dd = math.hypot(ix - x, iy - y)
                if not self.zone_ok(ix, iy, 15.0, dd / 11.0 + 5):
                    continue
                if kind == "cube":
                    v = 2.0 if dd < 80 else 0.0
                else:
                    if kind in slots or WEAPON_PREF.get(kind, 0) <= WEAPON_PREF.get(slot1_kind, -1):
                        continue
                    if abs(iz - z) > 20 and dd > 30:  # rooftop loot: skip in v1
                        continue
                    v = 3.0 + WEAPON_PREF[kind] if dd < 160 else 0.0
                if v <= 0:
                    continue
                v = v / (1.0 + dd / 40.0)
                if v > best_v:
                    best_v, goal = v, (ix, iy)
            if goal is None and not danger:
                for dr in obs.get("drops", ()):
                    dx_, dy_, _ = dr["pos"]
                    dd = math.hypot(dx_ - x, dy_ - y)
                    if dd < 170 and dr["lands_in"] < dd / 11.0 + 6 and self.zone_ok(dx_, dy_, 5.0, 10.0):
                        goal = (dx_, dy_)
                        label = "DROP"
                        break
            if goal is None:
                goal = (ax_, ay_)
                if nd < 0.55 * self.zone["next_radius"] + 2:
                    goal = None  # already in a good spot: stand still (silent)
            if goal:
                mvx, mvy = self.nav_to(x, y, z, goal[0], goal[1])
                want_boost = math.hypot(goal[0] - x, goal[1] - y) > 90 and me["energy"] > 60

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
        self.hist.append((t, x, y, wanted))
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
            if m_ > 0.5 and me["grounded"]:
                ux, uy = mvx / m_, mvy / m_
                rise = self.rise_ahead(x, y, z, ux, uy)
                # terrain bank (not a building) right ahead: hop it instead of sliding along it
                if 1.2 < rise < 14.0 and me["energy"] > 35 and self.terrain_ahead(x, y, z, ux, uy):
                    self.start_climb(t, ux, uy)
                    climbing, jump = True, True
                    mvx, mvy = ux, uy
            if not climbing and wanted and len(self.hist) >= 10 and all(h[3] for h in self.hist) and \
                    math.hypot(self.hist[0][1] - x, self.hist[0][2] - y) < 2.0:
                self.stuck_count += 1
                self.path_t = -99.0
                self.hist.clear()
                ux, uy = mvx / (m_ or 1), mvy / (m_ or 1)
                rise = self.rise_ahead(x, y, z, ux, uy)
                if self.stuck_count % 2 == 1 and me["energy"] > 35 and rise < 55:
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
            in_heal = self.state == "HEAL" and not under_fire and d > 50

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
                    if self.laser_hold_t is not None and t - self.laser_hold_t > full + 1.6:
                        hold = False  # don't let it auto-fire wildly
                    if li == 1:
                        fire1 = hold
                    else:
                        fire0 = hold
                    if not hold:
                        self.laser_hold_t = None
                elif lw.get("cooldown", 0) <= 0 and los_shot and d < lw["stats"]["range"] * 0.95:
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
                if WEAPON_PREF.get(k, 0) > WEAPON_PREF.get(w1["kind"], 0) and not (w1["kind"] == "laser" and (w1.get("charge") or 0) > 0):
                    act["take"] = p["id"]
                    act["drop"] = 1
                    break

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
