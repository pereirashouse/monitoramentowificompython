"""
Monitor de quedas Wi-Fi dos coletores (SSID configurado).

Somente leitura na controladora UniFi. Usa apenas a biblioteca padrão do Python.

Uso:
  python coletores_wifi_monitor.py poll     -> coleta uma vez e registra eventos
  python coletores_wifi_monitor.py report   -> gera o relatório do dia (Markdown)
  python coletores_wifi_monitor.py loop     -> poll a cada N segundos (config)

Configuração: arquivo config.json ao lado do script (ver config.example.json).
A chave da API é lida da variável de ambiente UNIFI_API_KEY (nunca do arquivo).
"""

import csv
import datetime as dt
import json
import os
import random
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config.json")

# Prefixos de fabricante usados para classificar os coletores
PREFIX_URUVO = ("b4:29:3d",)
PREFIX_SKORPIO = ("04:7b:cb", "3c:e1:a1")


def load_config(path=DEFAULT_CONFIG):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("ssid", "wms")
    cfg.setdefault("poll_seconds", 60)
    cfg.setdefault("alert_drops_per_hour", 3)
    cfg.setdefault("verify_tls", True)
    validate_tls_options(cfg)
    cfg.setdefault("retry_max_attempts", 3)
    cfg.setdefault("retry_backoff_seconds", 2)
    cfg.setdefault("retry_max_delay_seconds", 30)
    validate_retry_options(cfg)
    cfg.setdefault("state_dir", os.path.join(HERE, "dados"))
    cfg["collector_macs"] = {m.lower(): n for m, n in cfg.get("collector_macs", {}).items()}
    # Opcional: nome amigável das APs, pelo MAC (a API não envia o nome da AP).
    cfg["ap_names"] = {m.lower(): n for m, n in cfg.get("ap_names", {}).items()}
    return cfg


def is_random_mac(mac):
    """Bit 'locally administered' ligado = MAC aleatório (ex.: Android)."""
    try:
        first = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0x02)


def classify(mac, name=""):
    """Retorna (tipo, origem da identificação)."""
    mac = mac.lower()
    if mac in CONFIG_COLLECTORS:
        return CONFIG_COLLECTORS[mac], "inventario"
    if mac.startswith(PREFIX_URUVO):
        return "Urovo", "fabricante"
    if mac.startswith(PREFIX_SKORPIO):
        return "Skorpio X5", "fabricante"
    if is_random_mac(mac) and name.lower().startswith("android"):
        return "Android (MAC aleatório)", "nome"
    return "Desconhecido", "nenhuma"


CONFIG_COLLECTORS = {}


# ---------------------------------------------------------------- UniFi (leitura)

def validate_tls_options(cfg):
    """Rejeita opções ambíguas antes de aplicar a política TLS."""
    verify_tls = cfg.get("verify_tls", True)
    if not isinstance(verify_tls, bool):
        raise ValueError("verify_tls deve ser um booleano JSON: true ou false.")
    ca_file = cfg.get("ca_file")
    if ca_file is not None and not isinstance(ca_file, str):
        raise ValueError("ca_file deve ser null ou um caminho PEM em texto.")
    return verify_tls, ca_file or None


