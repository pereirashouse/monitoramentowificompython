"""Testes unitários para CollectorMonitor."""

import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import coletores_wifi_monitor as m


BASE_CONFIG = {
    "controller_url": "https://192.0.2.1",
    "clients_path": "/proxy/network/api/s/testsite/stat/sta",
    "ssid": "wms",
    "alert_drops_per_hour": 2,
    "collector_macs": {
        "b4:29:3d:00:00:01": "Urovo DT40 #1",
    },
}

CLIENT_A = {"mac": "b4:29:3d:00:00:01", "essid": "wms", "ap_mac": "aa:bb:cc:dd:ee:ff", "rssi": -55, "hostname": ""}
CLIENT_B = {"mac": "04:7b:cb:00:00:02", "essid": "wms", "ap_mac": "aa:bb:cc:dd:ee:ff", "rssi": -62, "hostname": ""}
CLIENT_C = {"mac": "0a:2c:da:01:02:03", "essid": " wms", "ap_mac": "aa:bb:cc:dd:ee:ff", "rssi": -70, "hostname": "android-xyz"}


def make_monitor(config=None) -> m.CollectorMonitor:
    cfg = config or BASE_CONFIG
    with patch.dict(os.environ, {"UNIFI_API_KEY": "test-key"}):
        mon = m.CollectorMonitor(cfg)
    return mon


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.mon = make_monitor()

    def test_inventory_match(self):
        self.assertEqual(self.mon._classify("b4:29:3d:00:00:01", ""), "Urovo DT40 #1")

    def test_oui_urovo(self):
        self.assertEqual(self.mon._classify("b4:29:3d:ff:ff:ff", ""), "Urovo")

    def test_oui_skorpio_1(self):
        self.assertEqual(self.mon._classify("04:7b:cb:ff:ff:ff", ""), "Skorpio")

    def test_oui_skorpio_2(self):
        self.assertEqual(self.mon._classify("3c:e1:a1:ff:ff:ff", ""), "Skorpio")

    def test_android_random_mac(self):
        self.assertEqual(self.mon._classify("0a:2c:da:01:02:03", "android-xyz"), "Android (MAC aleatório)")

    def test_unknown(self):
        self.assertEqual(self.mon._classify("de:ad:be:ef:00:00", ""), "Desconhecido")


class TestPoll(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_data = m.DATA_DIR
        m.DATA_DIR = Path(self.tmpdir.name)
        self.mon = make_monitor()
        self.mon._state_file = m.DATA_DIR / "estado.json"
        self.mon._events_file = m.DATA_DIR / "eventos.csv"
        self.mon._state = {"online": {}, "last_seen": {}}

    def tearDown(self):
        m.DATA_DIR = self.orig_data
        self.tmpdir.cleanup()

    def _patch_fetch(self, clients):
        self.mon._fetch_clients = MagicMock(return_value=clients)

    def test_primeira_vez(self):
        self._patch_fetch([CLIENT_A])
        events = self.mon.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["evento"], "primeira_vez")
        self.assertIn("b4:29:3d:00:00:01", self.mon._state["online"])

    def test_queda(self):
        self.mon._state["online"]["b4:29:3d:00:00:01"] = {"ap": "aa", "rssi": -55, "hostname": "", "tipo": "Urovo DT40 #1"}
        self._patch_fetch([])
        events = self.mon.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["evento"], "queda")
        self.assertNotIn("b4:29:3d:00:00:01", self.mon._state["online"])

    def test_reconexao(self):
        self.mon._state["last_seen"]["b4:29:3d:00:00:01"] = {"ts": "2024-01-01T10:00:00", "ap": "aa", "rssi": -55, "tipo": "Urovo DT40 #1"}
        self._patch_fetch([CLIENT_A])
        events = self.mon.poll()
        reconex = [e for e in events if e["evento"] == "reconexao"]
        self.assertEqual(len(reconex), 1)
        self.assertTrue(int(reconex[0]["duracao_s"]) > 0)

    def test_ssid_strip(self):
        """SSID com espaço à esquerda deve ser aceito."""
        self._patch_fetch([CLIENT_C])
        events = self.mon.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["evento"], "primeira_vez")

    def test_falha_na_coleta_retorna_lista_vazia(self):
        self.mon._fetch_clients = MagicMock(side_effect=Exception("timeout"))
        events = self.mon.poll()
        self.assertEqual(events, [])

    def test_csv_criado(self):
        self._patch_fetch([CLIENT_A])
        self.mon.poll()
        self.assertTrue(self.mon._events_file.exists())
        with open(self.mon._events_file) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_data = m.DATA_DIR
        m.DATA_DIR = Path(self.tmpdir.name)
        self.mon = make_monitor()
        self.mon._state_file = m.DATA_DIR / "estado.json"
        self.mon._events_file = m.DATA_DIR / "eventos.csv"
        self.mon._state = {"online": {}, "last_seen": {}}

    def tearDown(self):
        m.DATA_DIR = self.orig_data
        self.tmpdir.cleanup()

    def _write_events(self, rows):
        with open(self.mon._events_file, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=m._CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)

    def test_report_gerado(self):
        self._write_events([
            {"timestamp": "2024-03-15T10:00:00", "mac": "b4:29:3d:00:00:01", "tipo": "Urovo DT40 #1",
             "evento": "queda", "ap": "aa", "rssi": -55, "duracao_s": ""},
        ])
        path = self.mon.report("2024-03-15")
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("2024-03-15", content)
        self.assertIn("Total de quedas", content)

    def test_report_alerta(self):
        rows = []
        for h in range(3):
            rows.append({
                "timestamp": f"2024-03-15T10:0{h}:00", "mac": "04:7b:cb:00:00:02", "tipo": "Skorpio",
                "evento": "queda", "ap": "aa", "rssi": -65, "duracao_s": "",
            })
        self._write_events(rows)
        path = self.mon.report("2024-03-15")
        content = path.read_text()
        self.assertIn("Alertas", content)


if __name__ == "__main__":
    unittest.main()
