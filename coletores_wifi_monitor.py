#!/usr/bin/env python3
"""
Monitor de quedas Wi-Fi para coletores de dados (WMS) via API UniFi.

Detecta em tempo quase real quando coletores do SSID configurado somem e voltam,
registra duração, AP e RSSI em CSV e gera relatório diário em Markdown.

Uso:
  python coletores_wifi_monitor.py poll          # uma coleta
  python coletores_wifi_monitor.py loop          # loop contínuo (Ctrl+C para parar)
  python coletores_wifi_monitor.py report        # relatório de hoje
  python coletores_wifi_monitor.py report 2024-03-15   # relatório de outra data

Configuração: copie config.example.json para config.json e preencha.
Autenticação: defina a variável de ambiente UNIFI_API_KEY com a chave de API
  (somente leitura) gerada na aplicação Network do UniFi.
  Windows: setx UNIFI_API_KEY "sua-chave"   (reabra o terminal depois)
  Linux/macOS: export UNIFI_API_KEY="sua-chave"

Requisitos: Python 3.10+, biblioteca padrão apenas.
"""

import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

CONFIG_FILE = "config.json"
DATA_DIR = Path("dados")

# Prefixos OUI públicos para classificação automática
_OUI = {
    "b4:29:3d": "Urovo",
    "04:7b:cb": "Skorpio",
    "3c:e1:a1": "Skorpio",
}

_CSV_FIELDS = ["timestamp", "mac", "tipo", "evento", "ap", "rssi", "duracao_s"]