def create_tls_context(cfg):
    """Valida certificado e hostname por padrão, sem fallback inseguro."""
    verify_tls, ca_file = validate_tls_options(cfg)
    if verify_tls:
        return ssl.create_default_context(cafile=ca_file)

    warnings.warn(
        "TLS sem validação de certificado e hostname (verify_tls=false). "
        "Risco de interceptação da chave de API e dos dados; use somente "
        "temporariamente em laboratório isolado, nunca em produção.",
        RuntimeWarning,
        stacklevel=2,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class UnifiResponseError(ValueError):
    """Resposta da API inválida; não deve ser tratada como lista vazia."""


def parse_unifi_clients(data):
    """Valida toda a amostra e retorna os registros Wi-Fi, sem gravar estado."""
    if isinstance(data, dict):
        if data.get("error"):
            raise UnifiResponseError("A API UniFi retornou um erro explícito.")
        if "meta" in data:
            meta = data["meta"]
            if not isinstance(meta, dict) or meta.get("rc") != "ok":
                raise UnifiResponseError("A API UniFi não confirmou sucesso em meta.rc.")
        if "data" not in data:
            raise UnifiResponseError("Resposta UniFi sem a lista data.")
        clients = data["data"]
    else:
        clients = data
    if not isinstance(clients, list):
        raise UnifiResponseError("A resposta UniFi deve conter uma lista de clientes.")

    wireless = []
    for index, client in enumerate(clients):
        if not isinstance(client, dict):
            raise UnifiResponseError(f"Registro UniFi {index}: esperado um objeto.")
        mac = client.get("mac")
        if not isinstance(mac, str) or not mac.strip():
            raise UnifiResponseError(f"Registro UniFi {index}: mac ausente ou inválido.")
        # A API legada também retorna clientes cabeados; não pertencem ao SSID.
        if client.get("is_wired") is True:
            continue
        ssid = client.get("essid") or client.get("ssid")
        if not isinstance(ssid, str) or not ssid.strip():
            raise UnifiResponseError(f"Registro UniFi {index}: SSID ausente ou inválido.")
        wireless.append(client)
    return wireless


def validate_retry_options(cfg):
    """Limita tentativas e pausas; uma tentativa desativa as retentativas."""
    attempts = cfg.get("retry_max_attempts", 3)
    base = cfg.get("retry_backoff_seconds", 2)
    cap = cfg.get("retry_max_delay_seconds", 30)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 5:
        raise ValueError("retry_max_attempts deve ser um inteiro de 1 a 5, incluindo a primeira tentativa.")
    for name, value in (("retry_backoff_seconds", base), ("retry_max_delay_seconds", cap)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 300:
            raise ValueError(f"{name} deve ser um número maior que zero e no máximo 300 segundos.")
    if cap < base:
        raise ValueError("retry_max_delay_seconds deve ser maior ou igual a retry_backoff_seconds.")
    return attempts, float(base), float(cap)


def is_retryable_collection_error(error):
    """Política conservadora para GET: nunca repete TLS, credenciais ou dados."""
    if isinstance(error, urllib.error.HTTPError):
        if error.headers is not None and error.headers.get("Retry-After"):
            return False
        return error.code in (408, 500, 502, 503, 504)
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLError):
        return False
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    return isinstance(reason, (TimeoutError, ConnectionError))


def unifi_get_clients(cfg):
    """Coleta uma amostra validada, com retentativas limitadas e sem gravação."""
    attempts, base, cap = validate_retry_options(cfg)
    for attempt in range(1, attempts + 1):
        try:
            return _unifi_get_clients_once(cfg)
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.gaierror) as exc:
            if attempt == attempts or not is_retryable_collection_error(exc):
                raise
            # Libera a resposta HTTP de erro antes de abrir outra conexão.
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            nominal = min(base * (2 ** (attempt - 1)), cap)
            delay = random.uniform(nominal / 2, nominal)
            warnings.warn(
                f"Falha temporária na coleta. Retentativa {attempt + 1}/{attempts} "
                f"em {delay:.3f}s; estado preservado.",
                RuntimeWarning,
                stacklevel=2,
            )
            time.sleep(delay)


