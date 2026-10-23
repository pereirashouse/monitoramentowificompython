"""
Monitor de quedas Wi-Fi dos coletores (SSID configurado).

Somente leitura na controladora UniFi. Usa apenas a biblioteca padrão do Python.

Uso:
  python coletores_wifi_monitor.py poll     -> coleta uma vez e registra eventos
  python coletores_wifi_monitor.py report   -> gera o relatório do dia (Markdown)
  python coletores_wifi_monitor.py loop     -> poll a cada N segundos (config)
  python coletores_wifi_monitor.py recover-state -> restaura backup válido explicitamente

Configuração: arquivo config.json ao lado do script (ver config.example.json).
A chave da API é lida da variável de ambiente UNIFI_API_KEY (nunca do arquivo).
"""

import csv
import datetime as dt
import errno
from contextlib import contextmanager
from functools import wraps
import glob
import io
import json
import os
import random
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "config.json")
DEFAULT_CSV_MAX_BYTES = 10 * 1024 * 1024

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
    cfg.setdefault("csv_max_bytes", DEFAULT_CSV_MAX_BYTES)
    validate_csv_max_bytes(cfg["csv_max_bytes"])
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

class DataDirectoryBusy(RuntimeError):
    """Outra operação cooperante já usa a pasta de dados."""


_DATA_LOCKS = threading.local()


def _acquire_file_lock(file):
    if os.name == "posix":
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif os.name == "nt":
        import msvcrt
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        raise RuntimeError("Bloqueio de dados disponível somente em POSIX ou Windows.")


def _release_file_lock(file):
    if os.name == "posix":
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    elif os.name == "nt":
        import msvcrt
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def data_directory_lock(cfg):
    """Bloqueio não bloqueante e reentrante na mesma thread, em disco local."""
    folder = os.path.normcase(os.path.realpath(os.path.abspath(cfg["state_dir"])))
    if getattr(_DATA_LOCKS, "pid", None) != os.getpid():
        _DATA_LOCKS.pid = os.getpid()
        _DATA_LOCKS.held = set()
    held = _DATA_LOCKS.held
    if folder in held:
        yield
        return
    os.makedirs(folder, exist_ok=True)
    # Não remover o arquivo: outro processo pode estar usando o mesmo inode.
    with open(os.path.join(folder, ".monitor.lock"), "a+b") as file:
        try:
            _acquire_file_lock(file)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DataDirectoryBusy(
                    "Pasta de dados ocupada por outra execução. "
                    "Encerre a execução atual antes de iniciar outra; não apague .monitor.lock."
                ) from exc
            raise
        held.add(folder)
        try:
            yield
        finally:
            held.remove(folder)
            _release_file_lock(file)


def locked_data_operation(function):
    @wraps(function)
    def locked(cfg, *args, **kwargs):
        with data_directory_lock(cfg):
            return function(cfg, *args, **kwargs)
    return locked


def state_paths(cfg):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    return (
        os.path.join(cfg["state_dir"], "estado.json"),
        os.path.join(cfg["state_dir"], "eventos.csv"),
    )


class PersistenceError(RuntimeError):
    """Persistência inválida ou gravação malsucedida; não reiniciar do zero."""


def atomic_write_bytes(path, content):
    """Grava em temporário exclusivo na mesma pasta, fsync e replace."""
    path = os.fspath(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp",
                                         dir=os.path.dirname(os.path.abspath(path)))
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise PersistenceError(f"Falha ao gravar {os.path.basename(path)}; interrompa e verifique os arquivos.") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def atomic_write_text(path, content):
    atomic_write_bytes(path, content.encode("utf-8"))


def json_text(data):
    try:
        return json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise PersistenceError("Conteúdo de persistência não serializável ou não finito.") from exc


def read_persistent_json(path):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError, UnicodeError) as exc:
        raise PersistenceError(f"Arquivo inválido ou ilegível: {os.path.basename(path)}. Não reinicie do zero.") from exc