def load_config(path: str = CONFIG_FILE) -> dict:
    with open(path) as f:
        return json.load(f)


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class CollectorMonitor:
    """Monitora conexões Wi-Fi de coletores via API local do UniFi."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.api_url = config["controller_url"].rstrip("/") + config["clients_path"]
        self.api_key = os.environ["UNIFI_API_KEY"]
        self.ssid = config["ssid"]
        self.collector_macs: dict[str, str] = {
            k.lower(): v for k, v in config.get("collector_macs", {}).items()
        }
        self.alert_threshold: int = config.get("alert_drops_per_hour", 3)
        DATA_DIR.mkdir(exist_ok=True)
        self._state_file = DATA_DIR / "estado.json"
        self._events_file = DATA_DIR / "eventos.csv"
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self._state_file.exists():
            with open(self._state_file) as f:
                return json.load(f)
        return {"online": {}, "last_seen": {}}

    def _save_state(self) -> None:
        with open(self._state_file, "w") as f:
            json.dump(self._state, f, indent=2)

    def _fetch_clients(self) -> list[dict]:
        req = urllib.request.Request(
            self.api_url,
            headers={"X-API-KEY": self.api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=10) as resp:
            raw = json.load(resp)
        # Legacy API: {"data": [...], "meta": {"rc": "ok"}}
        clients = raw.get("data", raw) if isinstance(raw, dict) else raw
        return [c for c in clients if c.get("essid", "").strip() == self.ssid]

    def _classify(self, mac: str, hostname: str) -> str:
        mac = mac.lower()
        if mac in self.collector_macs:
            return self.collector_macs[mac]
        for prefix, name in _OUI.items():
            if mac.startswith(prefix):
                return name
        if hostname and hostname.startswith("android-"):
            return "Android (MAC aleatório)"
        return "Desconhecido"

    def _append_event(self, event: dict) -> None:
        write_header = not self._events_file.exists()
        with open(self._events_file, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            if write_header:
                w.writeheader()
            w.writerow(event)

    def poll(self) -> list[dict]:
        """Faz uma coleta e retorna os eventos gerados."""
        now = datetime.now()
        ts = now.isoformat(timespec="seconds")
        try:
            clients = self._fetch_clients()
        except Exception as exc:
            print(f"[{ts}] Falha na coleta: {exc}", file=sys.stderr)
            return []

        current = {c["mac"].lower(): c for c in clients}
        events: list[dict] = []

        for mac, info in list(self._state["online"].items()):
            if mac not in current:
                tipo = self._classify(mac, info.get("hostname", ""))
                ev = {
                    "timestamp": ts, "mac": mac, "tipo": tipo,
                    "evento": "queda", "ap": info.get("ap", ""),
                    "rssi": info.get("rssi", ""), "duracao_s": "",
                }
                self._append_event(ev)
                events.append(ev)
                self._state["last_seen"][mac] = {
                    "ts": ts, "ap": info.get("ap", ""), "rssi": info.get("rssi", ""), "tipo": tipo,
                }
                del self._state["online"][mac]

        for mac, c in current.items():
            if mac not in self._state["online"]:
                tipo = self._classify(mac, c.get("hostname", ""))
                ap = c.get("ap_mac", "")
                rssi = c.get("rssi", "")
                ev_type = "primeira_vez" if mac not in self._state["last_seen"] else "reconexao"
                duracao = ""
                if ev_type == "reconexao":
                    try:
                        t0 = datetime.fromisoformat(self._state["last_seen"][mac]["ts"])
                        duracao = str(int((now - t0).total_seconds()))
                    except Exception:
                        pass
                ev = {
                    "timestamp": ts, "mac": mac, "tipo": tipo,
                    "evento": ev_type, "ap": ap, "rssi": rssi, "duracao_s": duracao,
                }
                self._append_event(ev)
                events.append(ev)

            self._state["online"][mac] = {
                "ap": c.get("ap_mac", ""), "rssi": c.get("rssi", ""),
                "hostname": c.get("hostname", ""), "tipo": self._classify(mac, c.get("hostname", "")),
            }

        self._save_state()
        return events

    def loop(self, interval: int = 60) -> None:
        """Loop contínuo de coleta. Pressione Ctrl+C para parar."""
        print(f"Monitor iniciado — coleta a cada {interval}s. Ctrl+C para parar.")
        while True:
            events = self.poll()
            drops = [e for e in events if e["evento"] == "queda"]
            print(f"{datetime.now().strftime('%H:%M:%S')} — {len(events)} evento(s), {len(drops)} queda(s)")
            for e in drops:
                print(f"  ALERTA queda: {e['mac']} ({e['tipo']})  AP={e['ap']}  RSSI={e['rssi']}")
            time.sleep(interval)

    def report(self, report_date: "date | str | None" = None) -> Path:
        """Gera relatório diário em Markdown."""
        if report_date is None:
            report_date = date.today()
        elif isinstance(report_date, str):
            report_date = date.fromisoformat(report_date)

        date_str = report_date.isoformat()
        events: list[dict] = []
        if self._events_file.exists():
            with open(self._events_file, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row["timestamp"].startswith(date_str):
                        events.append(row)

        drops = [e for e in events if e["evento"] == "queda"]
        drops_by_mac = Counter(e["mac"] for e in drops)
        drops_by_tipo = Counter(e["tipo"] for e in drops)
        drops_by_hour = Counter(e["timestamp"][11:13] for e in drops)

        mac_hour_count: dict[tuple, int] = defaultdict(int)
        for e in drops:
            mac_hour_count[(e["mac"], e["timestamp"][11:13])] += 1
        alerts = [(mac, h, n) for (mac, h), n in mac_hour_count.items() if n > self.alert_threshold]

        lines = [
            f"# Relatório de quedas Wi-Fi — {date_str}",
            "",
            f"**Total de quedas:** {len(drops)}  |  **Total de eventos:** {len(events)}",
            "",
            "## Quedas por tipo",
            "",
        ]
        for tipo, n in drops_by_tipo.most_common():
            lines.append(f"- {tipo}: {n}")

        lines += ["", "## Coletores mais afetados (top 10)", ""]
        for mac, n in drops_by_mac.most_common(10):
            tipo = next((e["tipo"] for e in drops if e["mac"] == mac), "")
            lines.append(f"- `{mac}` ({tipo}): {n} queda(s)")

        lines += ["", "## Quedas por hora", ""]
        for h in sorted(drops_by_hour):
            lines.append(f"- {h}h: {drops_by_hour[h]}")

        if alerts:
            lines += ["", "## ⚠️ Alertas", ""]
            for mac, h, n in sorted(alerts, key=lambda x: -x[2]):
                lines.append(f"- `{mac}` teve **{n} quedas** na hora {h}h (limite: {self.alert_threshold})")

        report_path = DATA_DIR / f"relatorio_{date_str}.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Relatório gerado: {report_path}")
        return report_path


def main() -> None:
    try:
        config = load_config()
    except FileNotFoundError:
        print(f"Arquivo {CONFIG_FILE!r} não encontrado. Copie config.example.json e preencha.", file=sys.stderr)
        sys.exit(1)

    if "UNIFI_API_KEY" not in os.environ:
        print("Variável UNIFI_API_KEY não definida. Veja o README.", file=sys.stderr)
        sys.exit(1)

    monitor = CollectorMonitor(config)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "poll"

    if cmd == "poll":
        events = monitor.poll()
        print(f"{len(events)} evento(s)")
    elif cmd == "loop":
        monitor.loop()
    elif cmd == "report":
        d = sys.argv[2] if len(sys.argv) > 2 else None
        monitor.report(d)
    else:
        print(f"Uso: python {sys.argv[0]} [poll|loop|report [AAAA-MM-DD]]")
        sys.exit(1)


if __name__ == "__main__":
    main()
