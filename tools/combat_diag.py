"""Combat / DPS audit of a bot in the calibrated gauntlet sim (tools/gauntlet_sim.py --cal).
python3 tools/combat_diag.py [--bot bot.py] [--trials N] [--seed S]"""
import argparse, collections, math, os, random, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
import gauntlet_sim as G  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--bot", default=os.path.join(ROOT, "bot.py"))
ap.add_argument("--trials", type=int, default=30)
ap.add_argument("--seed", type=int, default=111)
args = ap.parse_args()
G.DIFF.update(spread=0.8, react=0.8, lhit=0.1, hp=1.4)          # --cal preset
mod = G.load_bot(args.bot)
g = G.Geo(os.path.join(ROOT, "data", "map.bin"))
orig = mod.Bot._act
C = collections.Counter()
F = collections.Counter()      # float sums
at4 = []                       # (t, cubes, upgrades) at the 4th kill
death = []
cur = {}


def w(self, obs):
    me = obs["self"]
    x, y, z = me["pos"]
    # incoming / outgoing damage
    for e in obs["events"]:
        if e["type"] == "damaged" and e.get("by") is not None:
            F["taken_" + e["weapon"] + ("_rear" if e.get("rear") else "")] += e["amount"]
        if e["type"] == "hit" and (e.get("target") or 0) < 100:
            F["dealt_mech_" + e["weapon"]] += e["amount"]
        if e["type"] == "kill":
            cur["kills"] = cur.get("kills", 0) + 1
            if cur["kills"] == 4:
                at4.append((obs["t"], me.get("cubes", 0), sorted(me.get("upgrades", []))))
    a = orig(self, obs)
    vis = [m for m in obs["mechs"] if m["kind"] == "mech" and
           math.hypot(m["pos"][0] - x, m["pos"][1] - y) < 184 and
           self.geo.los(x, y, z + 8, m["pos"][0], m["pos"][1], m["pos"][2] + 4.5)]
    C["ticks"] += 1
    if not vis:
        return a
    C["eng"] += 1
    C["eng_state_" + self.state] += 1
    gun = me["weapons"][0]
    w1 = me["weapons"][1]
    if a["fire"][0]:
        C["gun_fire"] += 1
    else:
        why = ("reload" if gun.get("reload", 0) > 0 or gun.get("ammo", 1) <= 0 else
               "heal_hold" if self.state == "HEAL" else
               "laser_charging_aim" if (w1 and w1["kind"] == "laser" and (w1.get("charge") or 0) > 0) else
               "aim_or_los")
        C["gun_idle_" + why] += 1
    if w1 and w1["kind"] == "laser":
        C["has_laser"] += 1
        ch = w1.get("charge") or 0
        if a["fire"][1]:
            C["laser_charging"] += 1
        elif ch > 0:
            C["laser_release"] += 1
            if ch < w1["stats"]["charge"] * 0.95:
                C["laser_release_partial"] += 1
        elif w1.get("cooldown", 0) > 0:
            C["laser_cooldown"] += 1
        else:
            C["laser_ready_idle"] += 1
    return a


mod.Bot._act = w
rng = random.Random(args.seed)
res = []
for i in range(args.trials):
    cur.clear()
    r = G.run_trial(mod, g, rng, secondaries=("laser",))
    res.append(r)
n = args.trials
eng_s = C["eng"] * 0.1
dealt = sum(v for k, v in F.items() if k.startswith("dealt_mech"))
taken = sum(v for k, v in F.items() if k.startswith("taken"))
print(f"bot {os.path.basename(args.bot)}  trials {n}  mean score {sum(r['score'] for r in res)/n:.2f}  survival {sum(r['t'] for r in res)/n:.0f}s")
print(f"engaged (hunter visible <184 m): {eng_s/n:.0f}s per match = {100*C['eng']/max(C['ticks'],1):.0f}% of alive time; state split:",
      {k[10:]: f"{100*v/C['eng']:.0f}%" for k, v in C.items() if k.startswith("eng_state_")})
print(f"delivered to mechs: {dealt/max(eng_s,1):.0f} dmg/s while engaged (theory gun+laser ~233; gun-only ~102)   "
      f"[gun {F['dealt_mech_gun']/max(eng_s,1):.0f}, laser {F['dealt_mech_laser']/max(eng_s,1):.0f}]")
print(f"taken: {taken/max(eng_s,1):.0f} dmg/s while engaged; breakdown", {k[6:]: int(v / n) for k, v in F.items() if k.startswith("taken")})
print(f"gun firing {100*C['gun_fire']/C['eng']:.0f}% of engaged ticks (max ~55% with reloads); idle reasons:",
      {k[9:]: f"{100*v/C['eng']:.0f}%" for k, v in sorted(C.items(), key=lambda kv: -kv[1]) if k.startswith("gun_idle_")})
if C["has_laser"]:
    print(f"laser (engaged, held {100*C['has_laser']/C['eng']:.0f}%): charging {100*C['laser_charging']/C['has_laser']:.0f}%  cooldown {100*C['laser_cooldown']/C['has_laser']:.0f}%  "
          f"READY-BUT-IDLE {100*C['laser_ready_idle']/C['has_laser']:.0f}%  releases {C['laser_release']/n:.1f}/match (partial {C['laser_release_partial']/n:.1f})")
if at4:
    print(f"at the 4th kill ({len(at4)}/{n} matches): t {sum(a_[0] for a_ in at4)/len(at4):.0f}s, cubes {sum(a_[1] for a_ in at4)/len(at4):.1f}, "
          f"upgrades {sum(len(a_[2]) for a_ in at4)/len(at4):.1f}: " + str(collections.Counter(u for a_ in at4 for u in a_[2]).most_common(6)))
