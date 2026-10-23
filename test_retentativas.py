"""Retentativas com API e espera simuladas: nunca acessa a controladora."""

import datetime as dt
import io
import json
import os
import socket
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import call, patch

import coletores_wifi_monitor as m


RAW = {"mac": "b4:29:3d:00:00:01", "essid": "wms", "name": "ficticio", "ap_name": "AP ficticia"}


def response(clients=None):
    return io.BytesIO(json.dumps({"data": [RAW] if clients is None else clients}).encode())


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://unifi.example.invalid", code, "erro ficticio", headers or {}, None)


class RetryConfigTests(unittest.TestCase):
    def test_defaults_incluem_primeira_tentativa(self):
        self.assertEqual(m.validate_retry_options({}), (3, 2.0, 30.0))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            cfg = m.load_config(str(path))
        self.assertEqual(cfg["retry_max_attempts"], 3)
        self.assertEqual(cfg["retry_backoff_seconds"], 2)
        self.assertEqual(cfg["retry_max_delay_seconds"], 30)

    def test_uma_tentativa_desativa_retentativas(self):
        self.assertEqual(m.validate_retry_options({"retry_max_attempts": 1})[0], 1)

    def test_rejeita_tentativas_fora_do_intervalo_ou_tipo(self):
        for value in (None, True, False, 0, -1, 6, 1.5, "3", [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "retry_max_attempts"):
                    m.validate_retry_options({"retry_max_attempts": value})

    def test_rejeita_pausas_ambiguas_nao_finitas_ou_fora_do_intervalo(self):
        for field in ("retry_backoff_seconds", "retry_max_delay_seconds"):
            for value in (None, True, False, 0, -1, 301, "2", [], {}, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        m.validate_retry_options({field: value})

    def test_teto_deve_ser_maior_ou_igual_a_base(self):
        with self.assertRaisesRegex(ValueError, "maior ou igual"):
            m.validate_retry_options({"retry_backoff_seconds": 20, "retry_max_delay_seconds": 10})

    def test_exemplo_distribuido_preserva_tls_e_limites(self):
        cfg = m.load_config(os.path.join(m.HERE, "config.example.json"))
        self.assertIs(cfg["verify_tls"], True)
        self.assertEqual(m.validate_retry_options(cfg), (3, 2.0, 30.0))


class RetryPolicyTests(unittest.TestCase):
    def test_erros_http_transitorios_conhecidos(self):
        for code in (408, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertTrue(m.is_retryable_collection_error(http_error(code)))

    def test_autenticacao_limite_e_outros_http_nao_sao_repetidos(self):
        for code in (400, 401, 403, 404, 429, 501):
            with self.subTest(code=code):
                self.assertFalse(m.is_retryable_collection_error(http_error(code)))

    def test_retry_after_impede_retentativa_dentro_da_coleta(self):
        for code in (500, 503):
            with self.subTest(code=code):
                self.assertFalse(m.is_retryable_collection_error(http_error(code, {"Retry-After": "60"})))

    def test_timeout_conexao_e_dns_temporario_sao_repetiveis(self):
        for reason in (TimeoutError(), ConnectionResetError(), ConnectionRefusedError(),
                       socket.gaierror(socket.EAI_AGAIN, "DNS temporario ficticio")):
            with self.subTest(reason=reason):
                self.assertTrue(m.is_retryable_collection_error(reason))
                self.assertTrue(m.is_retryable_collection_error(urllib.error.URLError(reason)))

    def test_dns_permanente_motivo_desconhecido_e_tls_nao_sao_repetidos(self):
        reasons = (socket.gaierror(socket.EAI_NONAME, "DNS permanente ficticio"),
                   ssl.SSLCertVerificationError("certificado ficticio"), ssl.SSLError("TLS ficticio"))
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertFalse(m.is_retryable_collection_error(reason))
                self.assertFalse(m.is_retryable_collection_error(urllib.error.URLError(reason)))
        self.assertFalse(m.is_retryable_collection_error(urllib.error.URLError("motivo desconhecido")))


class RetryFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.cfg = {
            "controller_host": "unifi.example.invalid", "clients_path": "/api/teste",
            "ssid": "wms", "verify_tls": True, "state_dir": str(self.folder),
            "collector_macs": {}, "ap_names": {}, "alert_drops_per_hour": 3,
            "retry_max_attempts": 3, "retry_backoff_seconds": 2, "retry_max_delay_seconds": 30,
        }
        patches = {
            "env": patch.dict(os.environ, {"UNIFI_API_KEY": "CHAVE_FICTICIA_NAO_LOGAR"}),
            "collectors": patch.object(m, "CONFIG_COLLECTORS", {}),
            "sleep": patch.object(m.time, "sleep"),
            "jitter": patch.object(m.random, "uniform", side_effect=lambda low, high: high),
            "warn": patch.object(m.warnings, "warn"),
        }
        for name, patcher in patches.items():
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        m.poll_once(self.cfg, [m.normalize(RAW)], now=dt.datetime(2026, 10, 23, 9, 0))
        m.pending_alerts(self.cfg, [], "2026-10-23")
        self.before = self.snapshot()

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}

    def test_sucesso_na_primeira_tentativa_nao_espera(self):
        with patch.object(m.urllib.request, "urlopen", return_value=response()) as request:
            self.assertEqual(m.unifi_get_clients(self.cfg), [RAW])
        request.assert_called_once()
        self.sleep.assert_not_called()
        self.jitter.assert_not_called()
        self.warn.assert_not_called()

    def test_falha_http_transitoria_seguida_de_sucesso(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=[http_error(503), response()]) as request:
            self.assertEqual(m.unifi_get_clients(self.cfg), [RAW])
        self.assertEqual(request.call_count, 2)
        self.sleep.assert_called_once_with(2.0)
        self.jitter.assert_called_once_with(1.0, 2.0)
        self.assertEqual(self.snapshot(), self.before)

    def test_esgotamento_usa_tres_tentativas_e_duas_pausas(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=lambda *a, **k: self.raise_http()) as request:
            with self.assertRaises(urllib.error.HTTPError):
                m.run_poll(self.cfg)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(2.0), call(4.0)])
        self.assertEqual(self.jitter.call_args_list, [call(1.0, 2.0), call(2.0, 4.0)])
        self.assertEqual(self.warn.call_count, 2)
        self.assertEqual(self.snapshot(), self.before)

    @staticmethod
    def raise_http():
        raise http_error(503)

    def test_backoff_respeita_teto_e_limite_de_cinco_tentativas(self):
        self.cfg.update(retry_max_attempts=5, retry_backoff_seconds=8, retry_max_delay_seconds=10)
        with patch.object(m.urllib.request, "urlopen", side_effect=TimeoutError("timeout ficticio")) as request:
            with self.assertRaises(TimeoutError):
                m.run_poll(self.cfg)
        self.assertEqual(request.call_count, 5)
        self.assertEqual(self.sleep.call_args_list, [call(8.0), call(10.0), call(10.0), call(10.0)])
        self.assertEqual(self.jitter.call_args_list, [call(4.0, 8.0), call(5.0, 10.0), call(5.0, 10.0), call(5.0, 10.0)])
        self.assertEqual(self.snapshot(), self.before)

    def test_desativar_retentativas_preserva_erro_original(self):
        self.cfg["retry_max_attempts"] = 1
        error = http_error(500)
        with patch.object(m.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                m.run_poll(self.cfg)
        self.assertIs(caught.exception, error)
        request.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(self.snapshot(), self.before)

    def test_timeout_direto_e_encapsulado_podem_recuperar(self):
        for error in (TimeoutError("ficticio"), urllib.error.URLError(TimeoutError("ficticio"))):
            with self.subTest(error=error):
                self.sleep.reset_mock()
                with patch.object(m.urllib.request, "urlopen", side_effect=[error, response()]) as request:
                    self.assertEqual(m.unifi_get_clients(self.cfg), [RAW])
                self.assertEqual(request.call_count, 2)
                self.sleep.assert_called_once()
        self.assertEqual(self.snapshot(), self.before)

    def test_dns_temporario_direto_e_encapsulado_recupera(self):
        errors = (socket.gaierror(socket.EAI_AGAIN, "DNS temporario ficticio"),
                  urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "DNS temporario ficticio")))
        for error in errors:
            with self.subTest(error=error):
                self.sleep.reset_mock()
                with patch.object(m.urllib.request, "urlopen", side_effect=[error, response()]) as request:
                    self.assertEqual(m.unifi_get_clients(self.cfg), [RAW])
                self.assertEqual(request.call_count, 2)
                self.sleep.assert_called_once()
        self.assertEqual(self.snapshot(), self.before)

    def test_dns_permanente_direto_e_encapsulado_nao_retenta(self):
        errors = (socket.gaierror(socket.EAI_NONAME, "DNS permanente ficticio"),
                  urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "DNS permanente ficticio")))
        for error in errors:
            with self.subTest(error=error):
                with patch.object(m.urllib.request, "urlopen", side_effect=error) as request:
                    with self.assertRaises(type(error)):
                        m.run_poll(self.cfg)
                request.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(self.snapshot(), self.before)

    def test_timeout_de_leitura_fecha_resposta_e_retenta(self):
        class SlowResponse(io.BytesIO):
            def read(self, *args, **kwargs):
                raise TimeoutError("leitura ficticia")
        slow = SlowResponse()
        with patch.object(m.urllib.request, "urlopen", side_effect=[slow, response()]):
            self.assertEqual(m.unifi_get_clients(self.cfg), [RAW])
        self.assertTrue(slow.closed)
        self.sleep.assert_called_once()

    def test_retentativa_http_libera_corpo_da_resposta(self):
        body = io.BytesIO(b"corpo ficticio")
        error = urllib.error.HTTPError("https://unifi.example.invalid", 503, "ficticio", {}, body)
        with patch.object(m.urllib.request, "urlopen", side_effect=[error, response()]):
            self.assertEqual(m.unifi_get_clients(self.cfg), [RAW])
        self.assertTrue(body.closed)

    def test_autenticacao_429_e_retry_after_nao_retenta_nem_espera(self):
        errors = (http_error(401), http_error(403), http_error(429), http_error(503, {"Retry-After": "60"}))
        for error in errors:
            with self.subTest(code=error.code, headers=error.headers):
                with patch.object(m.urllib.request, "urlopen", side_effect=error) as request:
                    with self.assertRaises(urllib.error.HTTPError):
                        m.run_poll(self.cfg)
                request.assert_called_once()
                self.assertEqual(self.snapshot(), self.before)
        self.sleep.assert_not_called()
        self.warn.assert_not_called()

    def test_falhas_tls_nao_sao_repetidas(self):
        for error in (ssl.SSLCertVerificationError("certificado ficticio"),
                      urllib.error.URLError(ssl.SSLCertVerificationError("certificado ficticio")),
                      urllib.error.URLError(ssl.SSLError("protocolo ficticio"))):
            with self.subTest(error=error):
                with patch.object(m.urllib.request, "urlopen", side_effect=error) as request:
                    with self.assertRaises(type(error)):
                        m.run_poll(self.cfg)
                request.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(self.snapshot(), self.before)

    def test_json_invalido_na_segunda_tentativa_interrompe_sem_terceira(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=[TimeoutError(), io.BytesIO(b"<html>ficticio</html>")]) as request:
            with self.assertRaises(json.JSONDecodeError):
                m.run_poll(self.cfg)
        self.assertEqual(request.call_count, 2)
        self.sleep.assert_called_once()
        self.assertEqual(self.snapshot(), self.before)

    def test_resposta_invalida_nao_e_repetida(self):
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(b'{"data": null}')) as request:
            with self.assertRaises(m.UnifiResponseError):
                m.run_poll(self.cfg)
        request.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(self.snapshot(), self.before)

    def test_configuracao_invalida_nao_envia_requisicao(self):
        self.cfg["retry_max_attempts"] = True
        with patch.object(m.urllib.request, "urlopen") as request:
            with self.assertRaises(ValueError):
                m.run_poll(self.cfg)
        request.assert_not_called()
        self.sleep.assert_not_called()

    def test_chave_ausente_nao_e_repetida(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(m.urllib.request, "urlopen") as request:
                with self.assertRaises(RuntimeError):
                    m.run_poll(self.cfg)
        request.assert_not_called()
        self.sleep.assert_not_called()

    def test_retentativas_preservam_politica_tls_em_todas_as_requisicoes(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=[TimeoutError(), response()]) as request:
            m.unifi_get_clients(self.cfg)
        for attempt in request.call_args_list:
            ctx = attempt.kwargs["context"]
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(attempt.kwargs["timeout"], 20)
        self.assertIs(self.cfg["verify_tls"], True)

    def test_avisos_nao_expoem_chave_ou_motivo_da_falha(self):
        marker = "CONTEUDO_FICTICIO_NAO_LOGAR"
        error = urllib.error.URLError(ConnectionResetError(marker))
        with patch.object(m.urllib.request, "urlopen", side_effect=[error, response()]):
            m.unifi_get_clients(self.cfg)
        text = self.warn.call_args.args[0]
        self.assertIn("Retentativa 2/3", text)
        self.assertNotIn(marker, text)
        self.assertNotIn(os.environ["UNIFI_API_KEY"], text)

    def test_recuperacao_grava_uma_amostra_sem_queda_falsa(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=[TimeoutError(), response()]), \
                patch.object(m, "poll_once", wraps=m.poll_once) as poll:
            events, _ = m.run_poll(self.cfg)
        poll.assert_called_once()
        self.assertEqual(events, [])
        self.assertEqual((self.folder / "eventos.csv").read_bytes(), self.before["eventos.csv"])
        state = m.load_state(str(self.folder / "estado.json"))
        self.assertTrue(state[RAW["mac"]]["online"])

    def test_lista_vazia_valida_apos_retentativa_ainda_registra_queda(self):
        with patch.object(m.urllib.request, "urlopen", side_effect=[TimeoutError(), response([])]):
            events, _ = m.run_poll(self.cfg)
        self.assertEqual([e["evento"] for e in events], ["queda"])
        self.assertEqual([e["evento"] for e in m.read_events(self.cfg)], ["primeira_vez", "queda"])

    def test_cancelamento_durante_backoff_nao_faz_nova_requisicao(self):
        self.sleep.side_effect = KeyboardInterrupt
        with patch.object(m.urllib.request, "urlopen", side_effect=TimeoutError()) as request:
            with self.assertRaises(KeyboardInterrupt):
                m.run_poll(self.cfg)
        request.assert_called_once()
        self.assertEqual(self.snapshot(), self.before)


if __name__ == "__main__":
    unittest.main()
