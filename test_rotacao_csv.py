"""Rotação por tamanho, histórico preservado e relatórios em arquivos temporários."""

import csv
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import coletores_wifi_monitor as m


CLIENT = {"mac": "b4:29:3d:00:00:01", "name": "coletor ficticio", "ap": "AP ficticia",
          "rssi_dbm": -55, "snr": 40, "band": "ng"}


def event(minute=0, kind="queda", name="coletor ficticio", day="2026-10-23"):
    return dict(zip(m.EVENT_FIELDS, [f"{day}T10:{minute:02d}:00", CLIENT["mac"], "Urovo", "fabricante",
                                   name, kind, CLIENT["ap"], -55, 40, "ng", ""]))


def row_bytes(row):
    output = io.StringIO(newline="")
    csv.DictWriter(output, fieldnames=m.EVENT_FIELDS).writerow(row)
    return output.getvalue().encode("utf-8")


class RotationConfigTests(unittest.TestCase):
    def test_padrao_no_carregamento_e_exemplo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(m.load_config(str(path))["csv_max_bytes"], m.DEFAULT_CSV_MAX_BYTES)
        self.assertEqual(m.load_config(str(Path(m.HERE) / "config.example.json"))["csv_max_bytes"], m.DEFAULT_CSV_MAX_BYTES)

    def test_zero_desativa_rotacao_e_limite_pequeno_e_valido(self):
        self.assertEqual(m.validate_csv_max_bytes(0), 0)
        self.assertEqual(m.validate_csv_max_bytes(1), 1)

    def test_rejeita_tipo_ambiguo_e_valor_negativo(self):
        for value in (None, True, False, -1, 1.5, "1024", [], {}, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "csv_max_bytes"):
                    m.validate_csv_max_bytes(value)

    def test_carregamento_rejeita_limite_invalido(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"csv_max_bytes": True}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "csv_max_bytes"):
                m.load_config(str(path))


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / "eventos.csv"
        self.cfg = {"state_dir": str(self.folder), "ssid": "wms", "alert_drops_per_hour": 1,
                    "collector_macs": {}, "ap_names": {}, "csv_max_bytes": 1}
        patcher = patch.object(m, "CONFIG_COLLECTORS", {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def archives(self):
        return sorted(self.folder.glob("eventos_arquivo_*.csv"))

    def test_primeiro_evento_cria_cabecalho_sem_rotacao(self):
        m.append_event(self.path, event(), max_bytes=1)
        self.assertEqual(self.archives(), [])
        with self.path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[0], m.EVENT_FIELDS)
        self.assertEqual(len(rows), 2)

    def test_nao_rotaciona_abaixo_ou_no_limite_exato(self):
        m.append_event(self.path, event(), max_bytes=0)
        before = self.path.read_bytes()
        limit = len(before) + len(row_bytes(event(1)))
        m.append_event(self.path, event(1), max_bytes=limit)
        self.assertEqual(self.path.stat().st_size, limit)
        self.assertEqual(self.archives(), [])
        self.assertEqual(len(m.read_events(self.cfg)), 2)

    def test_rotacao_preserva_arquivo_antigo_byte_a_byte(self):
        m.append_event(self.path, event(), max_bytes=0)
        before = self.path.read_bytes()
        limit = len(before) + len(row_bytes(event(1))) - 1
        m.append_event(self.path, event(1), max_bytes=limit)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(self.archives()[0].read_bytes(), before)
        with self.path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], event(1)["timestamp"])
        self.assertEqual([e["timestamp"] for e in m.read_events(self.cfg)], [event()["timestamp"], event(1)["timestamp"]])

    def test_tamanho_considera_bytes_utf8_e_nao_numero_de_caracteres(self):
        m.append_event(self.path, event(), max_bytes=0)
        row = event(1, name="ação, estação ç")
        before = self.path.read_bytes()
        utf8 = row_bytes(row)
        limit = len(before) + len(utf8.decode("utf-8"))
        self.assertGreater(len(utf8), len(utf8.decode("utf-8")))
        m.append_event(self.path, row, max_bytes=limit)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(m.read_events(self.cfg)[1]["nome"], row["nome"])

    def test_limite_zero_mantem_arquivo_ativo_sem_rotacao(self):
        for minute in range(4):
            m.append_event(self.path, event(minute), max_bytes=0)
        self.assertEqual(self.archives(), [])
        self.assertEqual(len(m.read_events(self.cfg)), 4)

    def test_arquivo_vazio_recebe_cabecalho_sem_arquivo_legado(self):
        self.path.touch()
        m.append_event(self.path, event(), max_bytes=1)
        self.assertEqual(len(m.read_events(self.cfg)), 1)
        self.assertEqual(list(self.folder.glob("eventos_legado_*.csv")), [])
        self.assertEqual(self.archives(), [])

    def test_cabecalho_sozinho_nao_produz_arquivo_rotacionado_vazio(self):
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            csv.DictWriter(stream, fieldnames=m.EVENT_FIELDS).writeheader()
        m.append_event(self.path, event(), max_bytes=1)
        self.assertEqual(self.archives(), [])
        self.assertEqual(len(m.read_events(self.cfg)), 1)

    def test_evento_maior_que_limite_e_preservado_inteiro(self):
        row = event(name="ficticio " * 100)
        m.append_event(self.path, row, max_bytes=1)
        self.assertGreater(self.path.stat().st_size, 1)
        self.assertEqual(m.read_events(self.cfg)[0]["nome"], row["nome"])
        m.append_event(self.path, event(1), max_bytes=1)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(len(m.read_events(self.cfg)), 2)

    def test_colisao_de_nome_nao_sobrescreve_arquivo_existente(self):
        m.append_event(self.path, event(), max_bytes=0)
        before = self.path.read_bytes()
        now = dt.datetime(2026, 10, 23, 10, 0)
        stamp = now.strftime("%Y%m%d%H%M%S%f")
        occupied = self.folder / f"eventos_arquivo_{stamp}_colisao.csv"
        occupied.write_bytes(b"arquivo existente ficticio\n")
        with patch.object(m.dt, "datetime") as clock, \
                patch.object(m.uuid, "uuid4", side_effect=[SimpleNamespace(hex="colisao"), SimpleNamespace(hex="novo")]):
            clock.now.return_value = now
            m.append_event(self.path, event(1), max_bytes=1)
        self.assertEqual(occupied.read_bytes(), b"arquivo existente ficticio\n")
        self.assertEqual((self.folder / f"eventos_arquivo_{stamp}_novo.csv").read_bytes(), before)

    def test_formato_legado_e_preservado_mas_nao_lido_como_atual(self):
        legacy = b"timestamp,mac,tipo,evento,ap,rssi_ultimo,duracao_s\r\nlinha-ficticia\r\n"
        self.path.write_bytes(legacy)
        m.append_event(self.path, event(), max_bytes=1)
        files = list(self.folder.glob("eventos_legado_*.csv"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), legacy)
        self.assertEqual(self.archives(), [])
        self.assertEqual(len(m.read_events(self.cfg)), 1)

    def test_varias_migracoes_legadas_nao_substituem_historico(self):
        snapshots = []
        for index in range(2):
            legacy = f"formato_antigo\nvalor_ficticio_{index}\n".encode()
            self.path.write_bytes(legacy)
            m.append_event(self.path, event(index), max_bytes=0)
            snapshots.append(legacy)
        files = list(self.folder.glob("eventos_legado_*.csv"))
        self.assertEqual(len(files), 2)
        self.assertEqual({p.read_bytes() for p in files}, set(snapshots))

    def test_evento_invalido_falha_antes_de_mover_arquivo(self):
        m.append_event(self.path, event(), max_bytes=0)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            m.append_event(self.path, dict(event(1), campo_extra="ficticio"), max_bytes=1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.archives(), [])

    def test_erro_ao_mover_preserva_csv_original(self):
        m.append_event(self.path, event(), max_bytes=0)
        before = self.path.read_bytes()
        with patch.object(m.os, "rename", side_effect=PermissionError("erro ficticio")):
            with self.assertRaises(PermissionError):
                m.append_event(self.path, event(1), max_bytes=1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.archives(), [])

    def test_varias_rotacoes_nao_perdem_nem_duplicam_eventos(self):
        expected = [event(i) for i in range(5)]
        for row in expected:
            m.append_event(self.path, row, max_bytes=1)
        self.assertEqual(len(self.archives()), 4)
        rows = m.read_events(self.cfg)
        self.assertEqual([r["timestamp"] for r in rows], [e["timestamp"] for e in expected])
        self.assertEqual(len(rows), 5)

    def test_leitura_funciona_so_com_arquivos_rotacionados(self):
        m.append_event(self.path, event(), max_bytes=0)
        m.archive_event_file(str(self.path), "arquivo")
        self.assertFalse(self.path.exists())
        self.assertEqual(len(m.read_events(self.cfg)), 1)

    def test_leitura_ordena_por_timestamp_independente_do_nome_do_arquivo(self):
        m.append_event(self.path, event(5), max_bytes=1)
        m.append_event(self.path, event(1), max_bytes=1)
        m.append_event(self.path, event(3), max_bytes=1)
        self.assertEqual([r["timestamp"] for r in m.read_events(self.cfg)], [event(i)["timestamp"] for i in (1, 3, 5)])

    def test_leitura_ignora_legados_e_csvs_de_outros_usos(self):
        (self.folder / "eventos_legado_ficticio.csv").write_text("arquivo antigo\n", encoding="utf-8")
        (self.folder / "outro.csv").write_text("outro uso\n", encoding="utf-8")
        self.assertEqual(m.read_events(self.cfg), [])

    def test_leitura_falha_visivelmente_com_cabecalho_de_arquivo_incompativel(self):
        (self.folder / "eventos_arquivo_invalido.csv").write_text("cabecalho_errado\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Cabeçalho CSV incompatível"):
            m.build_report(self.cfg, "2026-10-23")

    def test_arquivo_rotacionado_vazio_falha_em_vez_de_ocultar_historico(self):
        (self.folder / "eventos_arquivo_vazio.csv").touch()
        with self.assertRaisesRegex(ValueError, "Cabeçalho CSV incompatível: eventos_arquivo_vazio.csv"):
            m.build_report(self.cfg, "2026-10-23")

    def test_arquivo_ativo_vazio_e_aceito_como_inicial(self):
        self.path.touch()
        self.assertEqual(m.read_events(self.cfg), [])
        self.assertIn("Quedas no dia: **0**", m.build_report(self.cfg, "2026-10-23"))

    def test_linha_incompleta_ou_extra_nao_e_contada_silenciosamente(self):
        for content in ("linha,incompleta\r\n", row_bytes(event()).decode().rstrip("\r\n") + ",extra\r\n"):
            with self.subTest(content=content):
                with self.path.open("w", newline="", encoding="utf-8") as stream:
                    csv.DictWriter(stream, fieldnames=m.EVENT_FIELDS).writeheader()
                    stream.write(content)
                with self.assertRaisesRegex(ValueError, "Linha CSV"):
                    m.read_events(self.cfg)

    def test_pasta_com_colchetes_funciona_na_busca_de_arquivos(self):
        nested = self.folder / "dados[teste]"
        nested.mkdir()
        path = nested / "eventos.csv"
        for minute in (0, 1):
            m.append_event(path, event(minute), max_bytes=1)
        cfg = dict(self.cfg, state_dir=str(nested))
        self.assertEqual(len(m.read_events(cfg)), 2)

    def test_nomes_com_virgula_aspas_e_quebra_de_linha_sao_preservados(self):
        name = 'ação, "ficticia"\nsegunda linha'
        m.append_event(self.path, event(name=name), max_bytes=1)
        m.append_event(self.path, event(1), max_bytes=1)
        self.assertEqual(m.read_events(self.cfg)[0]["nome"], name)

    def test_poll_aplica_limite_e_preserva_estado_de_reconexao(self):
        start = dt.datetime(2026, 10, 23, 10, 0)
        m.poll_once(self.cfg, [CLIENT], now=start)
        m.poll_once(self.cfg, [], now=start + dt.timedelta(minutes=1))
        events = m.poll_once(self.cfg, [CLIENT], now=start + dt.timedelta(minutes=2))
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual(events[0]["duracao_s"], 60)
        self.assertEqual([r["evento"] for r in m.read_events(self.cfg)], ["primeira_vez", "queda", "reconexao"])
        self.assertTrue(m.load_state(str(self.folder / "estado.json"))[CLIENT["mac"]]["online"])

    def test_poll_rejeita_limite_invalido_antes_de_gravar(self):
        cfg = dict(self.cfg, state_dir=str(self.folder / "novo"), csv_max_bytes=False)
        with self.assertRaises(ValueError):
            m.poll_once(cfg, [CLIENT])
        self.assertFalse((self.folder / "novo").exists())

    def test_relatorio_e_alertas_iguais_antes_e_depois_de_rotacionar(self):
        for minute, kind, day in ((0, "queda", "2026-10-23"), (1, "reconexao", "2026-10-23"),
                                  (2, "queda", "2026-10-23"), (0, "queda", "2026-10-22")):
            m.append_event(self.path, event(minute, kind, day=day), max_bytes=0)
        before = m.build_report(self.cfg, "2026-10-23")
        alerts = m.alerts_for(self.cfg, m.read_events(self.cfg), "2026-10-23")
        m.archive_event_file(str(self.path), "arquivo")
        after = m.build_report(self.cfg, "2026-10-23")
        self.assertEqual(after, before)
        self.assertIn("Quedas no dia: **2**", after)
        self.assertEqual(m.alerts_for(self.cfg, m.read_events(self.cfg), "2026-10-23"), alerts)
        self.assertEqual(alerts, [(CLIENT["mac"], "10", 2)])
        m.append_event(self.path, event(3), max_bytes=1)
        self.assertIn("Quedas no dia: **3**", m.build_report(self.cfg, "2026-10-23"))
        self.assertIn("Quedas no dia: **1**", m.build_report(self.cfg, "2026-10-22"))

    def test_alerta_ja_emitido_nao_repete_depois_da_rotacao(self):
        for minute in (0, 1):
            m.append_event(self.path, event(minute), max_bytes=0)
        day = "2026-10-23"
        alerts = m.alerts_for(self.cfg, m.read_events(self.cfg), day)
        self.assertEqual(m.pending_alerts(self.cfg, alerts, day), alerts)
        m.archive_event_file(str(self.path), "arquivo")
        alerts = m.alerts_for(self.cfg, m.read_events(self.cfg), day)
        self.assertEqual(m.pending_alerts(self.cfg, alerts, day), [])


if __name__ == "__main__":
    unittest.main()
