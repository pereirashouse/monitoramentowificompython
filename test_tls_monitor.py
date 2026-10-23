"""Regressões TLS: contextos reais e API simulada, sem acesso à controladora."""

import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import coletores_wifi_monitor as m


INVALID_VERIFY_VALUES = ("false", "true", "", None, 0, 1, [], {})
INVALID_CA_VALUES = (False, True, 0, 1, [], {})


class ConfigTlsTests(unittest.TestCase):
    def load(self, values):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(values), encoding="utf-8")
            return m.load_config(str(path))

    def test_campo_omitido_ativa_validacao(self):
        self.assertIs(self.load({})["verify_tls"], True)

    def test_booleanos_explicitos_sao_preservados(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.assertIs(self.load({"verify_tls": value})["verify_tls"], value)

    def test_valores_ambiguos_sao_rejeitados(self):
        for value in INVALID_VERIFY_VALUES:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "booleano JSON"):
                    self.load({"verify_tls": value})

    def test_exemplo_distribuido_e_seguro(self):
        cfg = m.load_config(os.path.join(m.HERE, "config.example.json"))
        self.assertIs(cfg["verify_tls"], True)
        self.assertIsNone(cfg["ca_file"])

    def test_tipo_invalido_de_ca_e_rejeitado_no_carregamento(self):
        for verify in (True, False):
            for value in INVALID_CA_VALUES:
                with self.subTest(verify=verify, value=value):
                    with self.assertRaisesRegex(ValueError, "ca_file"):
                        self.load({"verify_tls": verify, "ca_file": value})


class TlsContextTests(unittest.TestCase):
    def test_contexto_padrao_valida_certificado_e_hostname(self):
        for cfg in ({}, {"verify_tls": True}, {"verify_tls": True, "ca_file": None}):
            with self.subTest(cfg=cfg):
                ctx = m.create_tls_context(cfg)
                self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(ctx.check_hostname)

    def test_caminho_da_ca_e_passado_ao_contexto_seguro(self):
        secure_context = ssl.create_default_context()
        with patch.object(m.ssl, "create_default_context", return_value=secure_context) as factory:
            ctx = m.create_tls_context({"verify_tls": True, "ca_file": "/ca/interna.pem"})
        factory.assert_called_once_with(cafile="/ca/interna.pem")
        self.assertIs(ctx, secure_context)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_ca_vazia_usa_cas_padrao(self):
        secure_context = ssl.create_default_context()
        with patch.object(m.ssl, "create_default_context", return_value=secure_context) as factory:
            m.create_tls_context({"verify_tls": True, "ca_file": ""})
        factory.assert_called_once_with(cafile=None)

    def test_ca_inexistente_falha_sem_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                m.create_tls_context({"verify_tls": True, "ca_file": str(Path(tmp) / "ausente.pem")})

    def test_ca_invalida_falha_sem_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalida.pem"
            path.write_text("Isto não é um certificado PEM.\n", encoding="utf-8")
            with self.assertRaises(ssl.SSLError):
                m.create_tls_context({"verify_tls": True, "ca_file": str(path)})

    def test_modo_inseguro_explicito_avisa_e_ignora_ca(self):
        with patch.object(m.ssl, "create_default_context") as secure_factory:
            with self.assertWarnsRegex(RuntimeWarning, "interceptação da chave de API"):
                ctx = m.create_tls_context({"verify_tls": False, "ca_file": "/ca/ausente.pem"})
        secure_factory.assert_not_called()
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_contexto_tambem_rejeita_configuracao_ambigua(self):
        for value in INVALID_VERIFY_VALUES:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "booleano JSON"):
                    m.create_tls_context({"verify_tls": value})

    def test_tipo_invalido_de_ca_nao_usa_cas_padrao(self):
        with patch.object(m.ssl, "create_default_context") as factory:
            for value in INVALID_CA_VALUES:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "ca_file"):
                        m.create_tls_context({"verify_tls": True, "ca_file": value})
            factory.assert_not_called()


class UnifiTlsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "controller_host": "unifi.example.invalid",
            "clients_path": "/proxy/network/api/s/testsite/stat/sta",
            "ssid": "wms",
            "verify_tls": True,
        }
        env = patch.dict(os.environ, {"UNIFI_API_KEY": "chave-ficticia-de-teste"})
        env.start()
        self.addCleanup(env.stop)

    def test_requisicao_recebe_contexto_seguro_com_campo_omitido(self):
        del self.cfg["verify_tls"]
        body = json.dumps({"data": [{"mac": "ficticio", "essid": "wms"}]}).encode("utf-8")
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(body)) as request:
            clients = m.unifi_get_clients(self.cfg)
        self.assertEqual(len(clients), 1)
        request.assert_called_once()
        ctx = request.call_args.kwargs["context"]
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(request.call_args.kwargs["timeout"], 20)
        self.assertTrue(request.call_args.args[0].full_url.startswith("https://"))

    def test_erro_de_certificado_nao_repete_coleta_nem_registra_quedas(self):
        reason = ssl.SSLCertVerificationError("certificado de teste não confiável")
        with patch.object(m.urllib.request, "urlopen", side_effect=urllib.error.URLError(reason)) as request, \
                patch.object(m, "poll_once") as poll, \
                patch.object(m, "create_tls_context", wraps=m.create_tls_context) as contexts:
            with self.assertRaises(urllib.error.URLError) as caught:
                m.run_poll(self.cfg)
        self.assertIs(caught.exception.reason, reason)
        request.assert_called_once()
        contexts.assert_called_once_with(self.cfg)
        poll.assert_not_called()
        ctx = request.call_args.kwargs["context"]
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_ca_invalida_impede_envio_da_requisicao(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.cfg["ca_file"] = str(Path(tmp) / "ausente.pem")
            with patch.object(m.urllib.request, "urlopen") as request:
                with self.assertRaises(FileNotFoundError):
                    m.unifi_get_clients(self.cfg)
            request.assert_not_called()

    def test_tipo_invalido_de_ca_impede_requisicao(self):
        self.cfg["ca_file"] = False
        with patch.object(m.urllib.request, "urlopen") as request:
            with self.assertRaisesRegex(ValueError, "ca_file"):
                m.unifi_get_clients(self.cfg)
        request.assert_not_called()

    def test_modo_inseguro_requer_false_e_emite_aviso_na_coleta(self):
        self.cfg["verify_tls"] = False
        with patch.object(m.urllib.request, "urlopen", return_value=io.BytesIO(b'{"data": []}')) as request:
            with self.assertWarnsRegex(RuntimeWarning, "nunca em produção"):
                self.assertEqual(m.unifi_get_clients(self.cfg), [])
        ctx = request.call_args.kwargs["context"]
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
