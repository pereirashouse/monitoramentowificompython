import shutil
import tempfile
import unittest

import coletores_wifi_monitor as m


def cfg_for(tmp):
    return {"state_dir": tmp, "alert_drops_per_hour": 3, "ssid": "wms",
            "collector_macs": {}, "ap_names": {"ac:8b:a9:00:00:01": "Área de expedição - 6"}}


class PendingAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg_for(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_alerta_emitido_uma_vez_por_hora(self):
        alertas = [("04:7b:cb:aa:bb:01", "14", 4)]
        self.assertEqual(m.pending_alerts(self.cfg, alertas, "2026-10-15"), alertas)
        self.assertEqual(m.pending_alerts(self.cfg, alertas, "2026-10-15"), [])

    def test_novo_alerta_na_mesma_execucao_aparece(self):
        m.pending_alerts(self.cfg, [("04:7b:cb:aa:bb:01", "14", 4)], "2026-10-15")
        novos = m.pending_alerts(self.cfg, [("04:7b:cb:aa:bb:01", "14", 4),
                                            ("04:7b:cb:aa:bb:01", "15", 5)], "2026-10-15")
        self.assertEqual(novos, [("04:7b:cb:aa:bb:01", "15", 5)])


class NormalizeApTests(unittest.TestCase):
    def test_ap_mac_vira_nome_pelo_mapa(self):
        n = m.normalize({"mac": "AA:BB:CC:00:00:01", "ap_mac": "AC:8B:A9:00:00:01",
                         "signal": -60, "rssi": 35, "radio": "ng"},
                        {"ac:8b:a9:00:00:01": "Área de expedição - 6"})
        self.assertEqual(n["ap"], "Área de expedição - 6")

    def test_ap_sem_mapa_fica_com_mac(self):
        n = m.normalize({"mac": "AA:BB:CC:00:00:01", "ap_mac": "AC:8B:A9:00:00:01"})
        self.assertEqual(n["ap"], "ac:8b:a9:00:00:01")

    def test_ap_name_da_api_tem_prioridade(self):
        n = m.normalize({"mac": "AA:BB:CC:00:00:01", "ap_mac": "ac:8b:a9:00:00:01",
                         "ap_name": "Expedicao-1"},
                        {"ac:8b:a9:00:00:01": "Outro"})
        self.assertEqual(n["ap"], "Expedicao-1")


if __name__ == "__main__":
    unittest.main()
