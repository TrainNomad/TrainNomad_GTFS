"""
Tests de l'intégration SNCB (Belgique).

    python -m unittest SNCB/test_sncb.py -v      (depuis Backend/gtfs)

- filtrage du GTFS (ingest_sncb_gtfs.filter_feed) sur un mini-GTFS synthétique
- correspondance des gares par UIC et libellés des trains (build_network)
- si SNCB/sncb_gtfs.zip existe : contrôles sur le vrai flux filtré
- si network.bin existe : présence des trains SNCB dans le réseau compilé
"""
import io
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
GTFS_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, GTFS_DIR)

import build_network as bn  # noqa: E402
import ingest_sncb_gtfs as ingest  # noqa: E402

REAL_ZIP = os.path.join(HERE, "sncb_gtfs.zip")
NETWORK_BIN = os.path.join(GTFS_DIR, "network.bin")


def csv(rows):
    return "\n".join(",".join(r) for r in rows) + "\n"


def make_feed(path):
    """Mini-GTFS au format SNCB : un IC, un L avec point de passage, un bus, un OTC,
    des TRN (résolu par numéro, Eurostar 9xxx, ICE Frankfurt, inconnu)."""
    files = {
        "agency.txt": csv([["agency_id", "agency_name", "agency_timezone", "agency_url"],
                           ["nmbssncb", "NMBS/SNCB", "Europe/Brussels", "http://www.belgiantrain.be/"]]),
        "feed_info.txt": csv([["feed_id", "feed_publisher_name"], ["nmbssncb", "nmbssncb"]]),
        "routes.txt": csv([["agency_id", "route_id", "route_long_name", "route_short_name", "route_type"],
                           ["nmbssncb", "gr:nmbssncb:1", "Bruxelles-Midi -- Anvers-Central", "IC", "2"],
                           ["nmbssncb", "gr:nmbssncb:2", "Bruxelles-Midi -- Anvers-Central", "L", "2"],
                           ["nmbssncb", "gr:nmbssncb:3", "Bruxelles-Midi -- Anvers-Central", "BUS", "3"],
                           ["nmbssncb", "gr:nmbssncb:4", "Paris Nord (FR) -- Bruxelles-Midi", "OTC", "2"],
                           ["nmbssncb", "gr:nmbssncb:5", "Bruxelles-Midi -- Anvers-Central", "TRN", "2"],
                           ["nmbssncb", "gr:nmbssncb:6", "Frankfurt Main (DE) -- Bruxelles-Midi", "TRN", "2"],
                           ["nmbssncb", "gr:nmbssncb:7", "Bruxelles-Midi -- Anvers-Central", "S1", "2"]]),
        "trips.txt": csv([["route_id", "service_id", "trip_id", "trip_short_name"],
                          ["gr:nmbssncb:1", "C1", "T_IC", "2838"],
                          ["gr:nmbssncb:2", "C1", "T_L", "1960"],
                          ["gr:nmbssncb:3", "C1", "T_BUS", "12850"],
                          ["gr:nmbssncb:4", "C1", "T_OTC", "56"],
                          ["gr:nmbssncb:5", "C1", "T_TRN_IC", "2838"],    # même numéro qu'un IC
                          ["gr:nmbssncb:5", "C1", "T_TRN_EST", "9310"],   # Eurostar
                          ["gr:nmbssncb:6", "C1", "T_TRN_ICE", "16"],     # ICE Frankfurt
                          ["gr:nmbssncb:5", "C1", "T_TRN_UNK", "14403"],  # inconnu
                          ["gr:nmbssncb:7", "C_OLD", "T_OLD", "1982"]]),  # calendrier expiré
        "calendar.txt": csv([["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                              "saturday", "sunday", "start_date", "end_date"],
                             ["C1", "1", "1", "1", "1", "1", "1", "1", "20260101", "20261212"],
                             ["C_OLD", "1", "1", "1", "1", "1", "1", "1", "20260101", "20260301"]]),
        "calendar_dates.txt": csv([["service_id", "date", "exception_type"],
                                   ["C1", "20260105", "2"],     # passé : supprimé
                                   ["C1", "20261010", "2"]]),   # futur : conservé
        "stops.txt": csv([["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"],
                          ["gs:nmbssncb:S8814001", "Bruxelles-Midi", "50.8357", "4.3361", "1", ""],
                          ["gs:nmbssncb:8814001_12", "Bruxelles-Midi", "50.8357", "4.3361", "0", "gs:nmbssncb:S8814001"],
                          ["gs:nmbssncb:S8821006", "Anvers-Central", "51.2172", "4.4211", "1", ""],
                          ["gs:nmbssncb:8821006_3", "Anvers-Central", "51.2172", "4.4211", "0", "gs:nmbssncb:S8821006"],
                          ["gs:nmbssncb:8814217", "Braine-Alliance", "50.66", "4.37", "0", ""],
                          ["gs:nmbssncb:8821030_1", "Anvers-Dam", "51.23", "4.42", "0", ""]]),
        "transfers.txt": csv([["from_stop_id", "to_stop_id", "transfer_type", "min_transfer_time"],
                              ["gs:nmbssncb:8814001_12", "gs:nmbssncb:8814001_12", "2", "300"]]),
    }
    st = [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"]]
    for tid in ("T_IC", "T_BUS", "T_OTC", "T_TRN_IC", "T_TRN_EST", "T_TRN_ICE", "T_TRN_UNK", "T_OLD"):
        st += [[tid, "08:00:00", "08:00:00", "gs:nmbssncb:8814001_12", "1", "0", "1"],
               [tid, "08:45:00", "08:45:00", "gs:nmbssncb:8821006_3", "2", "1", "0"]]
    # train L avec un point de simple passage (pickup = drop_off = 1) au milieu
    st += [["T_L", "09:00:00", "09:00:00", "gs:nmbssncb:8814001_12", "1", "0", "1"],
           ["T_L", "09:20:00", "09:20:00", "gs:nmbssncb:8814217", "2", "1", "1"],
           ["T_L", "09:50:00", "09:50:00", "gs:nmbssncb:8821006_3", "3", "1", "0"]]
    files["stop_times.txt"] = csv(st)
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)


def read_zip(path, name):
    with zipfile.ZipFile(path) as z:
        return pd.read_csv(z.open(name), dtype=str, keep_default_na=False)


class TestFilterFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        src = os.path.join(cls.tmp, "raw.zip")
        cls.out = os.path.join(cls.tmp, "sncb.zip")
        make_feed(src)
        cls.stats = ingest.filter_feed(src, cls.out, today=date(2026, 10, 5))
        cls.trips = read_zip(cls.out, "trips.txt")
        cls.routes = read_zip(cls.out, "routes.txt")
        cls.cat = dict(zip(cls.trips["trip_id"], cls.trips["route_id"].map(cls.routes.set_index("route_id")["route_short_name"])))

    def test_trains_only(self):
        self.assertNotIn("T_BUS", self.cat)
        self.assertTrue((self.routes["route_type"] == "2").all())

    def test_duplicates_from_other_feeds_removed(self):
        self.assertNotIn("T_OTC", self.cat)      # OUIGO Train Classique -> GTFS SNCF
        self.assertNotIn("T_TRN_EST", self.cat)  # Eurostar 9xxx -> GTFS Eurostar

    def test_generic_category_resolved(self):
        self.assertEqual(self.cat["T_TRN_IC"], "IC")
        self.assertEqual(self.cat["T_TRN_ICE"], "ICE")
        self.assertEqual(self.cat["T_TRN_UNK"], "TRN")
        self.assertEqual(self.cat["T_IC"], "IC")
        self.assertEqual(self.cat["T_L"], "L")

    def test_resolved_routes_are_consistent(self):
        self.assertTrue(self.trips["route_id"].isin(self.routes["route_id"]).all())
        self.assertFalse(self.routes["route_id"].duplicated().any())

    def test_expired_calendar_removed(self):
        self.assertNotIn("T_OLD", self.cat)
        cd = read_zip(self.out, "calendar_dates.txt")
        self.assertEqual(cd["date"].tolist(), ["20261010"])

    def test_passing_points_removed(self):
        st = read_zip(self.out, "stop_times.txt")
        self.assertNotIn("gs:nmbssncb:8814217", set(st["stop_id"]))
        self.assertEqual(len(st[st["trip_id"] == "T_L"]), 2)
        self.assertFalse(((st["pickup_type"] == "1") & (st["drop_off_type"] == "1")).any())

    def test_parent_stations_kept(self):
        stops = set(read_zip(self.out, "stops.txt")["stop_id"])
        self.assertIn("gs:nmbssncb:S8814001", stops)
        self.assertNotIn("gs:nmbssncb:8821030_1", stops)  # quai inutilisé

    def test_loadable_by_builder(self):
        ref = _ref()
        builder = bn.NetworkBuilder([{"id": "SNCB"}], ref)
        builder.load_feed({"id": "SNCB"}, self.out)
        types = {builder.types[t["type"]] for t in builder.trips.values()}
        self.assertEqual(types, {"SNCB InterCity", "SNCB Local", "SNCB ICE", "SNCB Train"})
        numbers = {t["number"] for t in builder.trips.values()}
        self.assertEqual(numbers, {"2838", "1960", "16", "14403"})
        # Bruxelles-Midi / Anvers-Central reconnues par UIC dans stations.csv
        self.assertEqual({s["id"] for s in builder.stops}, {"8814001", "8821006"})

    def test_duplicate_of_earlier_feed_skipped(self):
        """Un train déjà fourni par la SNCF (même numéro, mêmes horaires, fuseau à la même heure) est écarté."""
        ops = [{"id": "SNCF"}, {"id": "SNCB"}]
        builder = bn.NetworkBuilder(ops, _ref())
        bxl = builder.stop_for("SNCB", {"stop_id": "gs:nmbssncb:8814001_12"}, "Europe/Brussels")
        anr = builder.stop_for("SNCB", {"stop_id": "gs:nmbssncb:8821006_3"}, "Europe/Brussels")
        tz = builder.intern(builder.timezones, "Europe/Paris")
        all_days = (1 << bn.NDAYS) - 1
        builder.trips["sncf"] = {"op": 0, "tz": tz, "stops": [bxl, anr], "flags": [2, 1], "arr": [480, 525],
                                 "dep": [480, 525], "number": "2838", "type": 0, "checkin": 0, "days": all_days}
        builder.load_feed(ops[1], self.out)
        sncb = {t["number"] for t in builder.trips.values() if t["op"] == 1}
        self.assertNotIn("2838", sncb)
        self.assertIn("1960", sncb)
        self.assertGreaterEqual(builder.stats["trips_SNCB_duplicates"], 1)


_REF = None


def _ref():
    global _REF
    if _REF is None:
        _REF = bn.StationRef(bn.STATIONS_CSV)
    return _REF


class TestStationMatch(unittest.TestCase):
    def match(self, stop_id):
        rid = _ref().match("SNCB", stop_id, "")
        return _ref().rows[rid]["uic"] if rid else None

    def test_station_and_platform(self):
        self.assertEqual(self.match("gs:nmbssncb:S8814001"), "8814001")    # Bruxelles-Midi (gare)
        self.assertEqual(self.match("gs:nmbssncb:8814001_12"), "8814001")  # ... quai 12
        self.assertEqual(self.match("gs:nmbssncb:8821006"), "8821006")     # Anvers-Central
        self.assertEqual(self.match("gs:nmbssncb:8841004_A"), "8841004")   # Liège-Guillemins, voie A

    def test_foreign_stations(self):
        self.assertIsNotNone(self.match("gs:nmbssncb:8727100_1"))  # Paris Nord
        self.assertIsNotNone(self.match("gs:nmbssncb:8400058_5"))  # Amsterdam Centraal

    def test_no_uic(self):
        self.assertIsNone(self.match("nmbssncb:s-aachenhbfde"))
        self.assertIsNone(self.match("gs:nmbssncb:88140011"))  # 8 chiffres : pas un UIC SNCB


class TestTripLabels(unittest.TestCase):
    def label(self, short_name, number="1234"):
        b = bn.NetworkBuilder.__new__(bn.NetworkBuilder)
        return b.trip_labels("SNCB", {"trip_short_name": number, "trip_id": "x"},
                             {"route_short_name": short_name}, "gs:nmbssncb:8814001_1")

    def test_categories(self):
        expected = {"IC": "SNCB InterCity", "L": "SNCB Local", "P": "SNCB Heure de pointe",
                    "S1": "SNCB S-Train", "S32": "SNCB S-Train", "T": "SNCB Touristique",
                    "EXT": "SNCB Extra", "EC": "SNCB EuroCity", "ICE": "SNCB ICE",
                    "NJ": "SNCB Nightjet", "TRN": "SNCB Train"}
        for rs, ttype in expected.items():
            self.assertEqual(self.label(rs), ("1234", ttype, 0), rs)

    def test_unknown_category(self):
        self.assertEqual(self.label("XYZ")[1], "SNCB XYZ")
        self.assertEqual(self.label("")[1], "SNCB Train")


@unittest.skipUnless(os.path.exists(REAL_ZIP), "SNCB/sncb_gtfs.zip absent (lancer ingest_sncb_gtfs.py)")
class TestRealFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = read_zip(REAL_ZIP, "routes.txt")
        cls.trips = read_zip(REAL_ZIP, "trips.txt")
        with zipfile.ZipFile(REAL_ZIP) as z:
            cls.stop_ids = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["stop_id"])["stop_id"].unique()

    def test_no_bus_no_duplicates(self):
        cats = set(self.routes["route_short_name"])
        self.assertFalse(cats & ingest.EXCLUDED_CATEGORIES)
        self.assertTrue(self.routes["route_type"].isin(["2"]).all())

    def test_every_category_has_a_type(self):
        for rs in set(self.routes["route_short_name"]):
            cat = "S" if rs.startswith("S") and rs[1:].isdigit() else rs
            self.assertIn(cat, bn.SNCB_TYPES, rs)

    def test_train_numbers(self):
        self.assertTrue((self.trips["trip_short_name"] != "").all())

    def test_stations_matched_by_uic(self):
        ref = _ref()
        matched = [ref.match("SNCB", s, "") is not None for s in self.stop_ids]
        ratio = sum(matched) / len(matched)
        self.assertGreater(ratio, 0.95, f"{ratio:.1%} des arrêts reconnus")
        # toutes les gares belges principales
        for uic in ("8814001", "8813003", "8812005", "8821006", "8892007", "8841004", "8863008", "8872009"):
            self.assertTrue(any(s.split(":")[-1].startswith(uic) for s in self.stop_ids), uic)


