#!/usr/bin/env python3
"""Osvježavanje voznog reda bez izdanja aplikacije: gradi novu ZET.sqlite kad ZET objavi novi GTFS i priprema
datoteke za GitHub Release (vidi .github/workflows/timetable.yml i "Zastarjeli vozni red" u CLAUDE.md).

Upotreba:
  python3 -I tools/publish_timetable.py --out OUT [--latest-manifest URL|PATH] [--zip PATH] [--base-url URL]
  python3 -I tools/publish_timetable.py --health --latest-manifest URL|PATH

Objava: ako ima nečeg novog (ZIP ili kod koji gradi bazu), u OUT zapisuje ZET-<oznaka>.sqlite i manifest.json te
ispisuje "changed=true" i "tag=<oznaka>" (i u $GITHUB_OUTPUT); inače "changed=false". Oznaka je verzija voznog reda
("000397"), a ponovna izgradnja iste verzije dobiva reviziju ("000397-r2"). Izlazni kod != 0 kad je nova baza
neispravna ili se ne spaja s feedom uživo.

Provjera (--health): spaja li se zadnji objavljeni vozni red s feedom uživo. Izlazni kod != 0 ako ne (ZET je
promijenio vozni red, a novi nije objavljen).
"""
import argparse, datetime, hashlib, json, os, sqlite3, subprocess, sys, tempfile, urllib.error, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
from build_db import GTFS_URL, SCHEMA_VERSION  # noqa: E402
import rt_match  # noqa: E402

# Donje granice za ispravnu bazu (000397: 154 linije, 3.802 stajališta, 98.915 vožnji, 12,7 MB).
MIN_ROUTES, MIN_STOPS, MIN_TRIPS = 100, 2500, 30000
MAX_SIZE = 25e6
# Datoteke koje određuju sadržaj baze: promjena bilo koje znači novu reviziju iste verzije voznog reda.
BUILDER_FILES = ("build_db.py", "abbreviations.json")
USER_AGENT = "Tramko timetable job"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def builder_sha256():
    h = hashlib.sha256()
    for name in BUILDER_FILES:
        with open(os.path.join(HERE, name), "rb") as f:
            h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        f.write(r.read())


def load_manifest(src):
    """Zadnji objavljeni manifest ili None (prvo pokretanje, 404)."""
    if not src:
        return None
    try:
        if os.path.exists(src):
            with open(src, encoding="utf-8") as f:
                return json.load(f)
        with urllib.request.urlopen(src, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def version_key(v):
    """ZET-ove verzije su brojevi s vodećim nulama ("000397"); usporedba kao broj, inače kao tekst."""
    return (0, int(v), "") if v.isdigit() else (1, 0, v)


def today_zagreb():
    return int(datetime.datetime.now(ZoneInfo("Europe/Zagreb")).strftime("%Y%m%d"))


def validate(db_path, today):
    """Vraća meta podatke baze ili baca ValueError s razlogom."""
    size = os.path.getsize(db_path)
    if size > MAX_SIZE:
        raise ValueError(f"baza je prevelika: {size / 1e6:.1f} MB")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("integrity_check nije prošao")
        meta = dict(db.execute("SELECT key, value FROM meta"))
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("routes", "stops", "trips")}
        last_date = db.execute("SELECT MAX(date) FROM service_dates").fetchone()[0] or 0
    finally:
        db.close()
    if not meta.get("feed_version"):
        raise ValueError("nema feed_version")
    if meta.get("schema") != str(SCHEMA_VERSION):
        raise ValueError(f"shema {meta.get('schema')} != {SCHEMA_VERSION}")
    for table, minimum in (("routes", MIN_ROUTES), ("stops", MIN_STOPS), ("trips", MIN_TRIPS)):
        if counts[table] < minimum:
            raise ValueError(f"premalo redova u {table}: {counts[table]} < {minimum}")
    if last_date < today:
        raise ValueError(f"vozni red ne pokriva danas: zadnji servisni dan {last_date}")
    return meta, counts, size


def realtime_match(db_path):
    """(vožnje u feedu, poznate u bazi) ili None ako feed uživo nije dostupan (ZET ga vodi kao testni)."""
    try:
        return rt_match.match(db_path, rt_match.vehicle_trip_ids(rt_match.fetch_feed()))
    except Exception as e:  # noqa: BLE001 — nedostupan feed ne smije zaustaviti objavu
        print("Feed uživo nije dostupan:", e, file=sys.stderr)
        return None


def output(**values):
    lines = [f"{k}={v}" for k, v in values.items()]
    print("\n".join(lines))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def fail(message):
    print("GREŠKA:", message, file=sys.stderr)
    sys.exit(1)


