"""
Tests de l'intégration Allemagne (gtfs.de grandes lignes).

    python -m unittest GERMANY/test_de.py -v      (depuis Backend/gtfs)
"""
import os
import sys
import unittest
import zipfile
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, GTFS_DIR)

import build_network as bn  # noqa: E402
import ingest_de_gtfs as de  # noqa: E402

REAL_ZIP = os.path.join(HERE, "de_gtfs.zip")
_MATCHER = None


def matcher():
    global _MATCHER
    if _MATCHER is None:
        _MATCHER = de.StationMatcher(bn.StationRef(bn.STATIONS_CSV))
    return _MATCHER


class TestNames(unittest.TestCase):
    def test_score(self):
        self.assertGreaterEqual(de.name_score("Aalen, Hauptbahnhof", "Aalen"), 0.6)
        self.assertGreaterEqual(de.name_score("Krakow Glowny", "Kraków Główny"), 0.9)
        self.assertGreaterEqual(de.name_score("München Hbf", "München Hbf"), 1.0)
        self.assertLess(de.name_score("Chemnitz, Hauptbahnhof", "Chemnitz Schillerplatz"), 0.6)

    def test_secondary_stops_penalised(self):
        self.assertGreater(de.secondary_penalty("Kiel Hbf", "Kiel Hbf ZOB"), 0)
        self.assertGreater(de.secondary_penalty("Hamburg-Altona", "Hamburg-Altona (S)"), 0)
        self.assertEqual(de.secondary_penalty("Hamburg-Altona", "Hamburg-Altona"), 0)


class TestStationMatch(unittest.TestCase):
    def match(self, name, lat, lon):
        m = matcher().match(name, lat, lon)
        return matcher().ref.rows.get(m["trainline_id"], {}).get("name"), m["status"]

    def test_main_stations(self):
        self.assertEqual(self.match("S+U Berlin Hauptbahnhof", 52.524998, 13.369061)[0], "Berlin Hbf")
        self.assertEqual(self.match("Frankfurt (Main) Hauptbahnhof", 50.106817, 8.663003)[0], "Frankfurt (Main) Hbf")
        self.assertEqual(self.match("München Hbf", 48.140232, 11.558335)[0], "München Hbf")
        self.assertEqual(self.match("Paris Est", 48.876742, 2.359264)[0], "Paris Gare de l’Est")

    def test_rail_station_not_bus_stop(self):
        # "Bahnhof Kehl" (arrêt de bus voisin) ne doit pas être choisi
        self.assertEqual(self.match("Kehl Bahnhof", 48.574467, 7.80954)[0], "Kehl")

    def test_main_station_before_s_bahn(self):
        self.assertEqual(self.match("Hamburg, Hamburg-Altona", 53.551922, 9.934876)[0], "Hamburg-Altona")

    def test_manual_override(self):
        self.assertEqual(self.match("Ostbahnhof", 48.12815, 11.604545), ("München Ost", "manuel"))

    def test_unknown(self):
        self.assertEqual(self.match("Nowhere", 0.0, 0.0), (None, "non trouvée"))


def frame(rows):
    return pd.DataFrame(rows[1:], columns=rows[0])


class TestBorderMerge(unittest.TestCase):
    def run_merge(self, b_agency="2", b_first_arr="10:01:00", b_stops=("X", "Y")):
        routes = frame([["route_id", "agency_id", "route_short_name", "route_type"],
                        ["r1", "1", "IC", "2"], ["r2", b_agency, "EC", "2"]])
        trips = frame([["route_id", "service_id", "trip_id"], ["r1", "s", "A"], ["r2", "s", "B"]])
        stops = frame([["stop_id", "stop_name", "parent_station"], ["W", "W", ""], ["X", "X", ""], ["Y", "Y", ""]])
        st = frame([["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"],
                    ["A", "09:00:00", "09:00:00", "W", "0", "", "1"],
                    ["A", "10:00:00", "10:00:00", "X", "1", "1", ""],
                    ["B", b_first_arr, "10:05:00", b_stops[0], "0", "", "1"],
                    ["B", "11:00:00", "11:00:00", b_stops[1], "1", "1", ""]])
        days = {"s": {date(2026, 10, 5), date(2026, 10, 6)}}
        return de.merge_border_splits(trips, routes, st, stops, days)

    def test_merged(self):
        trips, st, services, n = self.run_merge()
        self.assertEqual(n, 1)
        merged = st[st["trip_id"] == "A+B"].sort_values("stop_sequence")
        self.assertEqual(merged["stop_id"].tolist(), ["W", "X", "Y"])
        junction = merged[merged["stop_id"] == "X"].iloc[0]
        self.assertEqual((junction["arrival_time"], junction["departure_time"]), ("10:00:00", "10:05:00"))
        self.assertEqual(junction["drop_off_type"], "")  # on peut descendre à la jonction
        self.assertEqual(services["merged:A+B"], {date(2026, 10, 5), date(2026, 10, 6)})
        # les morceaux seuls ne circulent plus ces jours-là
        rest = trips.set_index("trip_id")["service_id"]
        self.assertEqual(services[rest["A"]], set())

    def test_same_operator_not_merged(self):
        self.assertEqual(self.run_merge(b_agency="1")[3], 0)

    def test_gap_too_large(self):
        self.assertEqual(self.run_merge(b_first_arr="10:30:00")[3], 0)

    def test_turnaround_not_merged(self):
        self.assertEqual(self.run_merge(b_stops=("X", "W"))[3], 0)


