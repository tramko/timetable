#!/usr/bin/env python3
"""Spaja li se ZET-ov GTFS-RT feed s bazom: isti test kao `TimetableMatch` u aplikaciji (samo standardna biblioteka).

Upotreba: python3 -I tools/rt_match.py BAZA.sqlite [--feed URL|PUT]
"""
import argparse, sqlite3, sys, urllib.request

REALTIME_URL = "https://www.zet.hr/gtfs-rt-protobuf"
# isto kao TimetableMatch.minTrips / minKnownShare u aplikaciji
MIN_TRIPS = 20
MIN_KNOWN_SHARE = 0.5


def _varint(buf, i):
    shift = value = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, i
        shift += 7


def _fields(buf):
    """(broj polja, wire type, vrijednost) za jednu protobuf poruku; length-delimited vrijednosti su bytes."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            n, i = _varint(buf, i)
            value, i = buf[i:i + n], i + n
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"nepodržan wire type {wire}")
        yield field, wire, value


def vehicle_trip_ids(data):
    """trip_id-jevi vozila (FeedMessage.entity.vehicle.trip.trip_id)."""
    ids = set()
    for f, w, entity in _fields(data):
        if f != 2 or w != 2:
            continue
        for ef, ew, vehicle in _fields(entity):
            if ef != 4 or ew != 2:
                continue
            for vf, vw, trip in _fields(vehicle):
                if vf != 1 or vw != 2:
                    continue
                for tf, tw, value in _fields(trip):
                    if tf == 1 and tw == 2:
                        ids.add(value.decode("utf-8"))
    return ids


def fetch_feed(src=REALTIME_URL):
    if not src.startswith("http"):
        with open(src, "rb") as f:
            return f.read()
    req = urllib.request.Request(src, headers={"User-Agent": "Tramko timetable job"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def match(db_path, trip_ids):
    """(broj vožnji u feedu, koliko ih je u bazi)."""
    if not trip_ids:
        return 0, 0
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        ids = list(trip_ids)
        known = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            known += db.execute(f"SELECT COUNT(*) FROM trips WHERE trip_id IN ({','.join('?' * len(chunk))})",
                                chunk).fetchone()[0]
        return len(ids), known
    finally:
        db.close()


def is_outdated(total, known):
    return total >= MIN_TRIPS and known < total * MIN_KNOWN_SHARE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--feed", default=REALTIME_URL)
    a = ap.parse_args()
    total, known = match(a.db, vehicle_trip_ids(fetch_feed(a.feed)))
    print(f"vožnje vozila u feedu: {total}, u bazi: {known}")
    sys.exit(1 if is_outdated(total, known) else 0)


if __name__ == "__main__":
    main()