def validate_state(state):
    if not isinstance(state, dict):
        raise PersistenceError("Estado inválido: esperado um objeto de clientes.")
    for mac, info in state.items():
        if not isinstance(mac, str) or not mac.strip() or not isinstance(info, dict):
            raise PersistenceError("Estado inválido: chave ou registro de cliente incompatível.")
        if not isinstance(info.get("online"), bool):
            raise PersistenceError("Estado inválido: online deve ser booleano.")
        for field in ("name", "ap", "band"):
            if field in info and not isinstance(info[field], str):
                raise PersistenceError(f"Estado inválido: {field} deve ser textual.")
        for field in ("last_seen", "queda_em"):
            value = info.get(field)
            if value is not None:
                try:
                    if not isinstance(value, str) or dt.datetime.fromisoformat(value).tzinfo is not None:
                        raise ValueError
                except ValueError as exc:
                    raise PersistenceError(f"Estado inválido: {field} deve ser timestamp ISO sem fuso.") from exc
        if info["online"] and info.get("queda_em") is not None:
            raise PersistenceError("Estado inválido: cliente online com queda pendente.")
        if not info["online"] and info.get("queda_em") is None:
            raise PersistenceError("Estado inválido: cliente offline sem timestamp de queda.")
    return state


def read_state_file(path):
    state = validate_state(read_persistent_json(path))
    json_text(state)
    return state


def load_state(path):
    path = os.fspath(path)
    if not os.path.exists(path):
        folder = os.path.dirname(os.path.abspath(path))
        archives = glob.glob(os.path.join(glob.escape(folder), "eventos_arquivo_*.csv"))
        history = False
        active = os.path.join(folder, "eventos.csv")
        if os.path.exists(active):
            try:
                with open(active, newline="", encoding="utf-8") as file:
                    reader = csv.reader(file)
                    history = next(reader, []) == EVENT_FIELDS and next(reader, None) is not None
            except (OSError, UnicodeError, csv.Error) as exc:
                raise PersistenceError("Estado ausente e CSV ilegível; interrompa e reconcilie os arquivos.") from exc
        if os.path.exists(path + ".bak") or archives or history:
            raise PersistenceError("Estado ausente com backup ou histórico; recuperação explícita necessária.")
        return {}
    return read_state_file(path)


def save_state(path, state):
    path = os.fspath(path)
    content = json_text(validate_state(state))
    if os.path.exists(path):
        previous = read_state_file(path)
        atomic_write_text(path + ".bak", json_text(previous))
    elif os.path.exists(path + ".bak"):
        raise PersistenceError("Estado ausente com backup; use recuperação explícita antes de gravar.")
    atomic_write_text(path, content)


@locked_data_operation
def recover_state(cfg):
    """Restaura somente backup válido e preserva o estado corrompido."""
    state_file, _ = state_paths(cfg)
    backup_file = state_file + ".bak"
    if not os.path.exists(backup_file):
        raise PersistenceError("Backup de estado ausente; restauração automática não é possível.")
    backup = read_state_file(backup_file)
    preserved = None
    if os.path.exists(state_file):
        try:
            read_state_file(state_file)
        except PersistenceError:
            with open(state_file, "rb") as file:
                original = file.read()
            preserved = state_file + ".corrompido_" + uuid.uuid4().hex
            atomic_write_bytes(preserved, original)
        else:
            raise PersistenceError("Estado atual válido; recuperação recusada para evitar retrocesso.")
    atomic_write_text(state_file, json_text(backup))
    return preserved


EVENT_FIELDS = ["timestamp", "mac", "tipo", "origem_id", "nome", "evento",
                "ap", "rssi_dbm", "snr", "banda", "duracao_s"]