class TestTripLabels(unittest.TestCase):
    def label(self, agency, short_name):
        b = bn.NetworkBuilder.__new__(bn.NetworkBuilder)
        b.agency_names = {"a": agency}
        return b.trip_labels("DB", {"trip_id": "x"}, {"route_short_name": short_name, "agency_id": "a"}, "1")

    def test_types(self):
        self.assertEqual(self.label("DB Fernverkehr AG", "ICE 82"), ("", "DB ICE", 0))
        self.assertEqual(self.label("DB Fernverkehr AG", "IC 55"), ("", "DB Intercity", 0))
        self.assertEqual(self.label("PKP Intercity", "EC"), ("", "PKP EuroCity", 0))
        self.assertEqual(self.label("ÖBB", "RJ"), ("", "ÖBB Railjet", 0))
        self.assertEqual(self.label("SNCF", "ICE 82"), ("", "DB SNCF en coopération", 0))
        self.assertEqual(self.label("Inconnu", "FLX 10")[1], "DB Flx")


class TestCovers(unittest.TestCase):
    def test_tolerance_and_missing_stop(self):
        long = ([1, 2, 3, 4], [0, 10, 20, 30], [0, 11, 21, 30])
        self.assertTrue(bn.covers(*long, [1, 3, 4], [0, 20, 30], [1, 21, 30], tol=2))
        self.assertFalse(bn.covers(*long, [1, 3, 4], [0, 20, 30], [5, 21, 30], tol=2))
        # arrêt intermédiaire absent de l'autre flux (Darmstadt) : toléré si missing=1
        self.assertFalse(bn.covers(*long, [1, 9, 3, 4], [0, 5, 20, 30], [0, 5, 21, 30], tol=0))
        self.assertTrue(bn.covers(*long, [1, 9, 3, 4], [0, 5, 20, 30], [0, 5, 21, 30], tol=0, missing=1))


class FakeOsm:
    """OsmLookup sans réseau : renvoie des réponses préparées."""
    def __init__(self, answers):
        self.answers = answers

    def find(self, name, lat, lon):
        return self.answers.get(name)


def osm_answer(osm_id, name, uic, lat, lon):
    return {"osm_id": str(osm_id), "osm_name": name, "uic_ref": uic, "lat": str(lat), "lon": str(lon),
            "km": "0.1", "railway": "station"}


class TestOsm(unittest.TestCase):
    def test_variants(self):
        import osm_lookup as o
        v = [x for x, _ in o.name_variants("Mikulasovice dol. nadr.")]
        self.assertIn("Mikulasovice dolní nádraží", v)
        self.assertEqual(o.name_variants("Peine, Bahnhof")[1], ("Peine", o.MAX_KM))
        self.assertIn(("Alter Schlachthof", o.LOOSE_MAX_KM), o.name_variants("KA Tullastraße/Alter Schlachthof"))

    def test_uic_finds_stations_csv(self):
        # nom inconnu de stations.csv, mais OSM donne l'EVA de Peine (8004760, colonne db_id)
        m = de.StationMatcher(matcher().ref, FakeOsm({"Zzz Gare": osm_answer(1, "Peine", "8004760", 52.3189, 10.2322)}))
        r = m.match("Zzz Gare", 40.0, 0.0)
        self.assertEqual((m.ref.rows[r["trainline_id"]]["name"], r["status"]), ("Peine", "OSM (UIC)"))

    def test_missing_station_added(self):
        ref = bn.StationRef(bn.STATIONS_CSV)
        m = de.StationMatcher(ref, FakeOsm({"Gdansk Testowa": osm_answer(42, "Gdańsk Testowa", "5199999", 54.41, 18.57)}))
        r = m.match("Gdansk Testowa", 54.41, 18.57)
        self.assertEqual((r["trainline_id"], r["status"]), ("OSM42", "ajoutée (OSM)"))
        row = m.extra["OSM42"]
        self.assertEqual((row["name"], row["uic"], row["country"], row["time_zone"]),
                         ("Gdańsk Testowa", "5199999", "PL", "Europe/Warsaw"))
        self.assertEqual(ref.canonical("OSM42"), "OSM42")


@unittest.skipUnless(os.path.exists(REAL_ZIP), "GERMANY/de_gtfs.zip absent (lancer ingest_de_gtfs.py)")
class TestRealFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with zipfile.ZipFile(REAL_ZIP) as z:
            cls.stops = pd.read_csv(z.open("stops.txt"), dtype=str, keep_default_na=False)
            cls.routes = pd.read_csv(z.open("routes.txt"), dtype=str, keep_default_na=False)
            cls.st = pd.read_csv(z.open("stop_times.txt"), dtype=str, keep_default_na=False, usecols=["stop_id"])

    def test_trains_only(self):
        self.assertTrue((self.routes["route_type"] == "2").all())

    def test_stations_matched(self):
        used = self.stops[self.stops["stop_id"].isin(set(self.st["stop_id"]))]
        ratio = (used["stop_code"] != "").mean()
        self.assertGreater(ratio, 0.85, f"{ratio:.1%} des arrêts rattachés à stations.csv")
        rows = matcher().ref.rows
        self.assertTrue(used.loc[used["stop_code"] != "", "stop_code"].isin(rows.keys()).all())

    def test_main_german_stations(self):
        rows = matcher().ref.rows
        names = {rows[c]["name"] for c in set(self.stops["stop_code"]) if c}
        for n in ("Berlin Hbf", "München Hbf", "Hamburg Hbf", "Köln Hbf", "Frankfurt (Main) Hbf", "Stuttgart Hbf"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main()
