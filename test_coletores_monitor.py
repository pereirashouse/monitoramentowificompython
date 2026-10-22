"""Testes unitários do monitor de coletores (classificação, coleta, eventos e relatório)."""

import csv
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import coletores_wifi_monitor as m


INVENTARIO = {"b4:29:3d:00:00:01": "Urovo DT40 #1"}

CLIENT_A = {"mac": "b4:29:3d:00:00:01", "name": "", "ap": "aa:bb:cc:dd:ee:ff",
            "rssi_dbm": -55, "snr": 40, "band": "ng"}


def cfg_for(tmp, **extra):
    cfg = {"state_dir": tmp, "ssid": "wms", "alert_drops_per_hour": 2,
           "collector_macs": dict(INVENTARIO), "ap_names": {},
           "controller_host": "192.0.2.1",
           "clients_path": "/proxy/network/api/s/testsite/stat/sta",
           "verify_tls": True}
    cfg.update(extra)
    return cfg


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg_for(self.tmp)
        patcher = patch.object(m, "CONFIG_COLLECTORS", self.cfg["collector_macs"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def events_rows(self):
        _, events_file = m.state_paths(self.cfg)
        with open(events_file, encoding="utf-8") as f:
            return list(csv.DictReader(f))


class ClassifyTests(TempDirTestCase):
    def test_inventario(self):
        self.assertEqual(m.classify("B4:29:3D:00:00:01"), ("Urovo DT40 #1", "inventario"))

    def test_oui_urovo(self):
        self.assertEqual(m.classify("b4:29:3d:ff:ff:ff"), ("Urovo", "fabricante"))

    def test_oui_skorpio_1(self):
        self.assertEqual(m.classify("04:7b:cb:ff:ff:ff"), ("Skorpio X5", "fabricante"))

    def test_oui_skorpio_2(self):
        self.assertEqual(m.classify("3c:e1:a1:ff:ff:ff"), ("Skorpio X5", "fabricante"))

    def test_android_mac_aleatorio(self):
        self.assertEqual(m.classify("0a:2c:da:01:02:03", "android-xyz"),
                         ("Android (MAC aleatório)", "nome"))

    def test_desconhecido(self):
        self.assertEqual(m.classify("de:ad:be:ef:00:00"), ("Desconhecido", "nenhuma"))


class PollTests(TempDirTestCase):
    T0 = datetime(2026, 10, 15, 10, 0, 0)
    T1 = datetime(2026, 10, 15, 10, 1, 0)
    T2 = datetime(2026, 10, 15, 10, 3, 30)

    def test_primeira_vez(self):
        events = m.poll_once(self.cfg, [CLIENT_A], now=self.T0)
        self.assertEqual([e["evento"] for e in events], ["primeira_vez"])
        self.assertEqual(events[0]["tipo"], "Urovo DT40 #1")

    def test_queda(self):
        m.poll_once(self.cfg, [CLIENT_A], now=self.T0)
        events = m.poll_once(self.cfg, [], now=self.T1)
        self.assertEqual([e["evento"] for e in events], ["queda"])
        self.assertEqual(events[0]["ap"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(events[0]["rssi_dbm"], -55)

    def test_reconexao_calcula_duracao(self):
        m.poll_once(self.cfg, [CLIENT_A], now=self.T0)
        m.poll_once(self.cfg, [], now=self.T1)
        events = m.poll_once(self.cfg, [CLIENT_A], now=self.T2)
        self.assertEqual([e["evento"] for e in events], ["reconexao"])
        self.assertEqual(events[0]["duracao_s"], 150)

    def test_csv_registra_todos_os_eventos(self):
        m.poll_once(self.cfg, [CLIENT_A], now=self.T0)
        m.poll_once(self.cfg, [], now=self.T1)
        rows = self.events_rows()
        self.assertEqual([r["evento"] for r in rows], ["primeira_vez", "queda"])
        self.assertEqual(list(rows[0].keys()), m.EVENT_FIELDS)

    def test_csv_no_formato_antigo_e_preservado(self):
        _, events_file = m.state_paths(self.cfg)
        with open(events_file, "w", encoding="utf-8") as f:
            f.write("timestamp,mac,tipo,evento,ap,rssi_ultimo,duracao_s\n")
        m.poll_once(self.cfg, [CLIENT_A], now=self.T0)
        legados = [n for n in os.listdir(self.tmp) if n.startswith("eventos_legado_")]
        self.assertEqual(len(legados), 1)
        self.assertEqual(len(self.events_rows()), 1)


class UnifiClientsTests(TempDirTestCase):
    def fake_urlopen(self, clients):
        body = json.dumps({"data": clients}).encode("utf-8")

        def _open(req, context=None, timeout=None):
            return io.BytesIO(body)
        return _open

    def test_ssid_com_espaco_e_aceito(self):
        clients = [{"mac": "a", "essid": " wms"}, {"mac": "b", "essid": "wms"},
                   {"mac": "c", "essid": "visitantes"}]
        with patch.dict(os.environ, {"UNIFI_API_KEY": "chave-de-teste"}), \
                patch.object(m.urllib.request, "urlopen", self.fake_urlopen(clients)):
            result = m.unifi_get_clients(self.cfg)
        self.assertEqual([c["mac"] for c in result], ["a", "b"])

    def test_sem_chave_de_api_falha(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                m.unifi_get_clients(self.cfg)


class ReportTests(TempDirTestCase):
    def test_relatorio_gerado(self):
        m.poll_once(self.cfg, [CLIENT_A], now=datetime(2026, 10, 15, 10, 0))
        m.poll_once(self.cfg, [], now=datetime(2026, 10, 15, 10, 1))
        path = m.write_report(self.cfg, "2026-10-15")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("2026-10-15", content)
        self.assertIn("Quedas no dia: **1**", content)
        self.assertIn("Nenhum alerta no dia.", content)

    def test_relatorio_com_alerta(self):
        for minuto in range(0, 6, 2):
            m.poll_once(self.cfg, [CLIENT_A], now=datetime(2026, 10, 15, 10, minuto))
            m.poll_once(self.cfg, [], now=datetime(2026, 10, 15, 10, minuto + 1))
        content = m.build_report(self.cfg, "2026-10-15")
        self.assertIn("b4:29:3d:00:00:01 às 10h: 3 quedas", content)


if __name__ == "__main__":
    unittest.main()
