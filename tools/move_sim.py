"""Crude kinematic sanity sim: one mech, no enemies, walls from the grid. Checks the bot reaches the zone
and doesn't get pinned on buildings.  python3 tools/move_sim.py"""
import gzip, json, math, os, random, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bot as B  # noqa: E402

lines = gzip.open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt").read().splitlines()
hello = json.loads(lines[0])
base = json.loads(lines[1])
rng = random.Random(7)
fails = 0
results = []
for trial in range(12):
    b = B.Bot(hello)
    g = b.geo
    while True:
        x, y = rng.uniform(-420, 460), rng.uniform(-260, 320)
        if g.NAV[g.ncell(x, y)] == 1:
            break
    z = g.ground(x, y)
    zc = [rng.uniform(-150, 150), rng.uniform(-120, 120)]
    zone = {"center": [41.3, -72.8], "radius": 560, "next_center": zc, "next_radius": 140,
            "shrink_start": 999, "shrink_end": 1050, "dps": 20}
    vx = vy = vz = 0.0
    energy = 100.0
    grounded = True
    stuck_ticks = 0
    for k in range(900):  # 90 s
        obs = json.loads(json.dumps(base))
        obs.update(tick=k * 2, t=k * 0.1, mechs=[], projectiles=[], events=[], sounds=[], pickups=[], cubes=[],
                   crates=[], drops=[], zone=zone, offer=None)
        obs["self"].update(pos=[x, y, z], vel=[vx, vy, 0], hp=1400, max_hp=1400, energy=energy, in_zone=True,
                           grounded=grounded)
        a = b._act(obs)
        mx, my = a["move"]
        sp = 19.5 if (a["boost"] and grounded and energy > 0) else 11.7
        vx, vy = mx * sp, my * sp
        spent = False
        for _sub in range(2):
            dt = 0.05
            if a["jump"] and grounded and _sub == 0:
                vz, grounded = 11.0, False
            if not grounded:
                vz -= 22 * dt
                if a["boost"] and energy > 0:
                    vz = min(10.0, vz + 34 * dt); energy -= 15 * dt; spent = True
                z += vz * dt
                surf = g.surface(x, y, z)
                if z <= surf:
                    z, vz, grounded = surf, 0.0, True
            elif a["boost"] and math.hypot(mx, my) > 0.1:
                energy -= 17 * dt; spent = True
        if not spent:
            energy = min(100.0, energy + 18 * 0.1)
        nx_, ny_ = x + vx * 0.1, y + vy * 0.1
        if g.blocked_at(nx_, ny_, z, 1.0):
            if not g.blocked_at(nx_, y, z, 1.0):
                ny_ = y
            elif not g.blocked_at(x, ny_, z, 1.0):
                nx_ = x
            else:
                nx_, ny_ = x, y
                stuck_ticks += 1
        x, y = nx_, ny_
        if grounded:
            s_ = g.surface(x, y, z)
            if s_ < z - 1.0:
                grounded = False  # walked off a ledge
            else:
                z = s_
    d = math.hypot(x - zc[0], y - zc[1])
    ok = d < 0.55 * 140 + 10
    fails += not ok
    results.append(f"trial {trial}: end dist to zone center {d:6.1f} m  {'OK' if ok else 'FAIL'}  wall-pinned ticks {stuck_ticks}")
print("\n".join(results))
print(f"{12 - fails}/12 reached the zone")
