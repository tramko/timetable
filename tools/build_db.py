#!/usr/bin/env python3
"""Gradi kompaktnu ZET.sqlite iz ZET GTFS statičkog feeda (samo standardna biblioteka).

Upotreba: python3 -I tools/build_db.py [--zip PATH] [--out ZetLive/Resources/ZET.sqlite]

Izvor podataka: ZET d.o.o., Otvorena dozvola RH.
"""
import argparse, csv, io, json, math, os, re, sqlite3, struct, sys, urllib.request, zipfile

GTFS_URL = "https://www.zet.hr/gtfs-scheduled/latest"
# Verzija sheme baze. Povećati pri svakoj promjeni sheme koju stara aplikacija ne zna čitati: aplikacija preuzima samo
# baze sheme koju podržava (`TimetableUpdater.supportedSchema`), pa je ovo i prag za osvježavanje bez izdanja.
SCHEMA_VERSION = 1
DP_TOLERANCE_M = 5.0
# Udaljenost stajališta od osi trase (procjena): tramvajski peron uz kolosijek, autobusno stajalište uz rub ceste.
SIDE_OFFSET_TRAM_M = 5.0
SIDE_OFFSET_BUS_M = 7.0

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE stops(id TEXT PRIMARY KEY, code TEXT, name TEXT NOT NULL, lat REAL NOT NULL,
                   lon REAL NOT NULL, parent TEXT, bearing REAL,
                   -- položaj za prikaz na karti: pomaknut s osi trase na stranu ukrcaja (procjena)
                   map_lat REAL, map_lon REAL) WITHOUT ROWID;
CREATE TABLE routes(id TEXT PRIMARY KEY, short_name TEXT NOT NULL, long_name TEXT,
                    type INTEGER NOT NULL) WITHOUT ROWID;
-- trasa: blob od int32 parova (lat*1e6, lon*1e6), little endian
CREATE TABLE shapes(id TEXT PRIMARY KEY, points BLOB NOT NULL) WITHOUT ROWID;
-- pattern = jedinstveni niz (stop_id, offset dolaska, offset polaska) od početka vožnje
CREATE TABLE pattern_stops(pattern_id INTEGER NOT NULL, seq INTEGER NOT NULL, stop_id TEXT NOT NULL,
                           arr_off INTEGER NOT NULL, dep_off INTEGER NOT NULL,
                           PRIMARY KEY(pattern_id, seq)) WITHOUT ROWID;
