"""Garante que a demonstração sem controladora continua rodando."""

import contextlib
import io
import os
import runpy
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class DemonstracaoTest(unittest.TestCase):
    def test_demonstracao_gera_eventos_e_alerta(self):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            runpy.run_path(os.path.join(HERE, "exemplos", "demonstracao.py"), run_name="__main__")
        texto = saida.getvalue()
        self.assertIn("- Quedas no dia: **5**", texto)
        self.assertIn("b4:29:3d:00:00:01 às 22h: 4 quedas", texto)


if __name__ == "__main__":
    unittest.main()