def health(previous):
    """Spaja li se zadnji objavljeni vozni red s feedom uživo."""
    if not previous:
        fail("nema objavljenog voznog reda")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "ZET.sqlite")
        download(previous["url"], db_path)
        if sha256(db_path) != previous["sha256"]:
            fail("objavljena baza ne odgovara manifestu (sha256)")
        result = realtime_match(db_path)
    if result is None:
        print("Feed uživo nije dostupan, provjera preskočena")
        return
    total, known = result
    print(f"Vozni red {previous['feed_version']} r{previous.get('revision', 1)}: vožnje vozila u feedu {total}, "
          f"u voznom redu {known}")
    if rt_match.is_outdated(total, known):
        fail("objavljeni vozni red se ne spaja s feedom uživo: ZET je vjerojatno objavio novi vozni red koji ovaj "
             f"posao nije preuzeo (provjeri {GTFS_URL})")


def publish(a, previous):
    builder = builder_sha256()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = a.zip
        if not zip_path:
            zip_path = os.path.join(tmp, "gtfs.zip")
            print("Skidam", GTFS_URL, file=sys.stderr)
            download(GTFS_URL, zip_path)
        source_sha = sha256(zip_path)
        if previous and previous.get("source_sha256") == source_sha and previous.get("builder_sha256") == builder:
            print("Ni ZIP ni kod za izgradnju se nisu promijenili od", previous.get("feed_version"), file=sys.stderr)
            return output(changed="false")

        db_path = os.path.join(tmp, "ZET.sqlite")
        subprocess.run([sys.executable, "-I", os.path.join(HERE, "build_db.py"), "--zip", zip_path, "--out", db_path],
                       check=True, stdout=sys.stderr)
        db = sqlite3.connect(db_path)
        version = dict(db.execute("SELECT key, value FROM meta")).get("feed_version", "")
        if previous and version_key(version) < version_key(previous.get("feed_version", "")):
            db.close()
            print(f"Verzija {version} je starija od objavljene {previous['feed_version']}", file=sys.stderr)
            return output(changed="false")
        revision = previous.get("revision", 1) + 1 if previous and previous.get("feed_version") == version else 1
        db.execute("INSERT OR REPLACE INTO meta VALUES('revision', ?)", (str(revision),))
        db.commit()
        db.close()

        today = today_zagreb()
        try:
            meta, counts, size = validate(db_path, today)
        except ValueError as e:
            fail(f"neispravna baza: {e}")
        # Vozni red koji već vrijedi mora se spajati s feedom uživo; onaj objavljen unaprijed još ne može.
        feed_start = int(meta["feed_start"]) if meta.get("feed_start", "").isdigit() else None
        if feed_start is None or feed_start <= today:
            result = realtime_match(db_path)
            if result:
                total, known = result
                print(f"Feed uživo: {total} vožnji vozila, u novom voznom redu {known}", file=sys.stderr)
                if rt_match.is_outdated(total, known):
                    fail(f"novi vozni red {version} se ne spaja s feedom uživo ({known} od {total} vožnji)")

        tag = version if revision == 1 else f"{version}-r{revision}"
        os.makedirs(a.out, exist_ok=True)
        name = f"ZET-{tag}.sqlite"
        os.replace(db_path, os.path.join(a.out, name))
        manifest = {
            "schema": SCHEMA_VERSION,
            "feed_version": version,
            "revision": revision,
            "feed_start": feed_start,
            "sha256": sha256(os.path.join(a.out, name)),
            "size": size,
            "url": (a.base_url.format(tag=tag).rstrip("/") + "/" if a.base_url else "") + name,
            "realtime_url": rt_match.REALTIME_URL,
            "source_sha256": source_sha,
            "builder_sha256": builder,
            "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": counts,
        }
        with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        output(changed="true", tag=tag)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="mapa za ZET-<oznaka>.sqlite i manifest.json")
    ap.add_argument("--latest-manifest", help="URL ili put do zadnjeg objavljenog manifest.json")
    ap.add_argument("--zip", help="lokalni GTFS ZIP (inače se skida s ZET-a)")
    ap.add_argument("--base-url", default="",
                    help="adresa pod kojom će baza biti objavljena, bez imena datoteke; {tag} se zamjenjuje oznakom")
    ap.add_argument("--health", action="store_true", help="samo provjeri zadnji objavljeni vozni red")
    a = ap.parse_args()
    previous = load_manifest(a.latest_manifest)
    if a.health:
        return health(previous)
    if not a.out:
        ap.error("--out je obavezan")
    publish(a, previous)


if __name__ == "__main__":
    main()
