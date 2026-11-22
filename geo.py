"""Map geometry: line of sight, walkability probes, and budgeted A* on the precomputed grid (data/map.bin)."""
import array, heapq, json, math, struct, sys, zlib

INF = float("inf")


class Geo:
    def __init__(self, path):
        raw = open(path, "rb").read()
        hl = struct.unpack("<I", raw[:4])[0]
        m = json.loads(raw[4:4 + hl])
        body = zlib.decompress(raw[4 + hl:])
        self.x0, self.y0, self.cs = m["x0"], m["y0"], m["cs"]
        self.nx, self.ny = m["nx"], m["ny"]
        self.ncs, self.nnx, self.nny = m["ncs"], m["nnx"], m["nny"]
        n = self.nx * self.ny

        def arr(off):
            a = array.array("h")
            a.frombytes(body[off:off + 2 * n])
            if sys.byteorder != "little":
                a.byteswap()
            return a

        g, b, t = arr(0), arr(2 * n), arr(4 * n)
        self.G = [v * 0.1 for v in g]
        self.B = [1e9 if v == 32767 else v * 0.1 for v in b]
        self.T = [-1e9 if v == -32768 else v * 0.1 for v in t]
        self.NAV = body[6 * n:6 * n + self.nnx * self.nny]
        self.inv = 1.0 / self.cs
        self.ninv = 1.0 / self.ncs

    # ---------- fine grid ----------
    def cell(self, x, y):
        i = int((x - self.x0) * self.inv); j = int((y - self.y0) * self.inv)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return -1
        return j * self.nx + i

    def ground(self, x, y):
        c = self.cell(x, y)
        return self.G[c] if c >= 0 else 0.0

    def surface(self, x, y, z):
        """Highest standing surface at or below z+1.2 (ground or a roof)."""
        c = self.cell(x, y)
        if c < 0:
            return 0.0
        t = self.T[c]
        if t <= z + 1.2 and t > self.G[c]:
            return t
        return self.G[c]

    def los(self, ax, ay, az, bx, by, bz, step=1.5):
        """True if the segment clears buildings and terrain (trees ignored: they don't stop shots)."""
        dx, dy, dz = bx - ax, by - ay, bz - az
        d = math.sqrt(dx * dx + dy * dy)
        n = int(d / step) + 1
        if n < 2:
            return True
        sx, sy, sz = dx / n, dy / n, dz / n
        G, B, T = self.G, self.B, self.T
        x0, y0, inv, nx, ny = self.x0, self.y0, self.inv, self.nx, self.ny
        x, y, z = ax, ay, az
        for _ in range(n - 1):
            x += sx; y += sy; z += sz
            i = int((x - x0) * inv); j = int((y - y0) * inv)
            if i < 0 or j < 0 or i >= nx or j >= ny:
                continue
            c = j * nx + i
            if z < G[c] or B[c] <= z <= T[c]:
                return False
        return True

    def blocked_at(self, x, y, z, dist=3.0):
        """Would a mech with feet at height z be stopped at (x, y)?"""
        c = self.cell(x, y)
        if c < 0:
            return True
        if self.T[c] > z + 1.2 and self.B[c] < z + 9.0:
            return True
        rise = self.G[c] - z
        return rise > 1.2 and rise > 1.19 * dist

    def walk_clear(self, ax, ay, z, bx, by, step=2.0):
        dx, dy = bx - ax, by - ay
        d = math.hypot(dx, dy)
        n = int(d / step) + 1
        for k in range(1, n + 1):
            f = k / n
            x, y = ax + dx * f, ay + dy * f
            if self.blocked_at(x, y, z, step):
                return False
            z = self.surface(x, y, z)
        return True

    # ---------- nav grid ----------
    def ncell(self, x, y):
        i = int((x - self.x0) * self.ninv); j = int((y - self.y0) * self.ninv)
        i = min(max(i, 0), self.nnx - 1); j = min(max(j, 0), self.nny - 1)
        return j * self.nnx + i

    def ncenter(self, c):
        j, i = divmod(c, self.nnx)
        return (self.x0 + (i + 0.5) * self.ncs, self.y0 + (j + 0.5) * self.ncs)

    def nfree(self, c):
        return self.NAV[c] != 0

    def nearest_free(self, c, rmax=6):
        if self.NAV[c]:
            return c
        j0, i0 = divmod(c, self.nnx)
        for r in range(1, rmax + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < self.nnx and 0 <= j < self.nny and self.NAV[j * self.nnx + i]:
                        return j * self.nnx + i
        return -1


class AStar:
    """Weighted A* on the nav grid that runs a limited number of expansions per call (spread over ticks)."""
    NB = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
          (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]

    def __init__(self, geo, start_xy, goal_xy, w=2.0, cap=20000):
        self.geo = geo
        s = geo.nearest_free(geo.ncell(*start_xy))
        g = geo.nearest_free(geo.ncell(*goal_xy))
        self.goal_xy = goal_xy
        self.done = False
        self.path = None
        self.w, self.cap, self.exp = w, cap, 0
        if s < 0 or g < 0:
            self.done = True
            return
        self.s, self.g = s, g
        self.gj, self.gi = divmod(g, geo.nnx)
        self.cost = {s: 0.0}
        self.parent = {s: -1}
        self.open = [(self.h(s), s)]
        self.closed = set()
        self.best, self.besth = None, INF

    def h(self, c):
        j, i = divmod(c, self.geo.nnx)
        dx, dy = abs(i - self.gi), abs(j - self.gj)
        return self.w * (max(dx, dy) + 0.414 * min(dx, dy))

    def step(self, budget):
        if self.done:
            return True
        geo, nnx, nny, NAV = self.geo, self.geo.nnx, self.geo.nny, self.geo.NAV
        cost, parent, op, closed = self.cost, self.parent, self.open, self.closed
        push, pop = heapq.heappush, heapq.heappop
        gi, gj, w = self.gi, self.gj, self.w
        while op and budget > 0:
            _, c = pop(op)
            if c in closed:
                continue
            closed.add(c)
            budget -= 1
            self.exp += 1
            if c == self.g:
                self._finish(c)
                return True
            j, i = divmod(c, nnx)
            hc = max(abs(i - gi), abs(j - gj)) + 0.414 * min(abs(i - gi), abs(j - gj))
            if hc < self.besth:
                self.besth, self.best = hc, c
            gc = cost[c]
            for di, dj, d in self.NB:
                a, b = i + di, j + dj
                if a < 0 or b < 0 or a >= nnx or b >= nny:
                    continue
                n = b * nnx + a
                k = NAV[n]
                if not k or n in closed:
                    continue
                if di and dj and not (NAV[j * nnx + a] and NAV[b * nnx + i]):
                    continue  # no corner cutting
                ng = gc + d * k
                if ng < cost.get(n, INF):
                    cost[n] = ng
                    parent[n] = c
                    dx, dy = abs(a - gi), abs(b - gj)
                    push(op, (ng + w * (max(dx, dy) + 0.414 * min(dx, dy)), n))
        if not op or self.exp >= self.cap:
            # fall back to the closed node nearest the goal
            if self.best is not None:
                self._finish(self.best)
            self.done = True
            return True
        return False

    def _finish(self, c):
        out = []
        while c != -1:
            out.append(self.geo.ncenter(c))
            c = self.parent[c]
        out.reverse()
        self.path = out
        self.done = True
