"""Storm-win bot: survive to the final circle with max HP and outlast the squad in the 260 HP/s race.

STORM_MODE:
  "pacifist"  never shoots a mech. Farms crates/drones for cubes, breaks line of sight the moment a hunter sees us,
              camps high roofs or silent cover (standing still / < 5 m/s is silent), grabs supply drops, and sits on
              the final circle's centre when the zone closes.
  "hybrid"    plays v7 (bot.Bot) until it has HYBRID_KILLS kills (or HYBRID_T seconds), then flips to the pacifist
              storm plan: a ~v7 floor of early kills with the 15-point storm-win upside.

Reuses v7's map, navigation, cover search and climb helpers; logs only to stderr; no disk writes.
"""
import math

import mechfight as mf
from bot import Bot, wrap, clamp, TAU, PI

STORM_UPGRADES = ["plating", "servos", "jets", "capacitor", "nanites", "armor", "reactor", "gyros", "sensors",
                  "gun_mag", "gun_damage", "gun_rate", "laser_damage", "laser_charge", "gun_velocity",
                  "lightning_efficiency", "lightning_range", "lightning_chain", "mortar_reload", "mortar_velocity",
                  "mortar_splash", "laser_pierce"]

_ROOFS = None   # cached per process: [(x, y, top, height_above_ground)]


def roof_spots(geo):
    """Interior rooftop cells (flat within 1 m for 3 cells around), 15-58 m above the ground."""
    global _ROOFS
    if _ROOFS is not None:
        return _ROOFS
    G, B, T, nx, ny = geo.G, geo.B, geo.T, geo.nx, geo.ny
    out = []
    for j in range(3, ny - 3, 2):
        for i in range(3, nx - 3, 2):
            c = j * nx + i
            top = T[c]
            if top < -1e8:
                continue
            h = top - G[c]
            if not (15.0 <= h <= 58.0) or B[c] > G[c] + 1.0:
                continue
            ok = True
            for dj in (-3, 0, 3):
                for di in (-3, 0, 3):
                    cc = (j + dj) * nx + i + di
                    if abs(T[cc] - top) > 1.0:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                out.append((geo.x0 + (i + 0.5) * geo.cs, geo.y0 + (j + 0.5) * geo.cs, top, h))
    _ROOFS = out
    return out