def validate_csv_max_bytes(value):
    """Um inteiro não negativo; zero desativa apenas a rotação por tamanho."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("csv_max_bytes deve ser um inteiro não negativo; 0 desativa a rotação.")
    return value


def archive_event_file(path, label):
    """Move o arquivo inteiro para um nome exclusivo, em execução única."""
    stem, _ = os.path.splitext(path)
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
    while True:
        destination = f"{stem}_{label}_{stamp}_{uuid.uuid4().hex}.csv"
        if not os.path.lexists(destination):
            break
    os.rename(path, destination)
    return destination


def append_event(path, row, max_bytes=DEFAULT_CSV_MAX_BYTES):
    max_bytes = validate_csv_max_bytes(max_bytes)
    path = os.fspath(path)
    # Serializa antes de mover arquivos: um evento inválido não inicia rotação.
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=EVENT_FIELDS)
    writer.writeheader()
    header_text = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    writer.writerow(row)
    row_text = buffer.getvalue()
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    if not new:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, [])
            has_events = next(reader, None) is not None
        if header != EVENT_FIELDS:
            archive_event_file(path, "legado")
            new = True
        elif max_bytes and has_events and os.path.getsize(path) + len(row_text.encode("utf-8")) > max_bytes:
            archive_event_file(path, "arquivo")
            new = True
    with open(path, "a", newline="", encoding="utf-8") as f:
        if new:
            f.write(header_text)
        f.write(row_text)


def poll_once(cfg, clients_now, now=None):
    """Processa uma amostra sob bloqueio exclusivo da pasta de dados."""
    validate_csv_max_bytes(cfg.get("csv_max_bytes", DEFAULT_CSV_MAX_BYTES))
    with data_directory_lock(cfg):
        return _poll_once_locked(cfg, clients_now, now)


def _poll_once_locked(cfg, clients_now, now=None):
    """Compara a lista atual com o estado anterior e registra transições.

    clients_now: lista já normalizada (normalize()).
    Retorna lista de eventos gerados.
    """
    max_bytes = validate_csv_max_bytes(cfg.get("csv_max_bytes", DEFAULT_CSV_MAX_BYTES))
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

    validate_state(state)
    json_text(state)  # serialização precisa ser possível antes de acrescentar eventos
    try:
        for ev in events:
            append_event(events_file, ev, max_bytes=max_bytes)
        save_state(state_file, state)
    except OSError as exc:
        raise PersistenceError("Falha ao persistir a coleta; reconcilie CSV e estado antes de retomar.") from exc
    return events


# ---------------------------------------------------------------- relatório e alerta

@locked_data_operation
def read_events(cfg):
    _, events_file = state_paths(cfg)
    archives = sorted(glob.glob(os.path.join(glob.escape(cfg["state_dir"]), "eventos_arquivo_*.csv")))
    paths = archives + ([events_file] if os.path.exists(events_file) else [])
    events = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None and path == events_file:
                continue
            if reader.fieldnames != EVENT_FIELDS:
                raise ValueError(f"Cabeçalho CSV incompatível: {os.path.basename(path)}.")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"Linha CSV incompleta ou com colunas extras: {os.path.basename(path)}.")
                events.append(row)
    return sorted(events, key=lambda event: event["timestamp"])


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


@locked_data_operation
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


@locked_data_operation
def write_report(cfg, day):
    text = build_report(cfg, day)
    path = os.path.join(cfg["state_dir"], f"relatorio_{day}.md")
    atomic_write_text(path, text)
    return path


# ---------------------------------------------------------------- execução

def alert_registry_path(cfg):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    return os.path.join(cfg["state_dir"], "alertas_emitidos.json")


def load_alert_registry(cfg):
    path = alert_registry_path(cfg)
    if not os.path.exists(path):
        return set()
    values = read_persistent_json(path)
    if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
        raise PersistenceError("Registro de alertas inválido; esperado lista de identificadores textuais.")
    return set(values)


@locked_data_operation
def pending_alerts(cfg, alertas, day):
    """Devolve só os alertas (MAC, hora) ainda não emitidos no dia.

    Sem isso, no modo loop o mesmo alerta seria impresso a cada ciclo.
    """
    path = alert_registry_path(cfg)
    emitidos = load_alert_registry(cfg)
    novos = [(mac, hora, n) for mac, hora, n in alertas if f"{day}|{mac}|{hora}" not in emitidos]
    emitidos.update(f"{day}|{mac}|{hora}" for mac, hora, _ in novos)
    atomic_write_text(path, json_text(sorted(emitidos)))
    return novos


@locked_data_operation
def run_poll(cfg):
    state_file, _ = state_paths(cfg)
    load_state(state_file)
    load_alert_registry(cfg)
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
    if cmd not in ("poll", "loop", "report", "recover-state"):
        print(__doc__)
        return 2
    try:
        with data_directory_lock(cfg):
            return _execute_command(cfg, cmd, argv)
    except DataDirectoryBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except PersistenceError as exc:
        print("Falha de persistência:", str(exc), file=sys.stderr)
        return 4


def _execute_command(cfg, cmd, argv):
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
            except PersistenceError as exc:
                print("Falha de persistência:", str(exc), file=sys.stderr)
                return 4
            except Exception as exc:  # qualquer falha de coleta nao derruba o loop
                print("Falha na coleta:", exc, flush=True)
            time.sleep(cfg["poll_seconds"])
    elif cmd == "report":
        day = argv[2] if len(argv) > 2 else dt.date.today().isoformat()
        print(write_report(cfg, day))
    elif cmd == "recover-state":
        preserved = recover_state(cfg)
        print("Estado restaurado de estado.json.bak. Reconcilie CSV e lacunas antes de retomar a coleta.")
        if preserved:
            print("Estado anterior preservado em", os.path.basename(preserved))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