def _unifi_get_clients_once(cfg):
    """Lista os clientes Wi-Fi do SSID configurado.

    ATENÇÃO: o caminho do endpoint depende da versão da controladora.
    Confirmar contra a controladora real antes de produção (ver runbook IT-INFRA-017).
    """
    url = f"https://{cfg['controller_host']}{cfg['clients_path']}"
    ctx = create_tls_context(cfg)
    key = os.environ.get("UNIFI_API_KEY")
    if not key:
        raise RuntimeError("Defina a variável de ambiente UNIFI_API_KEY.")
    req = urllib.request.Request(url, headers={"X-API-KEY": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    clients = parse_unifi_clients(data)
    # O SSID vem com espaços em algumas controladoras (ex.: " wms"), por isso o strip.
    alvo = cfg["ssid"].strip()
    return [c for c in clients if (c.get("essid") or c.get("ssid")).strip() == alvo]


def normalize(raw, ap_names=None):
    """Converte o registro do UniFi para o formato interno do monitor.

    ap_names: mapa opcional {mac_da_ap: nome}, vindo de config.json.
    """
    mac = (raw.get("mac") or "").lower()
    ap_mac = (raw.get("ap_mac") or "").lower()
    ap = (raw.get("ap_name") or raw.get("access_point_name")
          or (ap_names or {}).get(ap_mac) or ap_mac)
    return {
        "mac": mac,
        "name": raw.get("name") or raw.get("hostname") or "",
        "ap": ap,
        # signal = potencia recebida em dBm; rssi na API legada e a relacao sinal-ruido (SNR)
        "rssi_dbm": raw.get("signal"),
        "snr": raw.get("rssi"),
        "band": raw.get("radio") or raw.get("channel_band") or "",
    }


# ---------------------------------------------------------------- estado e eventos

def state_paths(cfg):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    return (
        os.path.join(cfg["state_dir"], "estado.json"),
        os.path.join(cfg["state_dir"], "eventos.csv"),
    )


def load_state(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


EVENT_FIELDS = ["timestamp", "mac", "tipo", "origem_id", "nome", "evento",
                "ap", "rssi_dbm", "snr", "banda", "duracao_s"]


def append_event(path, row):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != EVENT_FIELDS:
            # formato antigo (coluna rssi_ultimo): preserva o arquivo e começa um novo
            stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
            os.replace(path, path[:-4] + "_legado_" + stamp + ".csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def poll_once(cfg, clients_now, now=None):
    """Compara a lista atual com o estado anterior e registra transições.

    clients_now: lista já normalizada (normalize()).
    Retorna lista de eventos gerados.
    """
    now = now or dt.datetime.now().replace(microsecond=0)
    state_file, events_file = state_paths(cfg)
    state = load_state(state_file)
    present = {c["mac"]: c for c in clients_now}
    events = []

    # Clientes que estavam conectados e sumiram = queda
    for mac, info in state.items():
        if info.get("online") and mac not in present:
            tipo, origem = classify(mac, info.get("name", ""))
            ev = {
                "timestamp": now.isoformat(), "mac": mac, "tipo": tipo,
                "origem_id": origem, "nome": info.get("name", ""),
                "evento": "queda", "ap": info.get("ap", ""),
                "rssi_dbm": info.get("rssi_dbm", ""), "snr": info.get("snr", ""),
                "banda": info.get("band", ""),
                "duracao_s": "",
            }
            events.append(ev)
            info["online"] = False
            info["queda_em"] = now.isoformat()

    # Clientes que voltaram ou apareceram
    for mac, c in present.items():
        tipo, origem = classify(mac, c["name"])
        prev = state.get(mac)
        if prev is None or not prev.get("online"):
            if prev and prev.get("queda_em"):
                queda = dt.datetime.fromisoformat(prev["queda_em"])
                dur = int((now - queda).total_seconds())
                ev_name = "reconexao"
            else:
                dur = ""
                ev_name = "primeira_vez" if prev is None else "reconexao"
            events.append({
                "timestamp": now.isoformat(), "mac": mac, "tipo": tipo,
                "origem_id": origem, "nome": c["name"], "evento": ev_name,
                "ap": c["ap"], "rssi_dbm": c.get("rssi_dbm", ""), "snr": c.get("snr", ""),
                "banda": c["band"],
                "duracao_s": dur,
            })
        state[mac] = {
            "online": True, "name": c["name"], "ap": c["ap"],
            "rssi_dbm": c.get("rssi_dbm", ""), "snr": c.get("snr", ""),
            "band": c["band"], "last_seen": now.isoformat(),
            "queda_em": None,  # limpa a marca de queda após a reconexão
        }

    for ev in events:
        append_event(events_file, ev)
    save_state(state_file, state)
    return events


# ---------------------------------------------------------------- relatório e alerta

def read_events(cfg):
    _, events_file = state_paths(cfg)
    if not os.path.exists(events_file):
        return []
    with open(events_file, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def drops_per_hour(events, day):
    """Conta quedas por MAC por hora, para um dia (YYYY-MM-DD)."""
    counts = {}
    for e in events:
        if e["evento"] != "queda" or not e["timestamp"].startswith(day):
            continue
        hora = e["timestamp"][11:13]
        key = (e["mac"], hora)
        counts[key] = counts.get(key, 0) + 1
    return counts


def alerts_for(cfg, events, day):
    limite = cfg["alert_drops_per_hour"]
    return [(mac, hora, n) for (mac, hora), n in sorted(drops_per_hour(events, day).items())
            if n > limite]


def build_report(cfg, day):
    events = read_events(cfg)
    dia = [e for e in events if e["timestamp"].startswith(day)]
    quedas = [e for e in dia if e["evento"] == "queda"]
    por_tipo = {}
    for e in quedas:
        por_tipo[e["tipo"]] = por_tipo.get(e["tipo"], 0) + 1
    por_mac = {}
    for e in quedas:
        por_mac.setdefault(e["mac"], {"nome": e["nome"], "tipo": e["tipo"], "n": 0})["n"] += 1
    top = sorted(por_mac.items(), key=lambda x: -x[1]["n"])[:10]
    alertas = alerts_for(cfg, events, day)

    linhas = [
        f"# Relatório de quedas dos coletores (SSID {cfg['ssid']}) - {day}",
        "",
        f"- Quedas no dia: **{len(quedas)}**",
        f"- Coletores com queda: **{len(por_mac)}**",
        "",
        "## Quedas por tipo",
        "",
        "| Tipo | Quedas |",
        "|---|---|",
    ]
    linhas += [f"| {t} | {n} |" for t, n in sorted(por_tipo.items(), key=lambda x: -x[1])] or ["| - | 0 |"]
    linhas += ["", "## Coletores mais afetados (top 10)", "", "| MAC | Nome | Tipo | Quedas |", "|---|---|---|---|"]
    linhas += [f"| {mac} | {v['nome']} | {v['tipo']} | {v['n']} |" for mac, v in top] or ["| - | - | - | 0 |"]
    linhas += ["", f"## Alertas (mais de {cfg['alert_drops_per_hour']} quedas por hora)", ""]
    linhas += [f"- {mac} às {hora}h: {n} quedas" for mac, hora, n in alertas] or ["- Nenhum alerta no dia."]
    return "\n".join(linhas) + "\n"


def write_report(cfg, day):
    text = build_report(cfg, day)
    path = os.path.join(cfg["state_dir"], f"relatorio_{day}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------- execução

def alert_registry_path(cfg):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    return os.path.join(cfg["state_dir"], "alertas_emitidos.json")


def pending_alerts(cfg, alertas, day):
    """Devolve só os alertas (MAC, hora) ainda não emitidos no dia.

    Sem isso, no modo loop o mesmo alerta seria impresso a cada ciclo.
    """
    path = alert_registry_path(cfg)
    emitidos = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            emitidos = set(json.load(f))
    novos = [(mac, hora, n) for mac, hora, n in alertas if f"{day}|{mac}|{hora}" not in emitidos]
    emitidos.update(f"{day}|{mac}|{hora}" for mac, hora, _ in novos)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(emitidos), f, indent=1)
    return novos


def run_poll(cfg):
    raw = unifi_get_clients(cfg)
    # Só uma amostra validada pode chegar à gravação de estado e eventos.
    clients = [normalize(r, cfg.get("ap_names")) for r in raw]
    events = poll_once(cfg, clients)
    hoje = dt.date.today().isoformat()
    alertas = pending_alerts(cfg, alerts_for(cfg, read_events(cfg), hoje), hoje)
    return events, alertas


def main(argv):
    global CONFIG_COLLECTORS
    cfg = load_config(os.environ.get("COLETORES_CONFIG", DEFAULT_CONFIG))
    CONFIG_COLLECTORS = cfg["collector_macs"]
    cmd = argv[1] if len(argv) > 1 else "poll"

    if cmd == "poll":
        events, alertas = run_poll(cfg)
        print(f"{len(events)} evento(s) registrado(s).")
        for mac, hora, n in alertas:
            print(f"ALERTA: {mac} com {n} quedas na hora {hora}h")
    elif cmd == "loop":
        while True:
            try:
                events, alertas = run_poll(cfg)
                print(dt.datetime.now().isoformat(), f"{len(events)} evento(s)", flush=True)
                for mac, hora, n in alertas:
                    print(f"ALERTA: {mac} com {n} quedas na hora {hora}h", flush=True)
            except Exception as exc:  # qualquer falha de coleta nao derruba o loop
                print("Falha na coleta:", exc, flush=True)
            time.sleep(cfg["poll_seconds"])
    elif cmd == "report":
        day = argv[2] if len(argv) > 2 else dt.date.today().isoformat()
        print(write_report(cfg, day))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
