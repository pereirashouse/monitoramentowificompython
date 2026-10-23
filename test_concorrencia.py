"""Bloqueio local: processos reais separados, threads e backend Windows simulado."""

import errno
import io
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import coletores_wifi_monitor as m


def _hold_lock(connection, folder):
    try:
        with m.data_directory_lock({"state_dir": folder}):
            connection.send("ready")
            connection.recv()
    except BaseException as exc:
        try:
            connection.send((type(exc).__name__, str(exc)))
        except (EOFError, BrokenPipeError):
            pass
    finally:
        connection.close()


class LocalLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.cfg = {"state_dir": str(self.folder), "ssid": "wms", "alert_drops_per_hour": 3,
                    "collector_macs": {}, "ap_names": {}, "poll_seconds": 60}

    def holder(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_hold_lock, args=(child, str(self.folder)))
        process.start()
        child.close()
        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(5)
            parent.close()
        self.addCleanup(cleanup)
        self.assertTrue(parent.poll(10), "Processo não informou aquisição em 10s.")
        self.assertEqual(parent.recv(), "ready")
        return process, parent

    def test_cria_arquivo_persistente_e_permite_reabrir_depois(self):
        with m.data_directory_lock(self.cfg):
            self.assertTrue((self.folder / ".monitor.lock").is_file())
        self.assertTrue((self.folder / ".monitor.lock").is_file())
        with m.data_directory_lock(self.cfg):
            pass

    def test_reentrada_na_mesma_thread_preserva_lock_externo(self):
        with m.data_directory_lock(self.cfg):
            with m.data_directory_lock(self.cfg):
                self.assertEqual(m.read_events(self.cfg), [])
            errors = []
            def competitor():
                try:
                    with m.data_directory_lock(self.cfg):
                        errors.append("entrou indevidamente")
                except m.DataDirectoryBusy:
                    errors.append("ocupado")
            thread = threading.Thread(target=competitor)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["ocupado"])

    def test_pastas_diferentes_podem_ser_usadas_ao_mesmo_tempo(self):
        other = dict(self.cfg, state_dir=str(self.folder / "outra"))
        with m.data_directory_lock(self.cfg), m.data_directory_lock(other):
            self.assertTrue((self.folder / "outra" / ".monitor.lock").exists())

    def test_caminhos_equivalentes_reentram_no_mesmo_lock(self):
        other = dict(self.cfg, state_dir=str(self.folder / "."))
        with m.data_directory_lock(self.cfg), m.data_directory_lock(other):
            self.assertEqual(m.read_events(other), [])

    @unittest.skipUnless(os.name == "posix", "Symlink real verificado em POSIX.")
    def test_alias_simbolico_identifica_a_mesma_pasta(self):
        alias = self.folder / "alias"
        alias.symlink_to(self.folder, target_is_directory=True)
        with m.data_directory_lock(self.cfg), m.data_directory_lock(dict(self.cfg, state_dir=str(alias))):
            self.assertEqual(m.read_events(self.cfg), [])

    def test_excecao_e_ctrl_c_liberam_bloqueio(self):
        for error in (ValueError("erro ficticio"), KeyboardInterrupt()):
            with self.subTest(error=error):
                with self.assertRaises(type(error)):
                    with m.data_directory_lock(self.cfg):
                        raise error
                with m.data_directory_lock(self.cfg):
                    pass

    def test_erro_de_permissao_ao_abrir_nao_e_tratado_como_busy(self):
        with patch("builtins.open", side_effect=PermissionError("permissao ficticia")):
            with self.assertRaises(PermissionError):
                with m.data_directory_lock(self.cfg):
                    pass

    def test_backend_nao_suportado_falha_sem_continuar_sem_lock(self):
        with (self.folder / "teste.lock").open("a+b") as file:
            with patch.object(m.os, "name", "outro"):
                with self.assertRaisesRegex(RuntimeError, "POSIX ou Windows"):
                    m._acquire_file_lock(file)

    def test_erro_inesperado_no_backend_e_propagado(self):
        with patch.object(m, "_acquire_file_lock", side_effect=OSError(errno.EIO, "erro ficticio")):
            with self.assertRaises(OSError) as caught:
                with m.data_directory_lock(self.cfg):
                    pass
        self.assertEqual(caught.exception.errno, errno.EIO)
        with m.data_directory_lock(self.cfg):
            pass

    def test_backend_windows_simulado_bloqueia_primeiro_byte(self):
        backend = SimpleNamespace(locking=Mock(), LK_NBLCK=1, LK_UNLCK=2)
        with (self.folder / "windows.lock").open("a+b") as file:
            with patch.object(m.os, "name", "nt"), patch.dict("sys.modules", {"msvcrt": backend}):
                m._acquire_file_lock(file)
                self.assertEqual(file.tell(), 0)
                backend.locking.assert_called_with(file.fileno(), 1, 1)
                m._release_file_lock(file)
                backend.locking.assert_called_with(file.fileno(), 2, 1)
        self.assertEqual((self.folder / "windows.lock").read_bytes(), b"\0")

    def test_backend_windows_simulado_nao_acrescenta_bytes_em_reabertura(self):
        path = self.folder / "windows.lock"
        path.write_bytes(b"\0")
        backend = SimpleNamespace(locking=Mock(), LK_NBLCK=1, LK_UNLCK=2)
        with path.open("a+b") as file:
            with patch.object(m.os, "name", "nt"), patch.dict("sys.modules", {"msvcrt": backend}):
                m._acquire_file_lock(file)
                m._release_file_lock(file)
        self.assertEqual(path.read_bytes(), b"\0")

    def test_processo_separado_impede_segunda_aquisicao(self):
        process, connection = self.holder()
        with self.assertRaises(m.DataDirectoryBusy):
            with m.data_directory_lock(self.cfg):
                pass
        connection.send("release")
        process.join(5)
        self.assertFalse(process.is_alive())
        with m.data_directory_lock(self.cfg):
            pass

    def test_termino_abrupto_libera_lock_sem_apagar_arquivo(self):
        process, _ = self.holder()
        process.terminate()
        process.join(5)
        self.assertFalse(process.is_alive())
        self.assertTrue((self.folder / ".monitor.lock").exists())
        with m.data_directory_lock(self.cfg):
            pass

    def test_run_poll_ocupado_nao_consulta_api_nem_grava_dados(self):
        self.holder()
        before = {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}
        with patch.object(m, "unifi_get_clients") as collect:
            with self.assertRaises(m.DataDirectoryBusy):
                m.run_poll(self.cfg)
        collect.assert_not_called()
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}, before)

    def test_operacoes_de_dados_ocupadas_falham_antes_de_gravar(self):
        self.holder()
        operations = (
            lambda: m.poll_once(self.cfg, []),
            lambda: m.read_events(self.cfg),
            lambda: m.build_report(self.cfg, "2026-10-23"),
            lambda: m.write_report(self.cfg, "2026-10-23"),
            lambda: m.pending_alerts(self.cfg, [], "2026-10-23"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(m.DataDirectoryBusy):
                    operation()
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [".monitor.lock"])

    def test_cli_busy_retorna_status_tres_sem_poll_ou_report(self):
        self.holder()
        for command in ("poll", "loop", "report"):
            with self.subTest(command=command), \
                    patch.object(m, "load_config", return_value=self.cfg), \
                    patch.object(m, "run_poll") as poll, \
                    patch.object(m, "write_report") as report, \
                    patch("sys.stderr", new_callable=io.StringIO) as output:
                self.assertEqual(m.main(["monitor", command]), 3)
                self.assertIn("Pasta de dados ocupada", output.getvalue())
                poll.assert_not_called()
                report.assert_not_called()

    def test_loop_mantem_bloqueio_durante_pausa_e_libera_no_ctrl_c(self):
        results = []
        def pause(seconds):
            def competitor():
                try:
                    with m.data_directory_lock(self.cfg):
                        results.append("entrou indevidamente")
                except m.DataDirectoryBusy:
                    results.append("ocupado")
            thread = threading.Thread(target=competitor)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            raise KeyboardInterrupt
        with patch.object(m, "load_config", return_value=self.cfg), \
                patch.object(m, "run_poll", return_value=([], [])), \
                patch.object(m.time, "sleep", side_effect=pause), \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(KeyboardInterrupt):
                m.main(["monitor", "loop"])
        self.assertEqual(results, ["ocupado"])
        with m.data_directory_lock(self.cfg):
            pass

    def test_cli_poll_e_report_reentram_sem_deadlock(self):
        with patch.object(m, "load_config", return_value=self.cfg), \
                patch.object(m, "unifi_get_clients", return_value=[]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(m.main(["monitor", "poll"]), 0)
            self.assertEqual(m.main(["monitor", "report", "2026-10-23"]), 0)
        self.assertTrue((self.folder / "relatorio_2026-10-23.md").is_file())
        with m.data_directory_lock(self.cfg):
            pass

    def test_comando_desconhecido_nao_cria_pasta_de_dados(self):
        cfg = dict(self.cfg, state_dir=str(self.folder / "novo"))
        with patch.object(m, "load_config", return_value=cfg), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(m.main(["monitor", "desconhecido"]), 2)
        self.assertFalse((self.folder / "novo").exists())


if __name__ == "__main__":
    unittest.main()
