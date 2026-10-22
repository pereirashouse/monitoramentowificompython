# wifi-collector-monitor

Monitor de quedas Wi-Fi para coletores de dados (WMS) via API local do UniFi.

Detecta em tempo quase real quando coletores do SSID configurado somem e voltam, registra duração, AP e RSSI em CSV e gera relatório diário em Markdown — sem dependências externas.

## Contexto

Ambientes de WMS (Warehouse Management System) com coletores Android/Urovo/Skorpio em Wi-Fi corporativo apresentam quedas de sessão que são difíceis de medir objetivamente. Esse agente foi desenvolvido para correlacionar as quedas com métricas de rádio (RSSI, TX Retry, Canal Busy) durante otimizações de Wi-Fi.

## Requisitos

- Python 3.10 ou superior
- Controladora UniFi com API local acessível (testado no UniFi Network 10.6 / UCK G2 Plus)
- Chave de API com permissão **somente leitura** gerada na aplicação Network

## Instalação

```bash
git clone https://github.com/pereirashouse/monitoramentowificompython.git
cd monitoramentowificompython
cp config.example.json config.json
# Edite config.json com os valores do seu ambiente
```

Defina a chave de API (nunca em arquivo):

```bash
# Linux / macOS
export UNIFI_API_KEY="sua-chave"

# Windows PowerShell (persiste após reiniciar)
setx UNIFI_API_KEY "sua-chave"
# Feche e reabra o terminal
```

## Configuração

Edite `config.json`:

| Campo | Descrição |
|---|---|
| `controller_url` | URL base da controladora, ex.: `https://192.168.1.1` |
| `clients_path` | Caminho da API legada. Veja **Descobrindo o site_id** abaixo. |
| `ssid` | SSID dos coletores (exato, com distinção de maiúsculas) |
| `alert_drops_per_hour` | Quantas quedas do mesmo MAC/hora disparam alerta (padrão: 3) |
| `collector_macs` | Inventário opcional: `"mac": "Modelo"`. MACs em minúsculas. |

### Descobrindo o site_id

```bash
curl -sk -H "X-API-KEY: $UNIFI_API_KEY" \
  https://SEU_CONTROLLER/proxy/network/integration/v1/sites | python3 -m json.tool
```

Procure o campo `internalReference` do seu site e substitua em `clients_path`:
`/proxy/network/api/s/SEU_SITE_ID/stat/sta`

## Uso

```bash
# Uma coleta imediata
python coletores_wifi_monitor.py poll

# Loop contínuo (uma coleta por minuto)
python coletores_wifi_monitor.py loop

# Relatório do dia atual
python coletores_wifi_monitor.py report

# Relatório de uma data específica
python coletores_wifi_monitor.py report 2024-03-15
```

## Saídas

| Arquivo | Descrição |
|---|---|
| `dados/estado.json` | Estado atual dos coletores (interno) |
| `dados/eventos.csv` | Log de todos os eventos (queda, reconexão) com timestamp, AP e RSSI |
| `dados/relatorio_AAAA-MM-DD.md` | Relatório diário com top coletores afetados, quedas por hora e alertas |

### Colunas do CSV

| Coluna | Descrição |
|---|---|
| `timestamp` | ISO 8601 (AAAA-MM-DDTHH:MM:SS) |
| `mac` | MAC do coletor |
| `tipo` | Urovo / Skorpio / Android (MAC aleatório) / Desconhecido |
| `evento` | `queda`, `reconexao` ou `primeira_vez` |
| `ap` | MAC do AP onde o coletor estava conectado |
| `rssi` | RSSI em dBm no momento da queda |
| `duracao_s` | Duração da queda em segundos (vazio na linha de queda; preenchido na reconexão) |

## Classificação automática

O agente classifica os coletores por prefixo OUI:

| Prefixo | Modelo |
|---|---|
| `b4:29:3d` | Urovo |
| `04:7b:cb` | Skorpio |
| `3c:e1:a1` | Skorpio |

Coletores com `hostname` começando em `android-` são identificados como Android com MAC aleatório. Adicione MACs individuais ao inventário em `config.json` para identificação precisa.

## Limitações

- Polling de 60 s: quedas mais curtas que isso podem não ser detectadas. Para rastreamento exato, use os logs de eventos do UniFi em paralelo.
- Coletores já offline quando o agente inicia não geram evento de queda; apenas a reconexão é registrada.
- A API legada (`/stat/sta`) não é documentada oficialmente pela Ubiquiti; pode mudar em versões futuras.
- TLS: a controladora usa certificado autoassinado por padrão; a verificação de certificado é desabilitada (sem opção de CA personalizada nesta versão).

## Segurança

- Nunca grave a chave de API em arquivo, código ou histórico de shell.
- Chave exposta: revogue imediatamente na aplicação Network e gere outra.
- O agente faz apenas leituras; não altera configuração da controladora.
- Não execute como root ou administrador.

## Testes

```bash
python -m unittest test_coletores_monitor -v
```

## Licença

MIT
