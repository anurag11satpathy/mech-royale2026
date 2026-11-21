"""Offline map precompute (run once, stdlib only): python3 tools/precompute.py

Writes data/map.bin = <u32 header length><JSON header><zlib body>. Body arrays, row-major, int16 in decimeters:
  G  ground height at each 2 m cell center
  B  lowest solid base in the cell   (32767 = no solid)
  T  highest solid top in the cell   (-32768 = no solid)
  NAV uint8 4 m grid: 0 blocked, 1 free, 3 next to a wall, 8 steep terrain (jet)
The map is fixed for every match, so this is shipped in the repo and loaded at hello.
"""
import array, json, os, struct, sys, zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CS = 2.0      # fine cell size (LOS / steering)
NCS = 4.0     # nav cell size (A*)
STEP_UP = 1.2
HEIGHT = 9.0


def pip(x, y, flat):
    """Point in polygon, flat [x0,y0,x1,y1,...]."""
    inside = False
    n = len(flat) // 2
    j = n - 1
    for i in range(n):
        xi, yi = flat[2 * i], flat[2 * i + 1]
        xj, yj = flat[2 * j], flat[2 * j + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def main():
    world = json.load(open(os.path.join(ROOT, "arena/v2/world.json")))
    ter = world["terrain"]
    tnx, tny, tx0, ty0 = ter["nx"], ter["ny"], ter["minX"], ter["minY"]
    traw = array.array("h")
    traw.frombytes(open(os.path.join(ROOT, "arena/v2", ter["file"]), "rb").read())
    if sys.byteorder != "little":
        traw.byteswap()

    x0, y0 = tx0, ty0
    nx = int((tnx - 1) * ter["step"] / CS)
    ny = int((tny - 1) * ter["step"] / CS)
    n = nx * ny
    G = [0.0] * n
    for j in range(ny):
        tj = min(tny - 1, int(round((y0 + (j + 0.5) * CS - ty0) / ter["step"])))
        for i in range(nx):
            ti = min(tnx - 1, int(round((x0 + (i + 0.5) * CS - tx0) / ter["step"])))
            G[j * nx + i] = traw[tj * tnx + ti] * ter["scale"]

    B = [1e9] * n
    T = [-1e9] * n
    WALL = bytearray(n)    # non-bridge solid that blocks walking at ground level
    BRIDGE = bytearray(n)
    for p in world["prisms"]:
        ring, holes = p["ring"], p.get("holes") or []
        xs, ys = ring[0::2], ring[1::2]
        i0 = max(0, int((min(xs) - x0) / CS)); i1 = min(nx - 1, int((max(xs) - x0) / CS))
        j0 = max(0, int((min(ys) - y0) / CS)); j1 = min(ny - 1, int((max(ys) - y0) / CS))
        for j in range(j0, j1 + 1):
            cy = y0 + (j + 0.5) * CS
            for i in range(i0, i1 + 1):
                cx = x0 + (i + 0.5) * CS
                if not pip(cx, cy, ring) or any(pip(cx, cy, h) for h in holes):
                    continue
                c = j * nx + i
                B[c] = min(B[c], p["base"]); T[c] = max(T[c], p["top"])
                if p["kind"] == "bridge":
                    BRIDGE[c] = 1
                elif p["top"] - G[c] > STEP_UP and p["base"] - G[c] < HEIGHT:
                    WALL[c] = 1

    # nav grid (4 m): blocked if any fine sub-cell is a wall
    k = int(NCS / CS)
    nnx, nny = nx // k, ny // k
    NAV = bytearray(nnx * nny)
    for J in range(nny):
        for I in range(nnx):
            blocked = False
            for dj in range(k):
                for di in range(k):
                    if WALL[(J * k + dj) * nx + I * k + di]:
                        blocked = True
            NAV[J * nnx + I] = 0 if blocked else 1
    # steep terrain inside a nav cell (river banks, ravines): passable only by jetting, so costly
    STEEP = bytearray(nnx * nny)
    for J in range(nny):
        for I in range(nnx):
            hs = [G[(J * k + dj) * nx + I * k + di] for dj in range(k) for di in range(k)]
            if max(hs) - min(hs) > 2.0:
                STEEP[J * nnx + I] = 1
    for J in range(nny):
        for I in range(nnx):
            c = J * nnx + I
            if not NAV[c]:
                continue
            for di, dj in ((1, 0), (0, 1)):
                a, b = I + di, J + dj
                if a < nnx and b < nny:
                    ga = sum(G[(J * k + q) * nx + I * k + r] for q in range(k) for r in range(k)) / (k * k)
                    gb = sum(G[(b * k + q) * nx + a * k + r] for q in range(k) for r in range(k)) / (k * k)
                    if abs(ga - gb) > 2.4:
                        STEEP[c] = 1; STEEP[b * nnx + a] = 1
    # cells next to walls cost more, so paths keep clear of the 2.5 m mech radius
    out = bytearray(NAV)
    for J in range(nny):
        for I in range(nnx):
            if not NAV[J * nnx + I]:
                continue
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    a, b = I + di, J + dj
                    if 0 <= a < nnx and 0 <= b < nny and not NAV[b * nnx + a]:
                        out[J * nnx + I] = 3
    for c in range(nnx * nny):
        if out[c] and STEEP[c]:
            out[c] = 8
    NAV = out

    def q(v, empty):
        return max(-32768, min(32767, int(round(v * 10)))) if abs(v) < 1e8 else empty

    ga = array.array("h", (q(v, 0) for v in G))
    ba = array.array("h", (q(v, 32767) for v in B))
    ta = array.array("h", (q(v, -32768) for v in T))
    for a in (ga, ba, ta):
        if sys.byteorder != "little":
            a.byteswap()
    body = zlib.compress(ga.tobytes() + ba.tobytes() + ta.tobytes() + bytes(NAV), 9)
    hdr = json.dumps({"x0": x0, "y0": y0, "cs": CS, "nx": nx, "ny": ny,
                      "ncs": NCS, "nnx": nnx, "nny": nny}).encode()
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    path = os.path.join(ROOT, "data/map.bin")
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(hdr)) + hdr + body)
    free = sum(1 for v in NAV if v); steep = sum(1 for v in NAV if v == 8)
    print(f"wrote {path}: {os.path.getsize(path)/1024:.0f} KiB, fine {nx}x{ny}, nav {nnx}x{nny} "
          f"({free}/{len(NAV)} free, {steep} steep), wall cells {sum(WALL)}, bridge cells {sum(BRIDGE)}")


if __name__ == "__main__":
    main()
