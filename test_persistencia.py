"""Persistência com dados fictícios, arquivos temporários e falhas simuladas."""

import copy
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coletores_wifi_monitor as m


MAC = "b4:29:3d:00:00:01"
CLIENT = {"mac": MAC, "name": "ficticio", "ap": "AP ficticia", "rssi_dbm": -55, "snr": 40, "band": "ng"}


def state(online=True):
    return {MAC: {"online": online, "name": "ficticio", "ap": "AP ficticia", "band": "ng",
                  "last_seen": "2026-10-23T10:00:00", "queda_em": None if online else "2026-10-23T10:01:00"}}


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / "estado.json"
        self.backup = self.folder / "estado.json.bak"
        self.cfg = {"state_dir": str(self.folder), "ssid": "wms", "alert_drops_per_hour": 3,
                    "collector_macs": {}, "ap_names": {}, "poll_seconds": 60}
        patcher = patch.object(m, "CONFIG_COLLECTORS", {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def temporary_files(self):
        return list(self.folder.glob(".*.tmp"))

    def seed_backup(self):
        m.save_state(self.path, state(True))
        m.save_state(self.path, state(False))
        self.assertEqual(m.read_state_file(self.backup), state(True))

    def test_primeira_gravacao_cria_estado_valido_sem_backup_inventado(self):
        m.save_state(self.path, state())
        self.assertEqual(m.load_state(self.path), state())
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.temporary_files(), [])

    def test_segunda_gravacao_preserva_ultimo_estado_anterior_valido(self):
        self.seed_backup()
        self.assertEqual(m.load_state(self.path), state(False))
        self.assertEqual(self.temporary_files(), [])

    def test_fsync_ocorre_antes_de_substituir_destino(self):
        order = []
        real_replace = m.os.replace
        with patch.object(m.os, "fsync", side_effect=lambda fd: order.append("fsync")), \
                patch.object(m.os, "replace", side_effect=lambda source, target: (order.append("replace"), real_replace(source, target))[1]):
            m.atomic_write_text(self.path, "ação fictícia\n")
        self.assertEqual(order, ["fsync", "replace"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "ação fictícia\n")

    def test_falha_de_fsync_nao_altera_destino_e_limpa_temporario(self):
        self.path.write_bytes(b"conteudo anterior ficticio\n")
        with patch.object(m.os, "fsync", side_effect=OSError("erro ficticio")):
            with self.assertRaises(m.PersistenceError):
                m.atomic_write_text(self.path, "novo conteudo ficticio\n")
        self.assertEqual(self.path.read_bytes(), b"conteudo anterior ficticio\n")
        self.assertEqual(self.temporary_files(), [])

    def test_falha_de_replace_nao_trunca_destino(self):
        self.path.write_bytes(b"anterior ficticio\n")
        with patch.object(m.os, "replace", side_effect=PermissionError("erro ficticio")):
            with self.assertRaises(m.PersistenceError):
                m.atomic_write_text(self.path, "novo ficticio\n")
        self.assertEqual(self.path.read_bytes(), b"anterior ficticio\n")
        self.assertEqual(self.temporary_files(), [])

    def test_falha_de_criacao_do_temporario_preserva_destino(self):
        self.path.write_bytes(b"anterior ficticio\n")
        with patch.object(m.tempfile, "mkstemp", side_effect=OSError("disco cheio ficticio")):
            with self.assertRaises(m.PersistenceError):
                m.atomic_write_text(self.path, "novo ficticio\n")
        self.assertEqual(self.path.read_bytes(), b"anterior ficticio\n")

    def test_temporario_antigo_nao_e_promovido_ou_sobrescrito(self):
        old = self.folder / "estado.json.tmp"
        old.write_bytes(b"temporario antigo ficticio\n")
        m.save_state(self.path, state())
        self.assertEqual(old.read_bytes(), b"temporario antigo ficticio\n")
        self.assertEqual(m.load_state(self.path), state())

    def test_estado_json_ou_utf8_corrompido_nao_vira_estado_vazio(self):
        for content in (b"", b'{"parcial":', b"\xff"):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(m.PersistenceError):
                    m.load_state(self.path)
                self.assertEqual(self.path.read_bytes(), content)

    def test_estado_com_schema_invalido_e_rejeitado(self):
        values = [None, [], "texto", {MAC: None}, {MAC: {"online": "false"}},
                  {MAC: {"online": False}}, {MAC: {"online": True, "queda_em": "2026-10-23T10:00:00"}},
                  {MAC: {"online": False, "queda_em": "data-invalida"}},
                  {MAC: {"online": True, "last_seen": "2026-10-23T10:00:00+00:00"}},
                  {MAC: {"online": True, "name": 12}}]
        for value in values:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(m.PersistenceError):
                    m.load_state(self.path)

    def test_valor_nao_finito_nao_e_gravado(self):
        m.save_state(self.path, state())
        before = self.path.read_bytes()
        value = state()
        value[MAC]["rssi_dbm"] = float("nan")
        with self.assertRaises(m.PersistenceError):
            m.save_state(self.path, value)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.backup.exists())

    def test_backup_nao_recebe_estado_corrompido(self):
        self.seed_backup()
        before = self.backup.read_bytes()
        self.path.write_bytes(b"estado corrompido ficticio")
        with self.assertRaises(m.PersistenceError):
            m.save_state(self.path, state())
        self.assertEqual(self.backup.read_bytes(), before)
        self.assertEqual(self.path.read_bytes(), b"estado corrompido ficticio")

    def test_falha_ao_salvar_backup_nao_substitui_estado_atual(self):
        self.seed_backup()
        before = self.path.read_bytes()
        backup_before = self.backup.read_bytes()
        with patch.object(m.os, "replace", side_effect=OSError("erro ficticio")):
            with self.assertRaises(m.PersistenceError):
                m.save_state(self.path, state())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.backup.read_bytes(), backup_before)

    def test_falha_ao_substituir_estado_preserva_atual_e_backup_valido(self):
        self.seed_backup()
        before = self.path.read_bytes()
        real_replace = m.os.replace
        def replace(source, target):
            if Path(target) == self.path:
                raise OSError("erro ficticio no estado")
            return real_replace(source, target)
        with patch.object(m.os, "replace", side_effect=replace):
            with self.assertRaises(m.PersistenceError):
                m.save_state(self.path, state())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(m.read_state_file(self.backup), state(False))
        self.assertEqual(self.temporary_files(), [])

    def test_estado_ausente_com_backup_nao_reinicia_do_zero(self):
        self.seed_backup()
        self.path.unlink()
        with self.assertRaises(m.PersistenceError):
            m.load_state(self.path)
        with self.assertRaises(m.PersistenceError):
            m.save_state(self.path, state())

    def test_estado_ausente_com_historico_nao_reinicia_do_zero(self):
        m.poll_once(self.cfg, [CLIENT], now=dt.datetime(2026, 10, 23, 10, 0))
        self.path.unlink()
        with self.assertRaises(m.PersistenceError):
            m.poll_once(self.cfg, [CLIENT], now=dt.datetime(2026, 10, 23, 10, 1))
        self.assertEqual(len(m.read_events(self.cfg)), 1)

    def test_recuperacao_explicita_preserva_corrompido_byte_a_byte(self):
        self.seed_backup()
        backup_before = self.backup.read_bytes()
        corrupt = b"\xffestado corrompido ficticio"
        self.path.write_bytes(corrupt)
        preserved = m.recover_state(self.cfg)
        self.assertEqual(Path(preserved).read_bytes(), corrupt)
        self.assertEqual(m.load_state(self.path), state(True))
        self.assertEqual(self.backup.read_bytes(), backup_before)
        self.assertFalse((self.folder / "eventos.csv").exists())

    def test_recuperacao_de_estado_ausente_nao_inventa_corrompido(self):
        self.seed_backup()
        self.path.unlink()
        self.assertIsNone(m.recover_state(self.cfg))
        self.assertEqual(m.load_state(self.path), state(True))
        self.assertEqual(list(self.folder.glob("estado.json.corrompido_*")), [])

    def test_recuperacao_recusa_estado_atual_valido(self):
        self.seed_backup()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(m.PersistenceError, "Estado atual válido"):
            m.recover_state(self.cfg)
        self.assertEqual(self.path.read_bytes(), before)

    def test_recuperacao_recusa_backup_ausente_ou_invalido(self):
        self.path.write_bytes(b"estado ruim ficticio")
        with self.assertRaises(m.PersistenceError):
            m.recover_state(self.cfg)
        self.backup.write_text("[]", encoding="utf-8")
        with self.assertRaises(m.PersistenceError):
            m.recover_state(self.cfg)
        self.assertEqual(self.path.read_bytes(), b"estado ruim ficticio")
        self.assertEqual(list(self.folder.glob("estado.json.corrompido_*")), [])

    def test_falha_na_recuperacao_preserva_estado_e_evidencia(self):
        self.seed_backup()
        original = b"estado ruim ficticio"
        self.path.write_bytes(original)
        real_replace = m.os.replace
        def replace(source, target):
            if Path(target) == self.path:
                raise OSError("erro ficticio")
            return real_replace(source, target)
        with patch.object(m.os, "replace", side_effect=replace):
            with self.assertRaises(m.PersistenceError):
                m.recover_state(self.cfg)
        self.assertEqual(self.path.read_bytes(), original)
        copies = list(self.folder.glob("estado.json.corrompido_*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), original)

    def test_estado_corrompido_interrompe_antes_da_api_e_csv(self):
        m.poll_once(self.cfg, [CLIENT], now=dt.datetime(2026, 10, 23, 10, 0))
        before = (self.folder / "eventos.csv").read_bytes()
        self.path.write_text("null", encoding="utf-8")
        with patch.object(m, "unifi_get_clients") as collect:
            with self.assertRaises(m.PersistenceError):
                m.run_poll(self.cfg)
        collect.assert_not_called()
        self.assertEqual((self.folder / "eventos.csv").read_bytes(), before)

    def test_registro_alertas_corrompido_interrompe_antes_da_api(self):
        registry = self.folder / "alertas_emitidos.json"
        for content in ("{", "null", '"texto"', "[1]", '[""]'):
            with self.subTest(content=content):
                registry.write_text(content, encoding="utf-8")
                with patch.object(m, "unifi_get_clients") as collect:
                    with self.assertRaises(m.PersistenceError):
                        m.run_poll(self.cfg)
                collect.assert_not_called()
                self.assertEqual(registry.read_text(), content)

    def test_falha_ao_salvar_alertas_preserva_registro_anterior(self):
        alerts = [(MAC, "10", 4)]
        m.pending_alerts(self.cfg, alerts, "2026-10-23")
        path = self.folder / "alertas_emitidos.json"
        before = path.read_bytes()
        with patch.object(m.os, "replace", side_effect=OSError("erro ficticio")):
            with self.assertRaises(m.PersistenceError):
                m.pending_alerts(self.cfg, [(MAC, "11", 4)], "2026-10-23")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(m.pending_alerts(self.cfg, alerts, "2026-10-23"), [])

    def test_falha_ao_salvar_relatorio_preserva_arquivo_anterior(self):
        path = self.folder / "relatorio_2026-10-23.md"
        path.write_text("relatorio anterior ficticio\n", encoding="utf-8")
        with patch.object(m.os, "replace", side_effect=OSError("erro ficticio")):
            with self.assertRaises(m.PersistenceError):
                m.write_report(self.cfg, "2026-10-23")
        self.assertEqual(path.read_text(), "relatorio anterior ficticio\n")

    def test_erro_de_persistencia_para_loop_com_status_quatro_sem_retentar(self):
        with patch.object(m, "load_config", return_value=self.cfg), \
                patch.object(m, "run_poll", side_effect=m.PersistenceError("erro ficticio")) as poll, \
                patch.object(m.time, "sleep") as sleep, \
                patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(m.main(["monitor", "loop"]), 4)
        poll.assert_called_once()
        sleep.assert_not_called()
        self.assertIn("Falha de persistência", output.getvalue())
        with m.data_directory_lock(self.cfg):
            pass

    def test_cli_recupera_somente_estado_sem_consultar_api(self):
        self.seed_backup()
        self.path.write_text("null", encoding="utf-8")
        with patch.object(m, "load_config", return_value=self.cfg), \
                patch.object(m, "unifi_get_clients") as collect, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(m.main(["monitor", "recover-state"]), 0)
        collect.assert_not_called()
        self.assertIn("Reconcilie CSV", output.getvalue())
        self.assertEqual(m.load_state(self.path), state(True))

    def test_cli_recuperacao_sem_backup_retorna_quatro(self):
        with patch.object(m, "load_config", return_value=self.cfg), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(m.main(["monitor", "recover-state"]), 4)
        self.assertFalse(self.path.exists())

    def test_falha_de_estado_apos_csv_para_sem_alegar_transacao_conjunta(self):
        m.poll_once(self.cfg, [CLIENT], now=dt.datetime(2026, 10, 23, 10, 0))
        before = self.path.read_bytes()
        real_replace = m.os.replace
        def replace(source, target):
            if Path(target) == self.path:
                raise OSError("erro ficticio")
            return real_replace(source, target)
        with patch.object(m.os, "replace", side_effect=replace):
            with self.assertRaises(m.PersistenceError):
                m.poll_once(self.cfg, [], now=dt.datetime(2026, 10, 23, 10, 1))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([e["evento"] for e in m.read_events(self.cfg)], ["primeira_vez", "queda"])
        self.assertTrue(m.load_state(self.path)[MAC]["online"])

    def test_estado_invalido_de_nova_amostra_nao_acrescenta_evento(self):
        m.poll_once(self.cfg, [CLIENT], now=dt.datetime(2026, 10, 23, 10, 0))
        before = (self.folder / "eventos.csv").read_bytes()
        invalid = copy.deepcopy(CLIENT)
        invalid["name"] = 123
        with self.assertRaises(m.PersistenceError):
            m.poll_once(self.cfg, [invalid], now=dt.datetime(2026, 10, 23, 10, 1))
        self.assertEqual((self.folder / "eventos.csv").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
