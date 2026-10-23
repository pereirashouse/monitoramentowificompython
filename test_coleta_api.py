"""Coleta UniFi simulada: contrato da resposta e proteção contra quedas falsas."""

import copy
import datetime as dt
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import coletores_wifi_monitor as m


RAW_CLIENT = {
    "mac": "b4:29:3d:00:00:01", "essid": "wms", "name": "coletor-ficticio",
    "ap_mac": "aa:bb:cc:00:00:01", "signal": -55, "rssi": 40, "radio": "ng",
}


def response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "controller_host": "unifi.example.invalid",
            "clients_path": "/proxy/network/api/s/testsite/stat/sta",
            "ssid": "wms", "verify_tls": True, "retry_max_attempts": 1,
        }
        env = patch.dict(os.environ, {"UNIFI_API_KEY": "chave-ficticia-de-teste"})
        env.start()
        self.addCleanup(env.stop)

    def collect(self, payload):
        with patch.object(m.urllib.request, "urlopen", return_value=response(payload)):
            return m.unifi_get_clients(self.cfg)

    def test_aceita_envelope_e_filtra_ssid_com_espacos(self):
        target = dict(RAW_CLIENT, essid=" wms ")
        other = dict(RAW_CLIENT, mac="04:7b:cb:00:00:02", essid="visitantes")
        self.cfg["ssid"] = " wms "
        self.assertEqual(self.collect({"data": [target, other]}), [target])

    def test_aceita_lista_direta_e_campo_ssid_alternativo(self):
        target = dict(RAW_CLIENT, ssid="wms")
        del target["essid"]
        self.assertEqual(self.collect([target]), [target])

    def test_lista_vazia_valida_e_aceita(self):
        for payload in ([], {"data": []}, {"meta": {"rc": "ok"}, "data": []}):
            with self.subTest(payload=payload):
                self.assertEqual(self.collect(payload), [])

    def test_ignora_cliente_explicitamente_cabeado(self):
        wired = {"mac": "02:00:00:00:00:02", "is_wired": True}
        self.assertEqual(self.collect({"meta": {"rc": "ok"}, "data": [wired, RAW_CLIENT]}), [RAW_CLIENT])

    def test_rejeita_topo_ou_data_sem_lista(self):
        invalid = (None, True, 1, "", {}, {"data": None}, {"data": {}}, {"data": ""})
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(m.UnifiResponseError):
                    self.collect(payload)

    def test_rejeita_erro_da_api_sem_expor_mensagem_recebida(self):
        marker = "CONTEUDO_SENSIVEL_FICTICIO_NAO_DEVE_SER_LOGADO"
        invalid = (
            {"meta": {"rc": "error", "msg": marker}, "data": []},
            {"error": marker, "data": []},
            {"error": {"message": marker}, "data": [RAW_CLIENT]},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(m.UnifiResponseError) as caught:
                    self.collect(payload)
                self.assertNotIn(marker, str(caught.exception))

    def test_meta_presente_precisa_confirmar_sucesso(self):
        for meta in (None, False, [], "ok", {}, {"rc": None}, {"rc": "unknown"}):
            with self.subTest(meta=meta):
                with self.assertRaises(m.UnifiResponseError):
                    self.collect({"meta": meta, "data": []})

    def test_rejeita_registro_que_nao_e_objeto(self):
        for item in (None, False, 1, "cliente", []):
            with self.subTest(item=item):
                with self.assertRaises(m.UnifiResponseError):
                    self.collect({"data": [item]})

    def test_rejeita_mac_ausente_vazio_ou_nao_textual(self):
        invalid = (None, "", "   ", 0, [], {})
        for mac in invalid:
            with self.subTest(mac=mac):
                with self.assertRaises(m.UnifiResponseError):
                    self.collect({"data": [dict(RAW_CLIENT, mac=mac)]})
        client = dict(RAW_CLIENT)
        del client["mac"]
        with self.assertRaises(m.UnifiResponseError):
            self.collect({"data": [client]})

    def test_rejeita_cliente_sem_ssid_ou_com_ssid_invalido(self):
        for ssid in (None, "", "   ", 123, [], {}):
            with self.subTest(ssid=ssid):
                with self.assertRaises(m.UnifiResponseError):
                    self.collect({"data": [dict(RAW_CLIENT, essid=ssid)]})
        client = dict(RAW_CLIENT)
        del client["essid"]
        with self.assertRaises(m.UnifiResponseError):
            self.collect({"data": [client]})

    def test_valida_toda_lista_antes_de_filtrar_outro_ssid(self):
        invalid_other = {"mac": None, "essid": "visitantes"}
        with self.assertRaises(m.UnifiResponseError):
            self.collect({"data": [RAW_CLIENT, invalid_other]})

    def test_json_malformado_ou_pagina_html_nao_vira_lista_vazia(self):
        for body in (b"", b'{"data": [', b"<html>login ficticio</html>"):
            with self.subTest(body=body):
                with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                    with self.assertRaises(json.JSONDecodeError):
                        m.unifi_get_clients(self.cfg)

    def test_utf8_invalido_interrompe_coleta(self):
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(b"\xff")):
            with self.assertRaises(UnicodeDecodeError):
                m.unifi_get_clients(self.cfg)


class CollectionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.cfg = {
            "controller_host": "unifi.example.invalid",
            "clients_path": "/proxy/network/api/s/testsite/stat/sta",
            "ssid": "wms", "verify_tls": True,
            "retry_max_attempts": 1,
            "state_dir": str(self.folder), "poll_seconds": 60,
            "alert_drops_per_hour": 3, "collector_macs": {}, "ap_names": {},
        }
        env = patch.dict(os.environ, {"UNIFI_API_KEY": "chave-ficticia-de-teste"})
        env.start()
        self.addCleanup(env.stop)
        collectors = patch.object(m, "CONFIG_COLLECTORS", {})
        collectors.start()
        self.addCleanup(collectors.stop)
        m.poll_once(self.cfg, [m.normalize(RAW_CLIENT)], now=dt.datetime(2026, 10, 23, 9, 0))
        m.pending_alerts(self.cfg, [], "2026-10-23")
        self.before = self.snapshot()

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}

    def assert_failed_snapshot(self, payload):
        with patch.object(m.urllib.request, "urlopen", return_value=response(payload)) as request:
            with self.assertRaises(m.UnifiResponseError):
                m.run_poll(self.cfg)
        request.assert_called_once()
        self.assertEqual(self.snapshot(), self.before)

    def test_envelope_sem_data_preserva_estado_csv_e_alertas(self):
        self.assert_failed_snapshot({})

    def test_meta_error_nao_gera_queda_de_todos_os_clientes(self):
        self.assert_failed_snapshot({"meta": {"rc": "error"}, "data": []})

    def test_erro_explicito_em_http_200_preserva_arquivos(self):
        self.assert_failed_snapshot({"error": "erro-ficticio", "data": []})

    def test_amostra_parcialmente_invalida_nao_e_gravada(self):
        self.assert_failed_snapshot({"data": [RAW_CLIENT, {"mac": None, "essid": "wms"}]})

    def test_ssid_ausente_nao_vira_ausencia_de_cliente(self):
        self.assert_failed_snapshot({"data": [{"mac": RAW_CLIENT["mac"]}]})

    def test_erro_http_preserva_arquivos_com_retentativas_desativadas(self):
        for code in (401, 403, 429, 500, 503):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://unifi.example.invalid", code, "erro ficticio", {}, None)
                with patch.object(m.urllib.request, "urlopen", side_effect=error) as request:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        m.run_poll(self.cfg)
                self.assertIs(caught.exception, error)
                request.assert_called_once()
                self.assertEqual(self.snapshot(), self.before)

    def test_timeout_direto_e_em_urlerror_preserva_arquivos(self):
        errors = (TimeoutError("timeout ficticio"), urllib.error.URLError(TimeoutError("timeout ficticio")),
                  urllib.error.URLError("falha de DNS ficticia"))
        for error in errors:
            with self.subTest(error=error):
                with patch.object(m.urllib.request, "urlopen", side_effect=error) as request:
                    with self.assertRaises(type(error)):
                        m.run_poll(self.cfg)
                request.assert_called_once()
                self.assertEqual(request.call_args.kwargs["timeout"], 20)
                self.assertEqual(self.snapshot(), self.before)

    def test_timeout_durante_leitura_preserva_arquivos(self):
        class SlowResponse(io.BytesIO):
            def read(self, *args, **kwargs):
                raise TimeoutError("timeout de leitura ficticio")
        with patch.object(m.urllib.request, "urlopen", return_value=SlowResponse()):
            with self.assertRaises(TimeoutError):
                m.run_poll(self.cfg)
        self.assertEqual(self.snapshot(), self.before)

    def test_json_ou_utf8_invalidos_preservam_arquivos(self):
        for body, error in ((b'{"data": [', json.JSONDecodeError), (b"\xff", UnicodeDecodeError)):
            with self.subTest(body=body):
                with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                    with self.assertRaises(error):
                        m.run_poll(self.cfg)
                self.assertEqual(self.snapshot(), self.before)

    def test_primeira_coleta_com_erro_nao_cria_pasta_de_dados(self):
        cfg = copy.deepcopy(self.cfg)
        folder = self.folder / "ainda-nao-existe"
        cfg["state_dir"] = str(folder)
        with patch.object(m.urllib.request, "urlopen", return_value=response({"data": None})):
            with self.assertRaises(m.UnifiResponseError):
                m.run_poll(cfg)
        self.assertFalse(folder.exists())

    def test_lista_vazia_valida_ainda_registra_queda_real_observada(self):
        with patch.object(m.urllib.request, "urlopen", return_value=response({"meta": {"rc": "ok"}, "data": []})):
            events, _ = m.run_poll(self.cfg)
        self.assertEqual([e["evento"] for e in events], ["queda"])
        state = m.load_state(str(self.folder / "estado.json"))
        self.assertFalse(state[RAW_CLIENT["mac"]]["online"])
        self.assertEqual([e["evento"] for e in m.read_events(self.cfg)], ["primeira_vez", "queda"])

    def test_recuperacao_apos_falha_nao_inventa_queda_ou_reconexao(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=TimeoutError("timeout ficticio")):
            with self.assertRaises(TimeoutError):
                m.run_poll(self.cfg)
        with patch.object(m.urllib.request, "urlopen", return_value=response({"data": [RAW_CLIENT]})):
            events, _ = m.run_poll(self.cfg)
        self.assertEqual(events, [])
        self.assertEqual([e["evento"] for e in m.read_events(self.cfg)], ["primeira_vez"])
        state = m.load_state(str(self.folder / "estado.json"))
        self.assertTrue(state[RAW_CLIENT["mac"]]["online"])

    def test_loop_continua_apos_falha_e_coleta_valida_seguinte(self):
        error = urllib.error.HTTPError("https://unifi.example.invalid", 503, "falha ficticia", {}, None)
        with patch.object(m, "load_config", return_value=self.cfg), \
                patch.object(m.urllib.request, "urlopen", side_effect=[error, response({"data": [RAW_CLIENT]})]) as request, \
                patch.object(m.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(KeyboardInterrupt):
                m.main(["monitor", "loop"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("Falha na coleta:", output.getvalue())
        self.assertIn("0 evento(s)", output.getvalue())
        self.assertEqual([e["evento"] for e in m.read_events(self.cfg)], ["primeira_vez"])

    def test_amostras_consecutivas_presentes_nao_inferem_queda_entre_elas(self):
        events = m.poll_once(self.cfg, [m.normalize(RAW_CLIENT)], now=dt.datetime(2026, 10, 23, 9, 1))
        self.assertEqual(events, [])
        self.assertEqual([e["evento"] for e in m.read_events(self.cfg)], ["primeira_vez"])


if __name__ == "__main__":
    unittest.main()