CREATE TABLE trips(trip_id TEXT PRIMARY KEY, route_id TEXT NOT NULL, service_id TEXT NOT NULL,
                   headsign TEXT, direction INTEGER, shape_id TEXT, pattern_id INTEGER NOT NULL,
                   start_seconds INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE service_dates(service_id TEXT NOT NULL, date INTEGER NOT NULL,
                           PRIMARY KEY(service_id, date)) WITHOUT ROWID;
"""
INDEXES = """
CREATE INDEX idx_ps_stop ON pattern_stops(stop_id, pattern_id);
CREATE INDEX idx_trips_pattern ON trips(pattern_id, start_seconds);
CREATE INDEX idx_trips_route ON trips(route_id);
CREATE INDEX idx_stops_parent ON stops(parent);
CREATE INDEX idx_sd_date ON service_dates(date, service_id);
"""


def hms(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def read_csv(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def douglas_peucker(pts, tol):
    """pts: lista (x, y) u metrima. Iterativno, vraća indekse koje treba zadržati."""
    n = len(pts)
    if n < 3:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if seg2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack.append((a, bi)); stack.append((bi, b))
    return [i for i, k in enumerate(keep) if k]


def _zigzags(ll):
    """Broj naglih vraćanja (dva duga segmenta koji završe blizu početka): znak ispremiješanih točaka."""
    n = 0
    for i in range(2, len(ll)):
        d1, d2, d3 = _dist(ll[i - 2], ll[i - 1]), _dist(ll[i - 1], ll[i]), _dist(ll[i - 2], ll[i])
        if d1 > 60 and d2 > 60 and d3 < 0.3 * min(d1, d2):
            n += 1
    return n


def untangle_shape(pts):
    """Neke ZET-ove trase u shapes.txt sadrže DVA niza točaka iste trase s istim shape_pt_sequence brojevima
    (različite gustoće), pa crta po redu skače između njih. Razdvajamo ih po blizini i zadržavamo čišći niz.
    pts: lista (seq, lat, lon) redom iz datoteke. Vraća novu listu ili None ako trasa nema duplikata."""
    groups, order = {}, []
    for seq, la, lo in pts:
        if seq not in groups:
            groups[seq] = []
            order.append(seq)
        groups[seq].append((la, lo))
    if all(len(g) == 1 for g in groups.values()):
        return None
    a, b = [], []
    for seq in sorted(order):
        g = groups[seq]
        if not a:
            a.append(g[0]); b.append(g[-1])
            g = g[1:-1] if len(g) > 2 else []
        if len(g) == 2:
            p, q = g
            if _dist(a[-1], p) + _dist(b[-1], q) <= _dist(a[-1], q) + _dist(b[-1], p):
                a.append(p); b.append(q)
            else:
                a.append(q); b.append(p)
        else:
            for p in g:
                if _dist(a[-1], b[-1]) < 1:      # nizovi se trenutno poklapaju: točka pripada obama
                    a.append(p); b.append(p)
                elif _dist(a[-1], p) <= _dist(b[-1], p):
                    a.append(p)
                else:
                    b.append(p)
    best = min((a, b), key=lambda c: (_zigzags(c), -len(c)))
    return [(i, la, lo) for i, (la, lo) in enumerate(best)]


def simplify_shape(latlon):
    lat0 = math.radians(latlon[0][0])
    kx, ky = 111320.0 * math.cos(lat0), 110540.0
    pts = [(lon * kx, lat * ky) for lat, lon in latlon]
    return [latlon[i] for i in douglas_peucker(pts, DP_TOLERANCE_M)]


def _dist(a, b):
    return math.hypot((b[0] - a[0]) * 110540.0, (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])))


def _bearing(a, b):
    p1, p2, dl = math.radians(a[0]), math.radians(b[0]), math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def stop_bearings(patterns, trip_rows, shapes, stop_pos, ahead=40.0):
    """Smjer u kojem vozila napuštaju svako stajalište: duž trase ~40 m iza stajališta (točnije od ravne crte do
    sljedećeg stajališta), prosjek svih vožnji kroz to stajalište. Bez trase: prema sljedećem stajalištu."""
    counts = {}
    for row in trip_rows:
        counts[(row[6], row[5])] = counts.get((row[6], row[5]), 0) + 1
    by_id = {pid: key for key, pid in patterns.items()}
    acc = {}
    for (pid, sid), n in counts.items():
        key = by_id[pid]
        pts = [(la, lo) for _, la, lo in shapes.get(sid, [])]
        idx = 0
        for i, (stop, _, _) in enumerate(key):
            here, b = stop_pos[stop], None
            if pts and idx < len(pts):
                j = min(range(idx, len(pts)), key=lambda k: _dist(here, pts[k]))
                if _dist(here, pts[j]) < 80:
                    idx = j
                    k = j
                    while k + 1 < len(pts) and _dist(pts[j], pts[k]) < ahead:
                        k += 1
                    if _dist(pts[j], pts[k]) >= 5:
                        b = _bearing(pts[j], pts[k])
                    else:  # kraj trase: smjer dolaska
                        k = j
                        while k > 0 and _dist(pts[k], pts[j]) < ahead:
                            k -= 1
                        if _dist(pts[k], pts[j]) >= 5:
                            b = _bearing(pts[k], pts[j])
            if b is None:
                if i + 1 < len(key):
                    b = _bearing(here, stop_pos[key[i + 1][0]])
                elif i > 0:
                    b = _bearing(stop_pos[key[i - 1][0]], here)
            if b is not None:
                v = acc.setdefault(stop, [0.0, 0.0, 0])
                v[0] += n * math.sin(math.radians(b)); v[1] += n * math.cos(math.radians(b)); v[2] += n
    out = {}
    for stop, (sx, cx, n) in acc.items():
        if math.hypot(sx, cx) / n >= 0.5:  # vožnje se slažu oko smjera; inače ga ne tvrdimo
            out[stop] = (math.degrees(math.atan2(sx, cx)) + 360) % 360
    return out


def _tokens(s):
    return [t for t in re.split(r"[.,\s\-]+", s.lower()) if t]


def is_abbreviation_of(short, full):
    """'Gl.kolodvor' je kratica od 'Glavni kolodvor': isti broj riječi, svaka je početak odgovarajuće, barem jedna kraća."""
    a, b = _tokens(short), _tokens(full)
    return len(a) == len(b) and all(y.startswith(x) for x, y in zip(a, b)) and any(len(x) < len(y) for x, y in zip(a, b))


def expand_headsigns(trip_rows, last_stop_name, stop_names):
    """ZET-ova odredišta su često kratice. Proširujemo ih samo kad jednoznačno odgovaraju punom imenu stajališta:
    prvo zadnjem stajalištu vožnje, inače jedinom stajalištu u mreži kojem kratica odgovara. Ništa se ne pogađa."""
    cache, expanded = {}, {}
    out = []
    for row in trip_rows:
        head, pattern = row[3], row[6]
        key = (head, last_stop_name.get(pattern))
        if key not in cache:
            full = head
            if "." in head or "-" in head:
                last = last_stop_name.get(pattern, "")
                if is_abbreviation_of(head, last):
                    full = last
                else:
                    cands = {n for n in stop_names if is_abbreviation_of(head, n)}
                    if len(cands) == 1:
                        full = cands.pop()
            full = " ".join(full.split())
            cache[key] = full
            if full != head:
                expanded[head] = full
        out.append(row[:3] + (cache[key],) + row[4:])
    return out, expanded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="lokalni GTFS ZIP (inače se skida)")
    ap.add_argument("--out", default="ZetLive/Resources/ZET.sqlite")
    a = ap.parse_args()

    if a.zip:
        zf = zipfile.ZipFile(a.zip)
    else:
        print("Skidam", GTFS_URL, file=sys.stderr)
        zf = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(GTFS_URL, timeout=120).read()))

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if os.path.exists(a.out):
        os.remove(a.out)
    db = sqlite3.connect(a.out)
    db.executescript(SCHEMA)
    stats = {}

    feed = next(read_csv(zf, "feed_info.txt"), {})
    db.executemany("INSERT INTO meta VALUES(?,?)", [
        ("feed_version", feed.get("feed_version", "")),
        ("feed_start", feed.get("feed_start_date", "")),
        ("feed_end", feed.get("feed_end_date", "")),
        ("attribution", "Izvor podataka: ZET d.o.o., Otvorena dozvola"),
        ("schema", str(SCHEMA_VERSION)),
    ])

    # Ručna tablica punih naziva (tools/abbreviations.json): ZET objavljuje samo kratice.
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "abbreviations.json"), encoding="utf-8") as f:
        abbr = json.load(f)
    used_abbr = set()

    def full_stop_name(name):
        if name in abbr["stops"]:
            used_abbr.add(name)
            return abbr["stops"][name]
        return name

    rows = [(r["stop_id"], r["stop_code"], full_stop_name(r["stop_name"]), float(r["stop_lat"]), float(r["stop_lon"]),
             r["parent_station"] or None) for r in read_csv(zf, "stops.txt")]
    stats["stop_names_expanded"] = len({k for k in used_abbr})
    db.executemany("INSERT INTO stops(id, code, name, lat, lon, parent) VALUES(?,?,?,?,?,?)", rows)
    stats["stops"] = len(rows)

    rows = [(r["route_id"], r["route_short_name"], r["route_long_name"], int(r["route_type"]))
            for r in read_csv(zf, "routes.txt")]
    db.executemany("INSERT INTO routes VALUES(?,?,?,?)", rows)
    stats["routes"] = len(rows)

    # calendar.txt je prazan (sve 0) -> samo calendar_dates (exception_type 1 = aktivno)
    rows = [(r["service_id"], int(r["date"])) for r in read_csv(zf, "calendar_dates.txt")
            if r["exception_type"] == "1"]
    db.executemany("INSERT OR IGNORE INTO service_dates VALUES(?,?)", rows)
    stats["service_dates"] = len(rows)

    # shapes
    shapes = {}
    for r in read_csv(zf, "shapes.txt"):
        shapes.setdefault(r["shape_id"], []).append(
            (int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))
    raw_pts = kept_pts = 0
    out = []
    untangled = 0
    for sid, pts in shapes.items():
        fixed = untangle_shape(pts)
        if fixed is not None:
            untangled += 1
            shapes[sid] = pts = fixed
        else:
            pts.sort(key=lambda p: p[0])
        ll = [(lat, lon) for _, lat, lon in pts]
        s = simplify_shape(ll)
        raw_pts += len(ll); kept_pts += len(s)
        out.append((sid, b"".join(struct.pack("<ii", round(la * 1e6), round(lo * 1e6)) for la, lo in s)))
    db.executemany("INSERT INTO shapes VALUES(?,?)", out)
    stats["shapes"] = len(out)
    stats["shape_points"] = f"{raw_pts} -> {kept_pts}"
    stats["shapes_untangled"] = untangled

    # stop_times -> patterns
    by_trip = {}
    for r in read_csv(zf, "stop_times.txt"):
        by_trip.setdefault(r["trip_id"], []).append(
            (int(r["stop_sequence"]), r["stop_id"], hms(r["arrival_time"]), hms(r["departure_time"])))
    patterns = {}
    trip_pattern = {}
    for tid, sts in by_trip.items():
        sts.sort()
        start = sts[0][3]
        key = tuple((s, ar - start, dp - start) for _, s, ar, dp in sts)
        pid = patterns.setdefault(key, len(patterns))
        trip_pattern[tid] = (pid, start)
    db.executemany("INSERT INTO pattern_stops VALUES(?,?,?,?,?)",
                   ((pid, i + 1, s, ar, dp) for key, pid in patterns.items()
                    for i, (s, ar, dp) in enumerate(key)))
    stats["patterns"] = len(patterns)
    stats["stop_times_rows"] = sum(len(v) for v in by_trip.values())

    rows, missing = [], 0
    for r in read_csv(zf, "trips.txt"):
        tp = trip_pattern.get(r["trip_id"])
        if not tp:
            missing += 1
            continue
        rows.append((r["trip_id"], r["route_id"], r["service_id"], r["trip_headsign"],
                     int(r["direction_id"] or 0), r["shape_id"] or None, tp[0], tp[1]))
    stop_name = {r[0]: r[2] for r in db.execute("SELECT id, code, name FROM stops")}
    last_stop_name = {pid: stop_name.get(key[-1][0], "") for key, pid in patterns.items()}
    # Odredišta: prvo ručna tablica (i za odredišta i za nazive stajališta), zatim jednoznačno podudaranje sa stajalištem.
    def from_table(head):
        for table in (abbr["headsigns"], abbr["stops"]):
            if head in table:
                used_abbr.add(head)
                return table[head]
        return head

    table_hits = {row[3]: from_table(row[3]) for row in rows}
    rows = [row[:3] + (table_hits[row[3]],) + row[4:] for row in rows]
    rows, expanded = expand_headsigns(rows, last_stop_name, set(stop_name.values()))
    expanded.update({k: v for k, v in table_hits.items() if k != v})
    stats["headsigns_expanded"] = len(expanded)
    db.executemany("INSERT INTO trips VALUES(?,?,?,?,?,?,?,?)", rows)

    # Nazivi linija su u ZET-ovim podacima gotovo svi skraćeni ("Zap.kol. - Borongaj"). Slažemo ih iz punih
    # odredišta oba smjera (najčešće odredište po smjeru); linije s jednim smjerom zadržavaju izvorni naziv.
    renamed = 0
    for (route_id,) in db.execute("SELECT id FROM routes").fetchall():
        ends = {}
        for direction, head, _ in db.execute(
                "SELECT direction, headsign, COUNT(*) c FROM trips WHERE route_id = ? GROUP BY 1, 2 ORDER BY c", (route_id,)):
            ends[direction] = head  # zadnji upis je najčešći
        if 0 in ends and 1 in ends and ends[0] != ends[1]:
            db.execute("UPDATE routes SET long_name = ? WHERE id = ?", (f"{ends[1]} – {ends[0]}", route_id))
            renamed += 1
    stats["route_names_rebuilt"] = renamed

    stop_pos = {r[0]: (r[1], r[2]) for r in db.execute("SELECT id, lat, lon FROM stops")}
    bearings = stop_bearings(patterns, rows, shapes, stop_pos)
    db.executemany("UPDATE stops SET bearing = ? WHERE id = ?", [(round(b, 1), sid) for sid, b in bearings.items()])
    # ZET-ove koordinate stajališta leže na osi trase (99 % unutar 2 m), pa ne govore s koje je strane ceste
    # stajalište. Ukrcaj je uvijek s desne strane vozila: oznaku pomičemo udesno od smjera vožnje.
    route_type = {r[0]: r[1] for r in db.execute("SELECT id, type FROM routes")}
    by_id = {pid: key for key, pid in patterns.items()}
    tram_stops = {st for row in rows if route_type.get(row[1]) == 0 for st, _, _ in by_id[row[6]]}
    shifted = []
    for sid, b in bearings.items():
        lat, lon = stop_pos[sid]
        offset = SIDE_OFFSET_TRAM_M if sid in tram_stops else SIDE_OFFSET_BUS_M
        right = math.radians(b + 90)
        shifted.append((lat + offset * math.cos(right) / 110540.0,
                        lon + offset * math.sin(right) / (111320.0 * math.cos(math.radians(lat))), sid))
    db.executemany("UPDATE stops SET map_lat = ?, map_lon = ? WHERE id = ?", shifted)
    served = {s for key in patterns for s, _, _ in key}
    stats["stop_bearings"] = f"{len(bearings)} od {len(served)} stajališta s vožnjama"
    stats["trips"] = len(rows)
    stats["trips_without_stop_times"] = missing

    db.executescript(INDEXES)
    db.commit()
    db.execute("VACUUM")
    db.close()

    size = os.path.getsize(a.out)
    print(f"\nZET.sqlite: {size / 1e6:.2f} MB ({'OK' if size < 15e6 else 'PREVELIKA, cilj < 15 MB'})")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    for short, full in sorted(expanded.items()):
        print(f"    {short} -> {full}")
    unused = sorted((set(abbr["stops"]) | set(abbr["headsigns"])) - used_abbr)
    if unused:
        print("  abbreviations.json, ključevi kojih više nema u feedu:", ", ".join(unused))


if __name__ == "__main__":
    main()
