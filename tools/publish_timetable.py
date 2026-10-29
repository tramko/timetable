#!/usr/bin/env python3
"""Osvježavanje voznog reda bez izdanja aplikacije: gradi novu ZET.sqlite kad ZET objavi novi GTFS i priprema
datoteke za GitHub Release (vidi .github/workflows/timetable.yml i "Zastarjeli vozni red" u CLAUDE.md).

Upotreba: python3 -I tools/publish_timetable.py --out OUT [--latest-manifest URL|PATH] [--zip PATH]

Ako ima nečeg novog, u OUT zapisuje ZET-<verzija>.sqlite i manifest.json te ispisuje "changed=true" i "tag=<verzija>"
(i u $GITHUB_OUTPUT). Inače "changed=false". Izlazni kod != 0 samo kad je nova baza neispravna.
"""
import argparse, datetime, hashlib, json, os, sqlite3, subprocess, sys, tempfile, urllib.error, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_db import GTFS_URL, SCHEMA_VERSION  # noqa: E402

# Donje granice za ispravnu bazu (000397: 154 linije, ~3.800 stajališta, ~66.700 vožnji, ~9,4 MB).
MIN_ROUTES, MIN_STOPS, MIN_TRIPS = 100, 2500, 30000
MAX_SIZE = 25e6


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def output(**values):
    lines = [f"{k}={v}" for k, v in values.items()]
    print("\n".join(lines))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="mapa za ZET-<verzija>.sqlite i manifest.json")
    ap.add_argument("--latest-manifest", help="URL ili put do zadnjeg objavljenog manifest.json")
    ap.add_argument("--zip", help="lokalni GTFS ZIP (inače se skida s ZET-a)")
    ap.add_argument("--base-url", default="",
                    help="adresa pod kojom će baza biti objavljena, bez imena datoteke; {version} se zamjenjuje verzijom")
    a = ap.parse_args()

    previous = load_manifest(a.latest_manifest)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = a.zip
        if not zip_path:
            zip_path = os.path.join(tmp, "gtfs.zip")
            print("Skidam", GTFS_URL, file=sys.stderr)
            req = urllib.request.Request(GTFS_URL, headers={"User-Agent": "ZetLive timetable job"})
            with urllib.request.urlopen(req, timeout=300) as r, open(zip_path, "wb") as f:
                f.write(r.read())
        source_sha = sha256(zip_path)
        if previous and previous.get("source_sha256") == source_sha:
            print("ZIP se nije promijenio od", previous.get("feed_version"), file=sys.stderr)
            return output(changed="false")

        db_path = os.path.join(tmp, "ZET.sqlite")
        subprocess.run([sys.executable, "-I", os.path.join(HERE, "build_db.py"), "--zip", zip_path, "--out", db_path],
                       check=True, stdout=sys.stderr)
        today = int(datetime.datetime.now(ZoneInfo("Europe/Zagreb")).strftime("%Y%m%d"))
        try:
            meta, counts, size = validate(db_path, today)
        except ValueError as e:
            print("NEISPRAVNA BAZA:", e, file=sys.stderr)
            sys.exit(1)
        version = meta["feed_version"]
        if previous and version_key(version) <= version_key(previous.get("feed_version", "")):
            print(f"ZIP je drugačiji, ali verzija {version} nije novija od {previous['feed_version']}", file=sys.stderr)
            return output(changed="false")

        os.makedirs(a.out, exist_ok=True)
        name = f"ZET-{version}.sqlite"
        os.replace(db_path, os.path.join(a.out, name))
        manifest = {
            "schema": SCHEMA_VERSION,
            "feed_version": version,
            "feed_start": int(meta["feed_start"]) if meta.get("feed_start", "").isdigit() else None,
            "sha256": sha256(os.path.join(a.out, name)),
            "size": size,
            "url": (a.base_url.format(version=version).rstrip("/") + "/" if a.base_url else "") + name,
            "source_sha256": source_sha,
            "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": counts,
        }
        with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        output(changed="true", tag=version)


if __name__ == "__main__":
    main()
