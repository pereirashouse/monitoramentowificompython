# Monitor de quedas Wi-Fi de coletores (WMS)

[![Testes](https://github.com/pereirashouse/monitoramentowificompython/actions/workflows/testes.yml/badge.svg)](https://github.com/pereirashouse/monitoramentowificompython/actions/workflows/testes.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Sem dependências](https://img.shields.io/badge/depend%C3%AAncias-nenhuma-brightgreen)
[![Licença MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-lightgrey)](LICENSE)

Ferramenta em Python que mede, de forma objetiva, as quedas de Wi-Fi dos coletores de dados de um centro de distribuição. Ela consulta a controladora UniFi em modo somente leitura, registra cada queda e reconexão com AP, sinal e banda, e gera um relatório diário em Markdown.

## Problema

Em um centro de distribuição, os operadores usam coletores Android (Urovo, Skorpio) conectados ao Wi-Fi para trabalhar no WMS (Warehouse Management System). Quando o coletor perde a rede, a sessão no sistema cai e o operador precisa refazer o login, o que atrasa a separação e a expedição.

As reclamações chegavam como "o coletor caiu de novo", sem horário, sem identificação do aparelho e sem a antena em que ele estava. Sem esses dados não era possível saber se o problema era de cobertura, de uma AP específica, de um modelo de coletor ou de um horário de pico.

## Objetivo

- Registrar automaticamente cada queda e cada reconexão dos coletores do SSID operacional.
- Guardar junto de cada evento a AP, o sinal (dBm), a relação sinal-ruído e a banda usada.
- Destacar os coletores que caem mais de N vezes na mesma hora.
- Produzir um relatório diário que possa ser usado para decidir ajustes de rádio (canal, potência, posição de AP) e comparar o antes e o depois.
- Fazer tudo isso sem alterar nada na controladora e sem instalar bibliotecas.

## O que eu desenvolvi

- **Coleta somente leitura** na API local do UniFi, com a chave lida de variável de ambiente (nunca de arquivo).
- **Detecção de transições** comparando a lista atual de clientes com o estado anterior: `primeira_vez`, `queda` e `reconexao`, com a duração da queda em segundos.
- **Classificação dos aparelhos** por inventário, por prefixo de fabricante (OUI) ou por nome, incluindo Android com MAC aleatório.
- **Log em CSV** (`eventos.csv`) com uma linha por evento, fácil de abrir no Excel ou cruzar com os logs da controladora.
- **Relatório diário em Markdown** com quedas por tipo, coletores mais afetados e alertas.
- **Alertas sem repetição** no modo contínuo: o mesmo alerta (MAC + hora) aparece uma vez só por dia.
- **TLS seguro por padrão**, com validação de certificado e hostname, suporte a CA interna e sem fallback automático para conexão insegura.
- **Demonstração sem controladora** (`exemplos/demonstracao.py`) e **testes automatizados** de coleta, eventos, relatórios e TLS, executados pelo GitHub Actions em Python 3.10 a 3.13.

```mermaid
flowchart LR
    A[Controladora UniFi<br/>API local, leitura] -->|lista de clientes do SSID| B[poll_once]
    B -->|compara com estado.json| C{Mudou?}
    C -->|sumiu| D[queda]
    C -->|voltou| E[reconexao + duração]
    D --> F[(eventos.csv)]
    E --> F
    F --> G[Relatório diário .md]
    F --> H[Alerta: mais de N quedas/hora]
```

## Demonstração

Dá para ver o monitor funcionando sem ter uma controladora UniFi. O script abaixo simula nove ciclos de coleta de um turno, com três coletores fictícios:

```bash
python exemplos/demonstracao.py
```

Um exemplo de relatório gerado com dados reais e sanitizados (MACs trocados por pseudônimos, antenas com nomes genéricos) está em [`exemplos/relatorio_exemplo.md`](exemplos/relatorio_exemplo.md). Ele cobre seis dias, 37 quedas e 4 antenas.

## Tecnologias

| Item | Uso |
|---|---|
| Python 3.10+ (só biblioteca padrão) | `urllib`, `ssl`, `csv`, `json`, `datetime` |
| API local do UniFi Network | Lista de clientes conectados (`/stat/sta`) |
| CSV e Markdown | Log de eventos e relatório diário |
| `unittest` + `unittest.mock` | Testes com a API simulada |
| GitHub Actions | Testes a cada push e pull request |

Testado com UniFi Network 10.6 em um UCK G2 Plus.

## Como executar

### 1. Preparar

```bash
git clone https://github.com/pereirashouse/monitoramentowificompython.git
cd monitoramentowificompython
cp config.example.json config.json
# Edite config.json com os valores do seu ambiente
```

Defina a chave de API, gerada na aplicação Network com permissão **somente leitura**:

```bash
# Linux / macOS
export UNIFI_API_KEY="sua-chave"

# Windows PowerShell (persiste após reiniciar)
setx UNIFI_API_KEY "sua-chave"
# Feche e reabra o terminal
```

### 2. Configurar

| Campo | Descrição |
|---|---|
| `controller_host` | Endereço da controladora, sem `https://`, ex.: `192.168.1.1` |
| `clients_path` | Caminho da API legada. Veja **Descobrindo o site_id** abaixo. |
| `ssid` | SSID dos coletores (espaços nas pontas são ignorados) |
| `verify_tls` | Booleano JSON; padrão `true`. Valida certificado e hostname. `false` desativa ambas as verificações e emite aviso de risco; somente para diagnóstico temporário em laboratório isolado. |
| `ca_file` | Opcional: caminho de um arquivo PEM com a CA confiável. `null`, vazio ou omitido usa as CAs padrão carregadas pelo Python. Caminho relativo é resolvido a partir do diretório de execução; prefira caminho absoluto. |
| `poll_seconds` | Intervalo do modo `loop`, em segundos (padrão: 60) |
| `retry_max_attempts` | Tentativas totais por coleta, incluindo a primeira; inteiro de 1 a 5, padrão 3. Use 1 para desativar as retentativas. |
| `retry_backoff_seconds` | Base do backoff exponencial, padrão 2 s. Número maior que zero e no máximo 300 s. |
| `retry_max_delay_seconds` | Teto de cada pausa entre tentativas, padrão 30 s. Número maior que zero e no máximo 300 s, não menor que a base. |
| `alert_drops_per_hour` | Alerta quando o mesmo MAC passa desse número de quedas na hora (padrão: 3) |
| `collector_macs` | Inventário opcional: `"mac": "Modelo"` |
| `ap_names` | Opcional: `"mac_da_ap": "Nome"`, já que a API legada não envia o nome da AP |
| `state_dir` | Opcional: pasta dos dados (padrão: `dados/` ao lado do script) |
| `csv_max_bytes` | Limite do CSV ativo em bytes, inteiro não negativo; padrão 10485760, ou 10 MiB. `0` desativa a rotação por tamanho. Um evento individual maior que o limite é preservado inteiro. |

Para usar outro arquivo de configuração, defina a variável `COLETORES_CONFIG` com o caminho dele.

**Certificados e CA interna**

O exemplo agora usa `verify_tls: true`. Isso também é o padrão quando o campo é omitido. Use `true` e `false` sem aspas: valores como `"false"`, `0` ou `null` são rejeitados para evitar configuração ambígua. `ca_file` deve ser `null` ou uma string; booleanos, números, listas e objetos são rejeitados, mesmo no modo de laboratório.

Se a controladora tiver certificado assinado por uma CA já confiável para o Python, mantenha `ca_file: null`. Para uma CA interna, obtenha o certificado público da CA com o administrador por um canal confiável e confira sua impressão digital por um canal independente antes de utilizá-lo. Não publique chaves privadas nem copie como confiável um certificado obtido de uma conexão não autenticada.

Edite os campos abaixo em `config.json`, preservando os demais:

```json
{
  "controller_host": "unifi.exemplo.interno",
  "verify_tls": true,
  "ca_file": "/caminho/absoluto/ca-interna.pem"
}
```

No Windows, um caminho JSON pode usar barras normais, por exemplo `"C:/certificados/ca-interna.pem"`. O arquivo deve conter certificado(s) PEM, sem chave privada. Se o servidor usar certificado autoassinado, prefira substituí-lo por um certificado emitido por uma CA confiável. Caso seja necessário confiar explicitamente naquele certificado, confirme sua autenticidade e forneça seu PEM em `ca_file`, desde que seja compatível com a validação do Python/OpenSSL. Confiar no emissor não dispensa a correspondência do hostname ou IP em `controller_host` com o SAN do certificado.

Um erro `CERTIFICATE_VERIFY_FAILED` deve ser investigado: confira CA/cadeia, validade, relógio e hostname. Se `ca_file` apontar para um arquivo inexistente ou um PEM inválido, a coleta falha sem recorrer às CAs padrão. Omitir o campo ou usar `null`/string vazia é permitido e usa as CAs padrão; nesse caso, o certificado do servidor precisa ser confiável por elas. O monitor **não** tenta novamente com a verificação desligada e **não** registra uma falha TLS como queda de todos os coletores. No modo `loop`, informa a falha e aguarda o próximo ciclo com a mesma política TLS.

`verify_tls: false` permanece apenas para compatibilidade e diagnóstico temporário em laboratório isolado. Essa opção desativa a autenticação do servidor, não apenas a rejeição de certificados autoassinados, pode expor a chave de API a interceptação e ignora `ca_file`. O monitor emite um `RuntimeWarning` explícito. Não use essa opção em produção nem suprima o aviso para contornar um erro de certificado. Configurações antigas não são reescritas automaticamente: se seu `config.json` já contém `false`, ajuste-o manualmente e configure a CA antes de coletar.

**Descobrindo o site_id**

```bash
curl --fail --silent --show-error -H "X-API-KEY: $UNIFI_API_KEY" \
  https://SEU_CONTROLLER/proxy/network/integration/v1/sites | python3 -m json.tool
```

Para CA interna, acrescente `--cacert /caminho/absoluto/ca-interna.pem` ao comando `curl`. Use um hostname ou IP presente no certificado; não acrescente `-k` ou `--insecure` para contornar uma falha.

Procure o campo `internalReference` do seu site e substitua em `clients_path`:
`/proxy/network/api/s/SEU_SITE_ID/stat/sta`

### 3. Rodar

```bash
# Uma coleta imediata
python coletores_wifi_monitor.py poll

# Loop contínuo (pausa padrão de 60 s após cada ciclo)
python coletores_wifi_monitor.py loop

# Relatório do dia atual
python coletores_wifi_monitor.py report

# Relatório de uma data específica
python coletores_wifi_monitor.py report 2026-10-15
```

### 4. Testar

```bash
python -m unittest discover -v
```

Os testes não acessam a controladora: a API é simulada. A suíte cobre respostas JSON/UTF-8 inválidas, formatos inesperados, erros explícitos da API, falhas HTTP, timeout na conexão/leitura e preservação de estado, CSV e alertas após uma falha. Os testes de retentativas simulam também a espera e a variação aleatória, verificando limites, recuperação e ausência de novas tentativas para erros não transitórios. A suíte TLS verifica o padrão seguro, o uso de CA, a rejeição de opções ambíguas, o aviso no modo inseguro e a propagação de erros sem fallback. Os contextos SSL são inspecionados, mas a suíte não substitui uma validação ponta a ponta contra a controladora.

### Falha de coleta não é queda de coletor

Antes de atualizar estado ou eventos, o monitor valida a amostra inteira. Aceita uma lista direta de registros ou um objeto com `data` contendo essa lista. Se houver `meta`, ele deve confirmar `rc: "ok"`; um campo `error` com conteúdo também interrompe a coleta. Cada registro precisa de um `mac` textual não vazio. Registros Wi-Fi precisam de `essid` ou `ssid` textual não vazio; clientes explicitamente cabeados (`is_wired: true`) são ignorados depois da validação do MAC. Um registro inválido, mesmo de outro SSID, invalida a amostra: ele não é descartado silenciosamente para produzir uma lista parcial.

Uma lista vazia **válida** continua significando que não há clientes do SSID naquela amostra e pode gerar eventos de queda para quem estava online. Já uma resposta sem `data`, JSON malformado, erro da API ou timeout não significa lista vazia: a exceção é propagada antes de gravar `estado.json`, `eventos.csv` ou `alertas_emitidos.json`. Os testes comparam os bytes desses arquivos antes e depois das falhas simuladas. O bloqueio é adquirido antes da requisição: mesmo se a primeira coleta falhar, a pasta e o arquivo técnico `.monitor.lock` podem existir, mas não são criados estado, CSV ou alertas por essa falha.

No comando `poll`, um erro não repetível ou o esgotamento das tentativas interrompe a execução com status de falha. No modo `loop`, a mensagem `Falha na coleta` é exibida e o próximo ciclo é tentado após `poll_seconds`, sem trocar a política TLS. Se o coletor continuar presente na amostra válida seguinte, não é inventada uma queda ou reconexão para preencher a lacuna. A ausência de eventos durante uma falha **não comprova disponibilidade**: o intervalo ficou sem observação.

O timeout de 20 segundos é aplicado pelo `urlopen` às operações de rede; não é um prazo total garantido para o ciclo inteiro. A validação exige o formato descrito: se outra versão da API omitir o SSID ou representar clientes cabeados de outra maneira, confirme e adapte o contrato antes de usar a coleta; não converta uma resposta desconhecida em ausência de clientes.

### Retentativas limitadas com backoff

Por padrão, uma coleta pode realizar **até três tentativas totais**, apenas de leitura (GET). São repetíveis timeout, falhas de conexão, DNS temporário (`EAI_AGAIN`) e HTTP 408/500/502/503/504 sem `Retry-After`. Antes de repetir, a resposta HTTP de erro é fechada, um aviso genérico é emitido e há uma pausa. O aviso não inclui a chave de API nem o corpo ou motivo devolvido pelo servidor.

A pausa nominal dobra a partir de `retry_backoff_seconds`, respeitando `retry_max_delay_seconds`. A espera efetiva é sorteada entre metade e o total dessa pausa (jitter), para reduzir a sincronização entre monitores. Com os padrões, a primeira espera fica entre 1 e 2 s e a segunda entre 2 e 4 s; não há espera adicional depois da última tentativa. As opções aceitam apenas números finitos dentro dos limites da tabela. Use `retry_max_attempts: 1` para manter uma única tentativa.

Não há retentativa dentro da mesma coleta para HTTP 401/403/429, outros códigos fora da lista, respostas com `Retry-After`, DNS permanente ou motivo de rede desconhecido. Também não são repetidos erros TLS, configuração inválida, chave ausente, JSON/UTF-8 malformado e falha de validação da amostra. A verificação TLS nunca é desligada para recuperar uma coleta.

HTTP 429 e `Retry-After` encerram o ciclo sem novas requisições imediatas; o modo `loop` ainda usa `poll_seconds` para o ciclo seguinte e **não implementa um agendador que respeite automaticamente um prazo maior indicado pelo servidor**. Se isso ocorrer, interrompa o loop e ajuste a frequência segundo a política da API antes de continuar. Essa política conservadora não é um limitador global de requisições.

Durante as tentativas, os arquivos de estado e eventos permanecem inalterados. Se uma tentativa recuperar uma amostra válida, ela é processada uma vez; se todas falharem, o erro final é propagado e o estado anterior é preservado. As pausas e o tempo das requisições aumentam o intervalo entre amostras. O bloqueio da pasta permanece ativo nas retentativas; registro separado de lacunas ainda não é implementado.

## Exemplo de uso

Cenário fictício: um Urovo na antena "Expedição - 1" com sinal fraco (-71 dBm) cai quatro vezes entre 22h e 22h30. Um Android com MAC aleatório cai uma vez. Um Skorpio na 5 GHz com sinal bom não cai.

**Eventos registrados** (saída de `python exemplos/demonstracao.py`, uma linha por evento do `eventos.csv`):

```text
2026-10-20T22:00:00  primeira_vez b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm
2026-10-20T22:00:00  primeira_vez 04:7b:cb:00:00:02  Skorpio X5                 Expedição - 5      -52 dBm
2026-10-20T22:00:00  primeira_vez 9e:0d:5e:00:00:03  Android (MAC aleatório)    Administração - 1  -49 dBm
2026-10-20T22:01:00  queda        b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm
2026-10-20T22:02:00  reconexao    b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm (60 s)
2026-10-20T22:10:00  queda        b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm
2026-10-20T22:11:00  reconexao    b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm (60 s)
2026-10-20T22:20:00  queda        b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm
2026-10-20T22:21:00  reconexao    b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm (60 s)
2026-10-20T22:30:00  queda        b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm
2026-10-20T22:30:00  queda        9e:0d:5e:00:00:03  Android (MAC aleatório)    Administração - 1  -49 dBm
2026-10-20T22:31:00  reconexao    b4:29:3d:00:00:01  Urovo DT40                 Expedição - 1      -71 dBm (60 s)
2026-10-20T22:31:00  reconexao    9e:0d:5e:00:00:03  Android (MAC aleatório)    Administração - 1  -49 dBm (60 s)
```

**Relatório do dia** (`dados/relatorio_2026-10-20.md`):

```markdown
# Relatório de quedas dos coletores (SSID wms) - 2026-10-20

- Quedas no dia: **5**
- Coletores com queda: **2**

## Quedas por tipo

| Tipo | Quedas |
|---|---|
| Urovo DT40 | 4 |
| Android (MAC aleatório) | 1 |

## Coletores mais afetados (top 10)

| MAC | Nome | Tipo | Quedas |
|---|---|---|---|
| b4:29:3d:00:00:01 |  | Urovo DT40 | 4 |
| 9e:0d:5e:00:00:03 | android-a1b2 | Android (MAC aleatório) | 1 |

## Alertas (mais de 3 quedas por hora)

- b4:29:3d:00:00:01 às 22h: 4 quedas
```

**Como ler:** o alerta aponta um aparelho e uma antena. Com o sinal de -71 dBm na 2,4 GHz, a hipótese a investigar primeiro é cobertura naquele ponto (posição ou potência da AP, ou o coletor preso em uma AP distante), e não um problema geral da rede.

### Saídas

| Arquivo | Descrição |
|---|---|
| `dados/estado.json` | Estado atual dos coletores (interno) |
| `dados/eventos.csv` | CSV ativo de eventos com timestamp, AP, sinal e banda |
| `dados/eventos_arquivo_<data>_<id>.csv` | Arquivos rotacionados no formato atual, incluídos na leitura de eventos, relatórios e alertas |
| `dados/eventos_legado_<data>_<id>.csv` | Formatos antigos preservados separadamente; não são convertidos nem incluídos automaticamente nos relatórios |
| `dados/alertas_emitidos.json` | Alertas já mostrados no dia, para o modo `loop` não repetir o mesmo alerta |
| `dados/relatorio_AAAA-MM-DD.md` | Relatório diário com top coletores afetados, quedas por hora e alertas |
| `dados/.monitor.lock` | Arquivo técnico persistente usado pelo bloqueio do sistema operacional; sua existência não significa que há execução ativa |

### Rotação do CSV sem exclusão de histórico

Antes de gravar um evento, o monitor calcula o tamanho da linha serializada em UTF-8. Se acrescentá-la ultrapassaria `csv_max_bytes` e o CSV ativo já possui eventos, ele move o arquivo inteiro para `eventos_arquivo_<data>_<id>.csv` na mesma pasta e inicia um novo `eventos.csv` com cabeçalho. O nome combina data/hora e identificador exclusivo, sem substituir nomes já existentes em execução única. A rotação preserva os bytes do arquivo anterior; não recorta linhas nem exclui arquivos por idade ou quantidade.

O limite é um alvo por arquivo, não um teto rígido: o primeiro evento de um arquivo é gravado inteiro, mesmo se cabeçalho e linha ultrapassarem o limite. Isso evita perder um evento grande ou criar arquivos rotacionados só com cabeçalho. Um CSV antigo já acima do limite é rotacionado na próxima gravação de evento. Arquivo vazio recebe um cabeçalho normal; `csv_max_bytes: 0` mantém a gravação sem rotação por tamanho.

`read_events`, os relatórios e os alertas leem os arquivos `eventos_arquivo_*.csv` e o CSV ativo, ordenando os eventos por timestamp. Funcionam também quando há somente arquivos rotacionados. CSVs legados e outros arquivos não são incluídos. Cabeçalho incompatível ou linha com colunas ausentes/extras em um arquivo atual gera erro visível, em vez de um relatório silenciosamente incompleto. Não copie o mesmo arquivo de histórico mais de uma vez com nomes que correspondam a esse padrão: o monitor não deduplica cópias inseridas manualmente.

Essa rotação limita o tamanho do **arquivo ativo**, mas não o uso total de disco nem o consumo de memória: os relatórios ainda carregam todo o histórico atual. Planeje backup e arquivamento externo conforme a necessidade; remover arquivos da pasta de dados reduz o período disponível nos relatórios. Com o monitor parado, copie `eventos.csv`, os arquivos rotacionados, `estado.json` e `alertas_emitidos.json` para preservar o conjunto consistente. Os testes desta etapa usam dados fictícios e não equivalem a validação de backup/restauração em produção.

Há bloqueio exclusivo por `state_dir` nas operações cooperantes, descrito abaixo. Não há transação conjunta entre CSV e estado: falhas de disco ou interrupção entre gravações ainda precisam de recuperação operacional, e não há promessa de ausência de perda/duplicação nesses cenários. Os testes de rotação verificam UTF-8, nomes exclusivos, preservação de bytes, histórico sem duplicação no fluxo normal e relatórios/alertas equivalentes antes e depois da rotação.

### Controle de concorrência e execução única

Os comandos `poll`, `loop` e `report` adquirem um bloqueio exclusivo e não bloqueante da pasta de dados antes de operar. No `loop`, ele permanece ativo durante toda a execução, inclusive requisições, retentativas e pausas entre ciclos. Uma segunda execução na mesma pasta termina com a mensagem `Pasta de dados ocupada` e **status de saída 3**, sem consultar a API nem gravar dados. Para gerar relatório de um loop em execução, encerre-o primeiro, execute `report` e depois reinicie o loop. Pastas distintas podem ser usadas de forma independente.

O bloqueio usa `flock` em POSIX e bloqueio de um byte via `msvcrt` no Windows, apenas da biblioteca padrão. A pasta é identificada pelo caminho real normalizado. Chamadas aninhadas na mesma thread reutilizam o bloqueio, sem deadlock; outra thread ou processo precisa obter seu próprio bloqueio. Sistemas sem esses backends falham explicitamente, em vez de seguir sem proteção.

O arquivo `.monitor.lock` **não deve ser apagado**, mesmo após encerrar o monitor. A proteção é do sistema operacional, não da existência do arquivo nem de um PID gravado. O bloqueio é liberado ao sair normalmente, após exceção/Ctrl+C ou quando o processo termina abruptamente. Um arquivo de lock remanescente é normal e não precisa de limpeza. Apagá-lo enquanto houver um processo ativo pode permitir que uma nova execução abra outro arquivo e contorne a proteção.

Ao importar o módulo, `run_poll`, `poll_once`, `read_events`, `build_report`, `write_report` e `pending_alerts` também usam o bloqueio e levantam `DataDirectoryBusy` em conflito. Funções de baixo nível como `append_event`, `save_state`, `load_state` e `archive_event_file` pressupõem que o chamador mantém `with data_directory_lock(cfg):` enquanto opera. Usar essas funções sem esse contexto, editar arquivos manualmente ou executar versões antigas que não participam do protocolo **não** está protegido.

O escopo é **disco local e processos cooperantes**. Não é um bloqueio distribuído, não foi validado para NFS/SMB/pastas sincronizadas e não impede ferramentas externas de alterar arquivos. Pare o monitor antes de backups ou manipulação externa. O CI verifica exclusão e liberação com processos reais separados no Linux; o caminho específico de `msvcrt` é simulado em testes, sem alegação de validação real no Windows. Recuperação de estado/CSV após interrupção continua sendo uma etapa separada.

<details>
<summary>Colunas do <code>eventos.csv</code></summary>

| Coluna | Descrição |
|---|---|
| `timestamp` | ISO 8601 (AAAA-MM-DDTHH:MM:SS) |
| `mac` | MAC do coletor |
| `tipo` | Modelo do inventário, Urovo, Skorpio X5, Android (MAC aleatório) ou Desconhecido |
| `origem_id` | Como o tipo foi identificado: `inventario`, `fabricante`, `nome` ou `nenhuma` |
| `nome` | Nome ou hostname informado pela controladora |
| `evento` | `queda`, `reconexao` ou `primeira_vez` |
| `ap` | Nome da AP (pela API ou por `ap_names`) ou, na falta dele, o MAC da AP |
| `rssi_dbm` | Potência recebida em dBm (campo `signal` da API) |
| `snr` | Relação sinal-ruído (campo `rssi` da API legada) |
| `banda` | Rádio usado (`ng` = 2,4 GHz, `na` = 5 GHz) |
| `duracao_s` | Duração da queda em segundos (vazio na linha de queda; preenchido na reconexão) |

Se o `eventos.csv` existente estiver no formato antigo, ele é movido para `eventos_legado_<data>_<id>.csv` e um novo arquivo é iniciado. O conteúdo antigo é preservado sem conversão e não entra automaticamente nos relatórios do formato atual. Arquivos legados de versões anteriores também permanecem fora da leitura atual.

</details>

<details>
<summary>Classificação automática dos aparelhos</summary>

| Prefixo | Modelo |
|---|---|
| `b4:29:3d` | Urovo |
| `04:7b:cb` | Skorpio X5 |
| `3c:e1:a1` | Skorpio X5 |

Dispositivos com MAC aleatório (bit "localmente administrado" ligado) e nome começando em `android` são identificados como Android com MAC aleatório. Adicione MACs individuais ao inventário em `config.json` para identificação precisa.

</details>

## Limitações

- **Amostragem por polling.** Só são observadas as transições presentes nas amostras válidas. Em um exemplo fictício, um cliente online às 10h00 e às 10h01 pode ter caído e voltado entre esses horários sem gerar evento. Reduzir o intervalo pode melhorar a chance de detecção, mas não garante captar toda queda. O modo `loop` espera 60 s por padrão depois de cada ciclo, além do tempo gasto na coleta; a duração registrada é uma estimativa entre observações, não a duração exata no rádio. Correlacione com os logs de eventos do UniFi.
- **Lacunas do próprio monitor.** Erros de coleta não alteram os arquivos nem são tratados como queda de todos os clientes. Ainda assim, o intervalo sem amostra válida é desconhecido. Se um coletor já estava marcado como offline, o tempo até a próxima reconexão pode incluir a parada do monitor. No relatório de exemplo, essas quedas estão marcadas como "possível lacuna"; a execução não produz ainda um registro separado de lacunas.
- **Início a frio.** Coletores já offline quando o monitor inicia não geram evento de queda; só a reconexão é registrada.
- **API não documentada.** A API legada (`/stat/sta`) não é documentada oficialmente pela Ubiquiti e pode mudar em versões futuras.
- **Só UniFi.** Outras controladoras (Aruba, Omada, Cisco) precisariam de um novo `unifi_get_clients`.
- **Queda não é causa.** O monitor mostra quando, onde e com que sinal o coletor caiu. A causa (roaming, interferência, economia de energia do Android, sessão do WMS) ainda precisa ser confirmada com outras fontes.
- **TLS.** Certificados autoassinados ou de CA interna exigem configuração de confiança explícita e hostname/IP compatível. O monitor falha sem reduzir a segurança; um laboratório validado não comprova funcionamento em outras controladoras.

## Próximos passos

- Incluir no relatório o resumo por AP e por banda (hoje só no relatório de exemplo), para apontar a antena problemática direto.
- Registrar os eventos de roaming (troca de AP sem queda) para separar roaming lento de perda real de sinal.
- Ler também métricas da AP no momento da queda (TX Retry, Channel Utilization) para correlacionar com interferência.
- Exportar para Prometheus ou Zabbix e acompanhar a tendência em um painel.
- Rodar como serviço (systemd no Linux, Agendador de Tarefas no Windows), com recuperação de falhas de arquivo; rotação por tamanho e bloqueio de execução única já estão implementados.
- Adaptador para outras controladoras.

## Segurança

- Nunca grave a chave de API em arquivo, código ou histórico de shell.
- Chave exposta: revogue imediatamente na aplicação Network e gere outra.
- O agente faz apenas leituras; não altera configuração da controladora.
- Não execute como root ou administrador.
- Mantenha `verify_tls: true`; proteja a integridade do arquivo de CA e renove certificados antes de expirarem.

## Licença

[MIT](LICENSE)
