"""
Demonstração sem controladora: simula um turno de coletas com dados fictícios.

Uso (a partir da raiz do repositório):
  python exemplos/demonstracao.py

Gera eventos.csv e o relatório do dia em uma pasta temporária e imprime os dois.
Nenhum dado é real: MACs usam o OUI do fabricante com final fictício e as APs têm
nomes genéricos.
"""

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coletores_wifi_monitor as m  # noqa: E402

DIA = dt.date(2026, 10, 20)

URO = {"mac": "b4:29:3d:00:00:01", "name": "", "ap": "Expedição - 1",
       "rssi_dbm": -71, "snr": 24, "band": "ng"}
SKO = {"mac": "04:7b:cb:00:00:02", "name": "SkorpioX5", "ap": "Expedição - 5",
       "rssi_dbm": -52, "snr": 44, "band": "na"}
AND = {"mac": "9e:0d:5e:00:00:03", "name": "android-a1b2", "ap": "Administração - 1",
       "rssi_dbm": -49, "snr": 47, "band": "na"}

# (hora, minuto, clientes presentes naquele ciclo de coleta)
CICLOS = [
    (22, 0, [URO, SKO, AND]),
    (22, 1, [SKO, AND]),        # Urovo some: queda
    (22, 2, [URO, SKO, AND]),   # volta: reconexão (60 s)
    (22, 10, [SKO, AND]),
    (22, 11, [URO, SKO, AND]),
    (22, 20, [SKO, AND]),
    (22, 21, [URO, SKO, AND]),
    (22, 30, [SKO]),            # Urovo e Android caem juntos
    (22, 31, [URO, SKO, AND]),
]


def main():
    tmp = tempfile.mkdtemp(prefix="demo-monitor-")
    cfg = {"state_dir": tmp, "ssid": "wms", "alert_drops_per_hour": 3,
           "collector_macs": {URO["mac"]: "Urovo DT40"}}
    m.CONFIG_COLLECTORS = cfg["collector_macs"]

    for hora, minuto, clientes in CICLOS:
        agora = dt.datetime.combine(DIA, dt.time(hora, minuto))
        for ev in m.poll_once(cfg, clientes, now=agora):
            dur = f" ({ev['duracao_s']} s)" if ev["duracao_s"] != "" else ""
            print(f"{ev['timestamp']}  {ev['evento']:<12} {ev['mac']}  "
                  f"{ev['tipo']:<26} {ev['ap']:<18} {ev['rssi_dbm']} dBm{dur}")

    print()
    caminho = m.write_report(cfg, DIA.isoformat())
    with open(caminho, encoding="utf-8") as f:
        print(f.read())
    print(f"Arquivos gerados em: {tmp}")


if __name__ == "__main__":
    main()