class StormBot(Bot):
    name = "anurag-pacifist"
    STORM_MODE = "pacifist"     # "pacifist" | "hybrid"  (toggle here)
    HYBRID_KILLS = 3
    HYBRID_T = 150.0            # hybrid also flips at this time even with fewer kills
    CUBE_GOAL = 12              # farm until this many cubes ...
    FARM_UNTIL = 230.0          # ... or this time, then hide
    USE_ROOFS = True
    SNEAK = 0.4                 # move fraction while sneaking (0.4 x 11.7 = 4.7 m/s: silent)

    def __init__(self, hello):
        super().__init__(hello)
        self.roofs = roof_spots(self.geo) if self.USE_ROOFS else []
        self.storm = self.STORM_MODE == "pacifist"
        self.hide_spot = None       # (x, y, z, kind)
        self.hide_t = -99.0
        self.evade_spot = None
        self.evade_t = -99.0
        self.s_hist = []
        self.stats_roof_ticks = 0
        self.stats_loud_ticks = 0

    # ------------------------------------------------------------------ helpers
    def pick_storm_upgrade(self, choices):
        rank = {u: i for i, u in enumerate(STORM_UPGRADES)}
        return min(range(len(choices)), key=lambda i: rank.get(choices[i], 99))

    def safe_later(self, px, py, ahead, margin):
        cx, cy, r = self.zone_at(self.zone, self.t + ahead)
        return math.hypot(px - cx, py - cy) < r - margin

    def choose_hide(self, x, y, z, threats, energy):
        """High roof inside the circle we will be in a minute from now, else ground cover near the next centre."""
        nx_, ny_ = self.zone["next_center"]
        nr = self.zone["next_radius"]
        best, bs = None, 1e18
        max_h = max(0.0, (energy + (30 if self.taken.get("capacitor") else 0) - 15) / 1.6)
        for (rx, ry, top, h) in self.roofs:
            if h > max_h + 6:                      # we'll recharge on the way, a little slack
                continue
            dn = math.hypot(rx - nx_, ry - ny_)
            if dn > max(nr - 20.0, 0.0) or not self.safe_later(rx, ry, 60.0, 15.0):
                continue
            d = math.hypot(rx - x, ry - y)
            if d > 450:
                continue
            sc = d - min(h, 45.0) * 2.0 + 0.3 * dn
            for tx, ty, _ in threats:
                sc += 120.0 * max(0.0, 1 - math.hypot(rx - tx, ry - ty) / 120.0)
            if sc < bs:
                bs, best = sc, (rx, ry, top, "roof")
        if best is not None:
            return best
        if threats:
            c = self.find_cover(x, y, z, threats)
            if c is not None:
                return (c[0], c[1], self.geo.ground(c[0], c[1]), "cover")
        ax, ay, _ = self.zone_anchor(x, y)
        return (ax, ay, self.geo.ground(ax, ay), "anchor")

    # ------------------------------------------------------------------ main
    def _act(self, obs):
        t = obs["t"]
        # hybrid: play v7 until the flip
        if not self.storm:
            for ev in obs.get("events", ()):
                pass
            if self.kills >= self.HYBRID_KILLS or t >= self.HYBRID_T:
                self.storm = True
                mf.log(f"[storm] flip to storm plan at t={t:.1f} kills={self.kills}")
            else:
                return super()._act(obs)
        return self.storm_act(obs)

    def storm_act(self, obs):
        self.obs = obs
        self.heavy = False
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
        label = "STORM"

        off = obs.get("offer")
        if off:
            act["pick"] = self.pick_storm_upgrade([c["id"] for c in off["choices"]])

        # ---- events / memory (same bookkeeping as v7)
        for ev in obs.get("events", ()):
            et = ev.get("type")
            if et == "damaged" and ev.get("by") is not None:
                self.dmg_log.append((t, ev["amount"], ev.get("by"), ev.get("bearing"), ev.get("weapon")))
                self.last_combat_t = t
                b = ev.get("bearing")
                if b is not None:
                    self.ghosts.append((t, x + math.cos(b) * 100, y + math.sin(b) * 100))
            elif et == "kill" and (ev.get("target") or 0) < 100:
                self.kills += 1
            elif et in ("take_failed", "took"):
                self.items.pop("p:%s" % ev.get("id"), None)
        while self.dmg_log and t - self.dmg_log[0][0] > 4.0:
            self.dmg_log.popleft()
        self.ghosts = [gh for gh in self.ghosts if t - gh[0] < 6.0]
        for c in obs.get("cubes", ()):
            self.items["c:%d" % c["id"]] = ["cube", c["pos"][0], c["pos"][1], c["pos"][2], t]
        seen_cu = {"c:%d" % c["id"] for c in obs.get("cubes", ())}
        for k in [k for k in self.items if k.startswith("c:")]:
            it = self.items[k]
            if k not in seen_cu and (math.hypot(it[1] - x, it[2] - y) < 20 or t - it[4] > 90):
                del self.items[k]
        for c in obs.get("crates", ()):
            self.crates[c["id"]] = [c["pos"][0], c["pos"][1], c["pos"][2], c["hp"], t]
        seen_cr = {c["id"] for c in obs.get("crates", ())}
        for cid in list(self.crates):
            cx_, cy_, cz_, _, ts = self.crates[cid]
            if cid not in seen_cr and (t - ts > 150 or (math.hypot(cx_ - x, cy_ - y) < 38 and g.los(x, y, eye_z, cx_, cy_, cz_ + 0.8))):
                del self.crates[cid]
        npcs = []
        enemies = []
        for m in obs.get("mechs", ()):
            mx, my, mz = m["pos"]
            d = math.hypot(mx - x, my - y)
            if m["kind"] == "mech":
                enemies.append((m, d, g.los(x, y, eye_z, mx, my, mz + 4.5)))
            else:
                npcs.append((m, d))
                self.npc_mem[m["id"]] = [mx, my, mz, t]
        threats = [(e[0]["pos"][0], e[0]["pos"][1], e[0]["pos"][2]) for e in enemies if e[1] < 300]
        threats += [(gx, gy, g.ground(gx, gy)) for (_, gx, gy) in self.ghosts]
        threats.sort(key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)
        seen_by = [e for e in enemies if e[2] and e[1] < 210]
        hit_recent = any(t - d_[0] < 1.5 for d_ in self.dmg_log)
        gun = me["weapons"][0] if me["weapons"] and me["weapons"][0] and me["weapons"][0]["kind"] == "gun" else None

        # ---- zone state
        zcx, zcy, zr = self.zone_at(self.zone, t)
        zmargin = zr - math.hypot(x - zcx, y - zcy)
        zone_urgent = (not me["in_zone"]) or zmargin < 12 or not self.zone_ok(x, y, 4.0, 10.0)
        final = zr <= 75.0
        frac = hp / mhp

        mvx = mvy = 0.0
        boost = False
        jump = False
        aim_yaw = me["aim"][0]
        aim_pitch = 0.0
        fire0 = False
        shoot = None   # (x, y, z_aim) farm target to shoot

        # ---- priorities
        if zone_urgent:
            gx, gy = self.zone["next_center"] if final else self.zone_anchor(x, y)[:2]
            mvx, mvy = self.nav_to(x, y, z, gx, gy)
            boost = True
            label = "ZONE"
        elif seen_by or hit_recent:
            # break line of sight now; never shoot back
            if self.evade_spot is None or t - self.evade_t > 0.6 or \
                    not self.hidden_from(self.evade_spot[0], self.evade_spot[1], g.ground(*self.evade_spot), threats[:4]):
                self.evade_spot = self.find_cover(x, y, z, threats[:4])
                self.evade_t = t
            if self.evade_spot:
                ex, ey = self.evade_spot
                mvx, mvy = self.nav_to(x, y, z, ex, ey) if math.hypot(ex - x, ey - y) > 0.8 else (0.0, 0.0)
                if not seen_by:
                    # out of sight: go quiet (boost/steps over 5 m/s are audible within 90/40 m)
                    m_ = math.hypot(mvx, mvy)
                    if m_ > 1e-3:
                        mvx, mvy = mvx / m_ * self.SNEAK, mvy / m_ * self.SNEAK
            elif threats:
                cx = sum(q[0] for q in threats[:4]) / len(threats[:4]); cy = sum(q[1] for q in threats[:4]) / len(threats[:4])
                ux, uy = x - cx, y - cy
                ud = math.hypot(ux, uy) or 1.0
                zx, zy = zcx - x, zcy - y
                zd = math.hypot(zx, zy) or 1.0
                mvx, mvy = ux / ud + 0.6 * zx / zd, uy / ud + 0.6 * zy / zd
            boost = bool(seen_by)          # sprint only while someone can actually see us
            self.hide_spot = None
            label = "EVADE" if seen_by else "EVADE-quiet"
        else:
            self.evade_spot = None
            goal = None
            # supply drop: full HP + 3 cubes
            dr = self.drop_target(x, y, me, 0) if self.USE_DROPS else None
            if dr is not None and not threats[:1] or (dr is not None and math.hypot(threats[0][0] - dr["pos"][0], threats[0][1] - dr["pos"][1]) > 90):
                goal = (dr["pos"][0], dr["pos"][1], dr["pos"][2], "drop")
            # farm (not in the final circles)
            if goal is None and not final and me.get("cubes", 0) < self.CUBE_GOAL and t < self.FARM_UNTIL and frac > 0.5:
                best_v = 0.0
                for k, it in self.items.items():
                    if it[0] != "cube":
                        continue
                    dd = math.hypot(it[1] - x, it[2] - y)
                    if dd < 200 and it[3] - z < 40 and self.zone_ok(it[1], it[2], 15.0, dd / 11 + 5):
                        v = 5.0 / (1 + dd / 45)
                        if v > best_v:
                            best_v, goal = v, (it[1], it[2], it[3], "cube")
                for cid, (cx_, cy_, cz_, chp, _) in self.crates.items():
                    dd = math.hypot(cx_ - x, cy_ - y)
                    if dd < 250 and cz_ - z < 45 and self.zone_ok(cx_, cy_, 15.0, dd / 11 + 5):
                        v = 3.0 / (1 + dd / 45)
                        if v > best_v:
                            best_v, goal = v, (cx_, cy_, cz_, "crate", cid)
                for nid, (nx_, ny_, nz_, _) in self.npc_mem.items():
                    dd = math.hypot(nx_ - x, ny_ - y)
                    if dd < 200 and frac > 0.7 and self.zone_ok(nx_, ny_, 15.0, dd / 11 + 5):
                        v = 3.4 / (1 + dd / 45)
                        if v > best_v:
                            best_v, goal = v, (nx_, ny_, nz_, "npc", nid)
            if goal is not None:
                gx, gy, gz, kind = goal[:4]
                dd = math.hypot(gx - x, gy - y)
                label = "FARM-" + kind if kind != "drop" else "DROP"
                if kind in ("crate", "npc"):
                    h = gz + (0.8 if kind == "crate" else 1.75)
                    if dd < 85 and g.los(x, y, muz_z, gx, gy, h):
                        shoot = (gx, gy, h)
                        if kind == "npc" and dd < 50:
                            mvx, mvy = (x - gx) / (dd or 1), (y - gy) / (dd or 1)
                    else:
                        mvx, mvy, jump = self.go_to(x, y, z, gx, gy, gz, me)
                elif kind == "drop" and dd < 4.0:
                    mvx, mvy = (gx - x) / 4.0, (gy - y) / 4.0
                else:
                    mvx, mvy, jump = self.go_to(x, y, z, gx, gy, gz, me)
                boost = dd > 60 and me["energy"] > 70 and not threats
            else:
                # hide: high roof or silent cover, re-chosen as the zone moves
                if final:
                    fx, fy = self.zone["next_center"]
                    if self.zone["next_radius"] <= 1.0 or zr <= 30.0:
                        self.hide_spot = (fx, fy, g.surface(fx, fy, z + 60), "centre")
                    elif self.hide_spot is None or t - self.hide_t > 3.0:
                        sp = self.find_cover(fx, fy, g.ground(fx, fy), threats[:4]) if threats else None
                        self.hide_spot = (sp[0], sp[1], g.ground(*sp), "final-cover") if sp else (fx, fy, g.ground(fx, fy), "centre")
                        self.hide_t = t
                elif self.hide_spot is None or t - self.hide_t > 8.0 or not self.safe_later(self.hide_spot[0], self.hide_spot[1], 30.0, 10.0):
                    self.hide_spot = self.choose_hide(x, y, z, threats[:4], me["energy"])
                    self.hide_t = t
                hx, hy, hz, kind = self.hide_spot
                dd = math.hypot(hx - x, hy - y)
                label = "HIDE-" + kind
                if dd > 1.2 or hz > z + 3.0:
                    mvx, mvy, jump = self.go_to(x, y, z, hx, hy, hz, me)
                    if not jump and not (t < self.climb_until):
                        # sneak (silent) when hunters are known nearby, otherwise walk
                        sneak = threats and math.hypot(threats[0][0] - x, threats[0][1] - y) < 140
                        m_ = math.hypot(mvx, mvy) or 1.0
                        k_ = self.SNEAK if sneak else 1.0
                        mvx, mvy = mvx / m_ * k_, mvy / m_ * k_
                        boost = (not sneak) and dd > 120 and me["energy"] > 80
                else:
                    mvx = mvy = 0.0          # standing still: silent, regenerating
                    if kind == "roof" or z > g.ground(x, y) + 10:
                        self.stats_roof_ticks += 1

        # ---- climb execution + stuck handling (v7 logic, simplified)
        climbing = False
        if t < self.climb_until:
            climbing = True
            mvx, mvy = self.climb_dir
            jump = me["grounded"] and t - self.climb_start < 0.15
            if me["grounded"] and t - self.climb_start > 0.6:
                self.climb_until = 0.0
        else:
            wanted = math.hypot(mvx, mvy) > 0.35
            self.s_hist.append((t, x, y, wanted, math.atan2(mvy, mvx)))
            self.s_hist = [h for h in self.s_hist if t - h[0] <= 1.5]
            if wanted and len(self.s_hist) >= 12 and all(h[3] for h in self.s_hist) and \
                    math.hypot(self.s_hist[0][1] - x, self.s_hist[0][2] - y) < 1.5:
                m_ = math.hypot(mvx, mvy) or 1.0
                ux, uy = mvx / m_, mvy / m_
                self.s_hist = []
                self.path_t = -99.0
                if me["energy"] > 35 and self.rise_ahead(x, y, z, ux, uy) < 55:
                    self.start_climb(t, ux, uy, 3.0)
                    climbing, jump = True, me["grounded"]
                    mvx, mvy = ux, uy
                else:
                    a = math.atan2(uy, ux) + 1.6
                    mvx, mvy = math.cos(a), math.sin(a)
        if not climbing:
            dvx, dvy = self.dodge(x, y, z)
            mvx += dvx; mvy += dvy
            m_ = math.hypot(mvx, mvy)
            if m_ > 1e-3:
                sx, sy = self.steer(x, y, z, mvx, mvy)
                mvx, mvy = sx, sy
        boost = (me["energy"] > 1) if climbing else (boost and me["energy"] > 12)

        # ---- aim: shoot farm targets; otherwise face the nearest threat (front armor) or scan
        if shoot is not None:
            sx, sy, sz = shoot
            aim_yaw = math.atan2(sy - y, sx - x)
            aim_pitch = math.atan2(sz - muz_z, max(math.hypot(sx - x, sy - y), 1.0))
            ay, ap = me["aim"]
            dd = max(math.hypot(sx - x, sy - y), 1.0)
            if gun and gun.get("ammo", 1) > 0 and gun.get("reload", 0) <= 0 and \
                    abs(wrap(ay - aim_yaw)) * dd < 1.4 and abs(ap - aim_pitch) * dd < 2.5:
                fire0 = True
        elif threats:
            tx, ty, _ = threats[0]
            aim_yaw = math.atan2(ty - y, tx - x)
        else:
            base = math.atan2(mvy, mvx) if math.hypot(mvx, mvy) > 0.3 else me["aim"][0]
            aim_yaw = base + 1.2 * math.sin(t * 0.7)

        reload_ = bool(gun and gun.get("reload", 0) <= 0 and gun.get("ammo", 12) < gun["stats"]["mag"] and shoot is None)
        # loud = moving faster than the 5 m/s step threshold while hunters are near
        spd = math.hypot(*me["vel"][:2]) if me.get("vel") else 0.0
        if spd > 5.0 and threats and math.hypot(threats[0][0] - x, threats[0][1] - y) < 120:
            self.stats_loud_ticks += 1

        act["move"] = [round(mvx, 3), round(mvy, 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        act["aim"] = [round(wrap(aim_yaw), 4), round(clamp(aim_pitch, st.get("min_pitch", -0.7), st.get("max_pitch", 1.3)), 4)]
        act["fire"] = [bool(fire0), False]
        if reload_:
            act["reload"] = True
        mf.draw.label(f"{label} hp{hp:.0f}")
        return act


if __name__ == "__main__":
    mf.run(StormBot)
