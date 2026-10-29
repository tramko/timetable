# Tramko – vozni red

Baza voznog reda za aplikaciju Tramko (ZetLive), izgrađena iz ZET-ova GTFS-a
(https://www.zet.hr/gtfs-scheduled/latest). Posao `.github/workflows/timetable.yml` svaki sat provjerava ima li
novog voznog reda i objavljuje ga kao Release (`ZET-<verzija>.sqlite` i `manifest.json`).

Aplikacija čita `releases/latest/download/manifest.json`.

Izvor podataka: ZET d.o.o., Otvorena dozvola (https://data.gov.hr/otvorena-dozvola).

Kod se ne uređuje ovdje: izvor je u repozitoriju aplikacije (`tools/`), a ovamo ga kopira `tools/timetable-job/sync.sh`.