def load_bin(path):
    """Lecteur minimal du format TNNET001 (voir BinWriter)."""
    codes = {1: np.uint8, 2: np.uint16, 3: np.uint32, 4: np.int32, 5: np.uint64, 6: np.float32}
    out = {}
    with open(path, "rb") as f:
        assert f.read(8) == b"TNNET001"
        n, _ = struct.unpack("<II", f.read(8))
        for _ in range(n):
            name = f.read(24).rstrip(b"\0").decode()
            code, size, nbytes = struct.unpack("<B7xQQ", f.read(24))
            out[name] = np.frombuffer(f.read(nbytes), codes[code], size)
            f.read(-nbytes % 8)
    return out


def strings(sec, name):
    off, dat = sec[name + ".off"], sec[name + ".dat"].tobytes()
    return [dat[off[i]:off[i + 1]].decode() for i in range(len(off) - 1)]


@unittest.skipUnless(os.path.exists(NETWORK_BIN), "network.bin absent (lancer build_network.py)")
class TestCompiledNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        cls.sec = load_bin(NETWORK_BIN)
        cls.meta = json.loads(cls.sec["meta"].tobytes())
        cls.ops = [o["id"] for o in cls.meta["operators"]]
        if "SNCB" not in cls.ops:
            raise unittest.SkipTest("network.bin compilé sans SNCB")

    def test_sncb_operator_and_types(self):
        op = self.ops.index("SNCB")
        trip_op, trip_type = self.sec["trip.op"], self.sec["trip.type"]
        sncb = trip_op == op
        self.assertGreater(int(sncb.sum()), 10000)
        types = {self.meta["types"][t] for t in np.unique(trip_type[sncb])}
        self.assertTrue(types <= set(bn.SNCB_TYPES.values()), types)
        self.assertIn("SNCB InterCity", types)
        numbers = strings(self.sec, "trip.number")
        self.assertTrue(all(numbers[i] for i in np.flatnonzero(sncb)[:5000]))

    def test_belgian_stations_present(self):
        ids = strings(self.sec, "stop.id")
        countries = strings(self.sec, "stop.country")
        for uic in ("8814001", "8821006", "8892007", "8841004"):
            self.assertIn(uic, set(ids), uic)
        self.assertGreater(sum(c == "BE" for c in countries), 400)


if __name__ == "__main__":
    unittest.main()
