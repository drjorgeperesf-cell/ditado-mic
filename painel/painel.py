# -*- coding: utf-8 -*-
"""
PAINEL DO DITADO - servidor local (somente leitura) - PORTE WINDOWS
====================================================================

PORTE DO MAC (2026-09): este arquivo veio do painel do macOS. So mudou o que
era EXCLUSIVO do Mac - caminhos, as sondas de sistema e a acao de reiniciar.
Rotas, portao de blindagem, cabecalhos, limites HTTP, ticket one-shot,
sessao por cabecalho, sanitizacao do FTS5 e exportacao em lotes: IDENTICOS.
As mudancas do porte estao marcadas com o comentario "PORTE WINDOWS".

O QUE E: um painel web que roda em http://127.0.0.1:7910 e mostra TRES telas:
  1. HISTORICO - lista/busca dos ditados guardados no history.db
                 (+ botao Exportar: baixa os ditados filtrados em .md/.txt -
                 o history.db e uma janela de 30 dias, exportar e o unico
                 jeito de nao perder o que expira)
  2. SAUDE     - as 4 tarefas do Agendador, whisper-server, processos,
                 aquecedor, memoria e os avisos do guardiao
  3. INSIGHTS  - numeros agregados do banco (ditados/dia, apps, perfis,
                 tempo falado). Foi um numero desses que revelou em 31/07
                 que o dicionario medico estava no perfil errado.

O QUE ELE **NAO** FAZ (regra de ouro do projeto):
  - NAO toca no caminho do ditado. E um PROCESSO SEPARADO.
  - NAO escreve no history.db (conexao read-only por URI, mode=ro).
  - NAO cria/edita/apaga tarefa do Agendador, nao mexe em registro.
  - NAO mexe em %LOCALAPPDATA%\\BotaoWispr\\ (so LE o .db e os logs).
  - A UNICA acao no sistema e o botao "Reiniciar o motor", que roda
    `schtasks /End` + `schtasks /Run` na tarefa
    DitadoMic-Servidor. O nome da tarefa e CONSTANTE no codigo,
    JAMAIS vem da request.
    A tarefa do APP do ditado (DitadoMic-App) NUNCA e reiniciada -
    isso derrubaria o gancho de teclado e o usuário perderia a hotkey.

AVISO MORTAL PARA QUEM FOR "SIMPLIFICAR" ISTO NO FUTURO:
  Nunca embuta estas rotas dentro do app.py. Foi medido: um leitor
  `mode=ro` e o escritor do ditado como THREADS DO MESMO PROCESSO fazem o
  INSERT do ditado morrer com "disk I/O error" (76 falhas em 94 tentativas).
  SQLITE_IOERR nao e SQLITE_BUSY, entao o busy_timeout nao salva e nao ha
  retentativa: o ditado e PERDIDO. Em processos SEPARADOS (o caso aqui) o
  problema nao existe: 4 leitores em loop apertado fizeram 11.277 leituras
  durante escritas, com 0 falha. Este arquivo tem que continuar sendo um
  processo proprio.

PRIVACIDADE:
  O history.db guarda o texto integral de ditados medicos e pessoais. Por isso:
    - bind SO em 127.0.0.1 (nunca 0.0.0.0)
    - NTFS nao tem umask: a protecao de arquivo e ACL via icacls (ver
      _restringir_acl). O painel so CRIA o .mestre e o proprio log.
    - log_message do http.server sobrescrito com `pass` (o padrao imprimiria
      a query string, ou seja, a busca medica E o ticket de sessao)
    - o log em %LOCALAPPDATA%\\BotaoWispr\\logs\\painel.log registra so
      boot/porta/erro/rota,
      NUNCA o texto dos ditados nem os parametros de busca
    - sessao por CABECALHO (X-Painel-Sessao, guardado no sessionStorage da
      porta) + portao de Host/Origin/Sec-Fetch-Site. Cookie foi ABANDONADO:
      cookie e cego a porta e vazava a sessao para todo outro servidor
      local que o Chrome visitasse - ver Manipulador._sessao_valida()

COMO RODAR (PORTE WINDOWS):
    python painel.py                      # sobe e abre o navegador
    python painel.py --sem-navegador      # so sobe (uso em teste)
    python painel.py --porta 7910
    python painel.py --garantir-mestre    # cria/le o .mestre e imprime
    set PAINEL_MESTRE=<segredo> && python painel.py --sem-navegador

  O PAINEL.bat passa o segredo mestre pela variavel de ambiente
  PAINEL_MESTRE e deve passar --sem-navegador, porque quem abre o navegador
  nesse fluxo e o proprio .bat.
  O segredo NUNCA vai por argv: linha de comando de qualquer processo e
  legivel por WMI/`Get-CimInstance Win32_Process` para o mesmo usuario e
  para admin. Foi por isso que o ticket passou a ser one-shot e curto.

Python do venv do projeto, stdlib pura, zero pip.
Estilo do original preservado (sem match/case, sem PEP 604).
"""

import argparse
import ctypes
import datetime
import errno
import glob
import html
import http.server
import json
import os
import plistlib
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor


# =====================================================================
# 2. CONSTANTES E CAMINHOS
# =====================================================================

PASTA_PAINEL = os.path.dirname(os.path.abspath(__file__))


# PORTE WINDOWS: ~/Library/Application Support/BotaoWispr vira
# %LOCALAPPDATA%\BotaoWispr, com override por MICROFONE_DADOS (mesma regra do
# recursos/log.py do app - painel e app TEM que apontar pro mesmo lugar).
def base_dados():
    raiz = os.environ.get("MICROFONE_DADOS")
    if raiz:
        return raiz
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "BotaoWispr")


def dir_logs():
    return os.path.join(base_dados(), "logs")


CAMINHO_DB = os.path.join(base_dados(), "history.db")

# PORTE WINDOWS: os 3 logs do ditado (o painel so LE mtime e tamanho deles).
LOG_APP = os.path.join(dir_logs(), "ditado-mic.log")
LOG_KEEPWARM = os.path.join(dir_logs(), "keepwarm.log")
LOG_INCIDENTES = os.path.join(dir_logs(), "guardiao.log")
# PORTE WINDOWS: /tmp/wispr-painel.log -> log proprio do painel.
LOG_PAINEL = os.path.join(dir_logs(), "painel.log")
LOG_MAX_B = 1024 * 1024          # 1 MB: acima disso o log e truncado no lugar

CAMINHO_MESTRE = os.path.join(PASTA_PAINEL, ".mestre")

URL_MOTOR = "http://127.0.0.1:7777/"

# PORTE WINDOWS: LaunchAgents viram tarefas do Agendador. Os 4 nomes sao
# CONSTANTES deste arquivo - nenhum deles vem da request, nunca.
TAREFA_APP = "DitadoMic-App"
TAREFA_SERVIDOR = "DitadoMic-Servidor"
TAREFA_GUARDIAO = "DitadoMic-Guardiao"
TAREFA_KEEPWARM = "DitadoMic-KeepWarm"

PORTA_PADRAO = 7910
SEGUNDOS_OCIOSO = 1200          # 20 min sem request -> o painel se encerra
VALIDADE_TICKET = 90.0          # o ?t= aparece no `ps`, entao vive pouco
TETO_CORPO_POST = 4096

# O segredo de sessao viaja em CABECALHO, nunca em cookie - ver o comentario
# grande em Manipulador._sessao_valida(). MARCA_SESSAO e o buraco que o
# interface.html traz e que o servidor preenche com o token na unica request
# que apresenta um ticket valido.
CABECALHO_SESSAO = "X-Painel-Sessao"
MARCA_SESSAO = "__TOKEN_DE_SESSAO__"

# Limiares COPIADOS do guardiao (recursos/wispr-guardiao-memoria.py).
# Painel e guardiao discordando e pior que nao ter painel - nao inventar
# numero novo aqui.
SWAP_LIVRE_MIN_MB = 512         # guardiao linha 97
KEEPWARM_PARADO_S = 600         # guardiao linha 103
JANELA_WAKE_S = 360             # vigiar_keepwarm

# PORTE WINDOWS: os caminhos usuais do Chrome no Windows, na ordem de busca.
CHROMES = (
    os.path.join(os.environ.get("PROGRAMFILES") or r"C:\Program Files",
                 "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)")
                 or r"C:\Program Files (x86)",
                 "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA") or "",
                 "Google", "Chrome", "Application", "chrome.exe"),
)

# Sentinela para o filtro dos 4 ditados que tem app_bundle = '' no banco.
# String vazia no parametro significa "sem filtro", entao esses 4 precisam
# de um valor proprio para serem selecionaveis na interface.
SENTINELA_VAZIO = "(vazio)"

# --- insights / exportacao -------------------------------------------
# Os 3 perfis que o app.py conhece (Right Cmd / +Shift / +Opt). A tela de
# insights mostra os tres SEMPRE, mesmo com zero uso - foi exatamente um
# "prontuario: 0" que revelou em 31/07 que o dicionario medico estava
# configurado no perfil errado. Zero e a informacao valiosa.
PERFIS_CONHECIDOS = ("livre", "prontuario", "casual")

# Janela do grafico "ditados por dia". E tambem o history_keep_days default
# do app.py - o banco inteiro cabe na janela.
JANELA_INSIGHTS_DIAS = 30

# Exportacao em LOTES: cada lote e uma conexao read-only propria, aberta e
# fechada na hora. NUNCA segurar uma conexao aberta durante o download
# inteiro - o banco e journal_mode=delete e leitor com transacao pendurada
# faz o INSERT do ditado esperar (ver abrir_db).
LOTE_EXPORTACAO = 400

RE_DIA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Nomes PT para o cabecalho do arquivo exportado (sem locale, stdlib pura).
DIAS_PT = ("segunda-feira", "terca-feira", "quarta-feira", "quinta-feira",
           "sexta-feira", "sabado", "domingo")

# Preenchidos no main() (dependem da porta escolhida).
PORTA = PORTA_PADRAO
HOST_ESPERADO = "127.0.0.1:%d" % PORTA_PADRAO
ORIGEM_ESPERADA = "http://127.0.0.1:%d" % PORTA_PADRAO
MESTRE = ""
TOKEN_SESSAO = ""

CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

# Lista FIXA de estaticos. O caminho NUNCA vem da URL: e comparacao exata
# nesta tabela. (Foi reproduzido: SimpleHTTPRequestHandler serviu o
# history.db inteiro por symlink e ainda gerou listagem de diretorio.)
ESTATICOS = {
    "/":            ("interface.html", "text/html; charset=utf-8"),
    "/app.css":     ("app.css",        "text/css; charset=utf-8"),
    "/app.js":      ("app.js",         "application/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg",    "image/svg+xml"),
}

FAVICON_RESERVA = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#1d2733"/>'
    '<rect x="26" y="14" width="12" height="24" rx="6" fill="#7fd1c7"/>'
    '<path d="M20 34a12 12 0 0 0 24 0" fill="none" stroke="#7fd1c7" '
    'stroke-width="4" stroke-linecap="round"/>'
    '<path d="M32 46v6" stroke="#7fd1c7" stroke-width="4" '
    'stroke-linecap="round"/></svg>'
)

PAGINA_403 = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Painel do Ditado - sessao nao autorizada</title></head>
<body style="font:16px -apple-system,system-ui,sans-serif;padding:40px;
             max-width:560px;margin:0 auto;color:#1d2733">
<h1 style="font-size:20px">Sessao nao autorizada</h1>
<p>Esta janela nao tem uma sessao valida do painel.</p>
<p>Feche esta janela e abra o painel pelo atalho
<strong>PAINEL.bat</strong>, na pasta do Microfone. Ele cria a sessao
sozinho.</p>
<p style="color:#6b7785;font-size:14px">Nada foi alterado. O ditado continua
funcionando normalmente.</p>
</body></html>
"""

PAGINA_SEM_INTERFACE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>Painel do Ditado</title></head>
<body style="font:16px -apple-system,system-ui,sans-serif;padding:40px;
             max-width:560px;margin:0 auto;color:#1d2733">
<h1 style="font-size:20px">Servidor no ar, interface faltando</h1>
<p>O servidor do painel esta funcionando, mas o arquivo
<code>interface.html</code> nao foi encontrado na pasta
<code>painel/</code>.</p>
<p style="color:#6b7785;font-size:14px">As rotas de dados
(<code>/api/resumo</code>, <code>/api/historico</code>,
<code>/api/saude</code>, <code>/api/insights</code>,
<code>/api/exportar</code>) ja respondem.</p>
</body></html>
"""


# ---------------------------------------------------------------------
# Log do painel - boot, porta, erro e rota. NUNCA texto de ditado nem
# parametro de busca (a busca do usuário pode ser um sintoma de paciente).
# ---------------------------------------------------------------------

_trava_log = threading.Lock()


def registrar(mensagem):
    """Escreve uma linha no log do painel. Nunca levanta excecao."""
    linha = "[%s] %s\n" % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mensagem)
    try:
        with _trava_log:
            try:
                os.makedirs(dir_logs(), exist_ok=True)
            except OSError:
                pass
            # PORTE WINDOWS: a rotacao do Mac renomeava para .1. Aqui o arquivo
            # e TRUNCADO NO LUGAR: no Windows outro processo segurando o
            # arquivo (um tail, o Bloco de Notas) faz o os.replace levantar
            # PermissionError e o log pararia de crescer em silencio.
            try:
                if os.path.getsize(LOG_PAINEL) > LOG_MAX_B:
                    with open(LOG_PAINEL, "w", encoding="utf-8") as f:
                        f.write("[%s] (log truncado ao passar de 1 MB)\n"
                                % datetime.datetime.now().strftime(
                                    "%Y-%m-%d %H:%M:%S"))
            except OSError:
                pass
            # PORTE WINDOWS: o modo 0o600 do os.open e ignorado no NTFS (aqui
            # quem protege e a ACL). Mantido porque nao atrapalha; o
            # os.fchmod que vinha depois NAO EXISTE no Windows e saiu.
            fd = os.open(LOG_PAINEL,
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, linha.encode("utf-8", "replace"))
            finally:
                os.close(fd)
    except Exception:
        pass


# PORTE WINDOWS: nenhuma janela preta pisca quando uma sonda roda.
CREATE_NO_WINDOW = 0x08000000


def _restringir_acl(caminho):
    """PORTE WINDOWS: o analogo do `chmod 0600` do Mac.

    NTFS nao tem umask nem modo octal - quem protege arquivo aqui e a ACL.
    `icacls <arq> /inheritance:r /grant:r "%USERNAME%":(F)` corta a heranca
    e deixa so o usuario atual.
    Falha NAO aborta nada: LOGA e segue. Um painel que nao sobe e pior que um
    .mestre com a ACL herdada numa maquina de um usuario so.
    """
    usuario = os.environ.get("USERNAME") or ""
    if not usuario:
        registrar("acl: USERNAME vazio - .mestre ficou com a ACL herdada")
        return False
    try:
        p = subprocess.run(
            ["icacls", caminho, "/inheritance:r",
             "/grant:r", "%s:(F)" % usuario],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, timeout=10,
            text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        registrar("acl: icacls falhou (%s) - segui sem restringir"
                  % type(e).__name__)
        return False
    if p.returncode != 0:
        registrar("acl: icacls rc=%d - segui sem restringir" % p.returncode)
        return False
    return True


def ler_ou_criar_mestre():
    """PORTE WINDOWS: o segredo MESTRE mora em painel\\.mestre.

    No Mac era o PAINEL.command que criava o arquivo com `chmod 0600`. Aqui o
    painel cria sozinho, com secrets.token_urlsafe(32) e ACL restrita por
    icacls - assim apagar o .mestre nao quebra nada (nasce de novo).
    O conteudo NUNCA e logado. Se nao der pra gravar em disco, o painel ainda
    sobe com um segredo so de memoria: quem precisa do arquivo e o PAINEL.bat,
    e a falha e visivel la (ele nao consegue pedir o ticket).
    """
    try:
        with open(CAMINHO_MESTRE, "r", encoding="utf-8") as f:
            atual = f.read().strip()
        if atual:
            return atual
    except OSError:
        pass
    segredo = secrets.token_urlsafe(32)
    try:
        with open(CAMINHO_MESTRE, "w", encoding="utf-8") as f:
            f.write(segredo + "\n")
    except OSError as e:
        registrar("mestre: nao consegui gravar o .mestre (%s) - segui "
                  "so com o segredo em memoria" % type(e).__name__)
        return segredo
    _restringir_acl(CAMINHO_MESTRE)           # falha aqui so loga, nao aborta
    registrar("mestre: .mestre criado")
    return segredo


def _run(args, timeout=3.0, juntar_erro=False):
    """subprocess com timeout que NUNCA trava - copiado do guardiao.

    Nao usar subprocess.run: no TimeoutExpired ele faz kill()+wait() SEM
    limite, e um processo travado congelaria a rota /api/saude exatamente
    durante o incidente que ela existe para mostrar.
    errors='replace': a saida de schtasks/tasklist vem na codepage do console
    (pt-BR), NAO em UTF-8 - sem o replace um acento derrubaria a sonda.
    PORTE WINDOWS: start_new_session e killpg sao POSIX. Aqui e
    CREATE_NO_WINDOW (nenhuma janela pisca) e p.kill() no timeout.
    """
    try:
        p = subprocess.Popen(
            args, stdout=subprocess.PIPE,
            stderr=(subprocess.STDOUT if juntar_erro else subprocess.DEVNULL),
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)
    except Exception:
        return (-1, "")
    try:
        saida, _ = p.communicate(timeout=timeout)
        return (p.returncode, saida or "")
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:
            pass
        return (-2, "")
    except Exception:
        return (-1, "")


# =====================================================================
# 3. CAMADA DE DADOS (leitura read-only do history.db)
# =====================================================================

def abrir_db():
    """Conexao read-only NOVA. Uma por request, sempre fechada no finally.

    - uri=True e OBRIGATORIO: sem ele o sqlite trata a string inteira como
      NOME DE ARQUIVO e cria um arquivo chamado "file:...?mode=ro" no disco -
      exatamente a escrita que este painel nao pode fazer.
    - mode=ro puro. NADA de immutable=1 (ignora o journal e pode devolver
      lixo com o app.py gravando) nem nolock=1 (le pagina rasgada).
    - o banco esta em journal_mode=delete (nao WAL). Leitor pega lock
      compartilhado, entao a consulta tem que ser CURTA e a conexao fechada:
      conexao aberta com transacao pendurada faz o INSERT do ditado esperar.
    """
    uri = "file:" + CAMINHO_DB + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")     # cinto + suspensorio
    return con


ERROS_DB = (sqlite3.OperationalError, sqlite3.DatabaseError, sqlite3.Error)
# Casos reais que viram "estado normal", nunca 500:
#   unable to open database file            (db apagado - e reconstruivel)
#   no such table: transcripts              (db recriado vazio)
#   database is locked                      (escritor no meio do insert)
#   attempt to write a readonly database    (journal quente)
#   database disk image is malformed


# --- nome amigavel de app -------------------------------------------

NOMES_DE_APP = {
    "com.anthropic.claudefordesktop": "Claude Desktop",
    "com.apple.Terminal":             "Terminal",
    "com.mitchellh.ghostty":          "Ghostty",
    "com.google.Chrome":              "Chrome",
    "com.goodnotesapp.x":             "GoodNotes",
    "com.openai.codex":               "Codex",
    "com.apple.universalcontrol":     "Universal Control",
    # os 5 que existem no banco do usuário e nao estavam no mapa original
    # (sem eles o fallback devolveria "Textedit" e "Whatsapp"):
    "com.apple.finder":               "Finder",
    "com.apple.Preview":              "Pre-visualizacao",
    "com.apple.Notes":                "Notas",
    "com.apple.TextEdit":             "TextEdit",
    "net.whatsapp.WhatsApp":          "WhatsApp",
}

_mapa_pwa = {}


def carregar_mapa_pwa():
    """com.google.Chrome.app.<id> -> nome que o Chrome deu ao PWA.

    Sao QUATRO bundles de Chrome-app diferentes no banco (141 ditados) e
    todos cairiam no mesmo rotulo "Chrome (app)". Ler o Info.plist local
    (somente leitura) resolve o nome de verdade quando o PWA ainda existe.
    Roda UMA vez no boot; se falhar, o fallback "Chrome (app)" continua.
    """
    achados = {}
    padroes = [
        os.path.expanduser("~/Applications/Chrome Apps.localized/*.app"),
        os.path.expanduser("~/Applications/Chrome Apps/*.app"),
    ]
    for padrao in padroes:
        try:
            caminhos = glob.glob(padrao)
        except Exception:
            continue
        for caminho in caminhos:
            try:
                with open(os.path.join(caminho, "Contents", "Info.plist"),
                          "rb") as f:
                    dados = plistlib.load(f)
            except Exception:
                continue
            ident = dados.get("CFBundleIdentifier") or ""
            nome = (dados.get("CrAppModeShortcutName")
                    or dados.get("CFBundleName"))
            if ident.startswith("com.google.Chrome.app.") and nome:
                achados[ident] = nome
    return achados


def nome_do_app(bundle):
    """Bundle id -> nome legivel. Nunca devolve string vazia."""
    if not bundle or bundle == SENTINELA_VAZIO:
        return "(desconhecido)"
    if bundle in NOMES_DE_APP:
        return NOMES_DE_APP[bundle]
    if bundle in _mapa_pwa:
        return _mapa_pwa[bundle]
    if bundle.startswith("com.google.Chrome.app."):
        return "Chrome (app)"
    if bundle.startswith("com.google.Chrome"):
        return "Chrome"
    # PORTE WINDOWS: aqui o "bundle" e' o nome do executavel ("chrome.exe").
    # A regra do Mac (ultimo segmento depois do ponto) devolveria "Exe" para
    # todo mundo — tira a extensao antes.
    baixo = bundle.lower()
    if baixo.endswith(".exe"):
        base = bundle[:-4].rstrip(".")
        return (base[:1].upper() + base[1:]) if base else "(desconhecido)"
    ultimo = bundle.rstrip(".").split(".")[-1]
    if not ultimo:
        return "(desconhecido)"
    return ultimo[:1].upper() + ultimo[1:]


# --- busca: texto humano -> query FTS5 sempre valida -----------------

def _eh_caractere_de_token(ch):
    """Aproxima a regra do tokenizer unicode61: letra, numero, marca, '_'."""
    if ch == "_":
        return True
    try:
        return unicodedata.category(ch)[0] in ("L", "N", "M")
    except Exception:
        return False


def sanitizar_busca(entrada, prefixo=True):
    """Texto livre digitado por humano -> query FTS5 SEMPRE valida.

    Nenhum pedaco da sintaxe do FTS5 sobrevive: cada palavra vira uma frase
    entre aspas, que o FTS5 trata como literal. Pontuacao no meio da palavra
    (hifen, dois-pontos, barra, virgula) vira separador DENTRO da frase, o
    que da busca por adjacencia - exatamente o desejado:
      'pos-operatorio' -> "pos operatorio"   acha as duas grafias
      '91,3'           -> "91 3"             acha '91,3%'
    Sem isso, 46 entradas humanas testadas produziram 7 erros de sintaxe
    ("NEAR(a b, 2)", "col*", '"', "(", ...). Com isso: 0 erros.

    Devolve "" quando a entrada nao tem nada pesquisavel. Nesse caso o
    chamador deve devolver ZERO resultados - NUNCA a lista inteira.
    """
    if not entrada:
        return ""
    texto = unicodedata.normalize("NFC", entrada)
    termos = []
    atual = []
    for ch in texto:
        if _eh_caractere_de_token(ch):
            atual.append(ch)
        elif ch.isspace():
            if atual:
                termos.append("".join(atual))
                atual = []
        else:
            # pontuacao: quebra o token mas continua no mesmo termo (frase)
            if atual and atual[-1] != " ":
                atual.append(" ")
    if atual:
        termos.append("".join(atual))

    termos = [t.strip() for t in termos if t.strip()]
    if not termos:
        return ""

    # ruido de abreviacao: o 'c' de 'c/', o 'o' de 'o Dr.'. So descarta se
    # sobrar alguma coisa - assim buscar por 'a', '1' ou 'c/' continua valendo.
    uteis = [t for t in termos if len(t) > 1]
    if uteis:
        termos = uteis

    termos = termos[:32]          # trava contra colar um paragrafo inteiro

    partes = []
    ultimo = len(termos) - 1
    for i, termo in enumerate(termos):
        frase = '"' + termo.replace('"', '""') + '"'
        # '*' de prefixo SO no ultimo termo (o que ele ainda esta digitando),
        # so em palavra unica e so com 2+ caracteres: '"a"*' devolve 652 hits
        # contra 440 de '"a"', e em frase multi-token o '*' so afeta o ultimo
        # pedaco e confunde mais do que ajuda.
        if prefixo and i == ultimo and " " not in termo and len(termo) >= 2:
            frase += "*"
        partes.append(frase)
    return " ".join(partes)       # espaco entre frases = AND implicito


def marcadores():
    """Sentinelas unicas por consulta para o snippet() do FTS5.

    snippet() devolve o texto do ditado CRU, sem escapar HTML. Sentinela
    FIXA nao serve: um ditado que contenha os proprios bytes de controle
    produz <mark> desbalanceado (reproduzido). Com nonce aleatorio o texto
    ditado nao tem como falsificar a marca.
    """
    n = secrets.token_hex(8)
    return ("\x02A" + n + "\x02", "\x02F" + n + "\x02")


def destaque_para_html(bruto, abre, fecha):
    """snippet() cru -> HTML seguro com <mark>.

    ORDEM OBRIGATORIA: escapar PRIMEIRO, trocar sentinela por tag DEPOIS.
    Invertido, o html.escape comeria as proprias tags <mark> - e como hoje
    nao ha nenhum '<' nas 732 linhas, o bug ficaria invisivel por meses.
    """
    if bruto is None:
        return None
    seguro = html.escape(bruto, quote=True)
    return seguro.replace(abre, "<mark>").replace(fecha, "</mark>")


# --- SQL -------------------------------------------------------------

SQL_RESUMO = """
SELECT COUNT(*)                                                    AS total,
       COALESCE(SUM(duration_s), 0) / 60.0                         AS minutos_audio,
       MIN(timestamp)                                              AS ts_min,
       MAX(timestamp)                                              AS ts_max,
       SUM(CASE WHEN date(timestamp, 'unixepoch', 'localtime')
                     = date('now', 'localtime') THEN 1 ELSE 0 END) AS hoje,
       SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END)             AS sete_dias
  FROM transcripts
"""

SQL_APPS = """
SELECT app_bundle AS bundle, COUNT(*) AS n
  FROM transcripts
 GROUP BY app_bundle
 ORDER BY n DESC, app_bundle ASC
"""

SQL_DIAS = """
SELECT date(timestamp, 'unixepoch', 'localtime') AS dia,
       COUNT(*)                                  AS n,
       ROUND(SUM(duration_s) / 60.0, 1)          AS minutos
  FROM transcripts
 GROUP BY dia
 ORDER BY dia DESC
"""

COLUNAS = ("rowid AS id, timestamp, text, app_bundle, profile, "
           "duration_s, transcribe_s, polish")


def _clausulas(app, dia):
    """Monta o WHERE dos filtros. Fragmentos CONSTANTES, valores em ?."""
    partes = []
    valores = []
    if app:
        if app == SENTINELA_VAZIO:
            partes.append("app_bundle = ''")
        else:
            partes.append("app_bundle = ?")
            valores.append(app)
    if dia:
        partes.append("date(timestamp, 'unixepoch', 'localtime') = ?")
        valores.append(dia)
    if not partes:
        return ("", [])
    return (" AND " + " AND ".join(partes), valores)


def _formatar_item(linha, destaque):
    ts = float(linha["timestamp"] or 0)
    dt = datetime.datetime.fromtimestamp(ts)
    texto = linha["text"] or ""
    previa = texto[:240] + ("..." if len(texto) > 240 else "")
    bundle = linha["app_bundle"] or ""
    return {
        "id": int(linha["id"]),
        "ts": ts,
        "quando": dt.strftime("%d/%m %H:%M"),
        "dia": dt.strftime("%Y-%m-%d"),
        "texto": texto,                     # INTEGRAL e CRU: no JS, textContent
        "previa": previa,
        "destaque": destaque,               # unico campo com HTML (ja escapado)
        "app_bundle": bundle,
        "app_nome": nome_do_app(bundle),
        "perfil": linha["profile"] or "",
        "duracao_s": int(linha["duration_s"] or 0),
        "transcricao_s": round(float(linha["transcribe_s"] or 0), 2),
        "polish": linha["polish"] or "",
        "palavras": len(texto.split()),
    }


def dados_resumo():
    """GET /api/resumo - uma varredura so (0,37 ms medidos)."""
    try:
        con = abrir_db()
    except ERROS_DB:
        return {"total": 0, "hoje": 0, "sete_dias": 0, "minutos_audio": 0.0,
                "primeiro_dia": "", "ultimo_dia": ""}
    try:
        linha = con.execute(SQL_RESUMO, (time.time() - 7 * 86400,)).fetchone()
    except ERROS_DB:
        linha = None
    finally:
        con.close()

    if not linha or not (linha["total"] or 0):
        return {"total": 0, "hoje": 0, "sete_dias": 0, "minutos_audio": 0.0,
                "primeiro_dia": "", "ultimo_dia": ""}

    def dia_de(t):
        return datetime.datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d")

    return {
        "total": int(linha["total"]),
        "hoje": int(linha["hoje"] or 0),
        "sete_dias": int(linha["sete_dias"] or 0),
        "minutos_audio": round(float(linha["minutos_audio"] or 0), 1),
        "primeiro_dia": dia_de(linha["ts_min"]),
        "ultimo_dia": dia_de(linha["ts_max"]),
    }


def dados_apps():
    """GET /api/apps - lista para o filtro. Agrupa por BUNDLE, mostra o nome.

    Os 4 Chrome-apps tem bundles diferentes e o mesmo nome amigavel; se a
    interface agrupasse por NOME, escolher "Chrome (app)" filtraria so um
    deles e o usuário acharia que perdeu ditado.
    """
    try:
        con = abrir_db()
    except ERROS_DB:
        return []
    try:
        linhas = con.execute(SQL_APPS).fetchall()
    except ERROS_DB:
        return []
    finally:
        con.close()

    saida = []
    for linha in linhas:
        bundle = linha["bundle"] or ""
        # bundle vazio vira sentinela para poder ser filtrado na interface
        saida.append({
            "bundle": bundle if bundle else SENTINELA_VAZIO,
            "nome": nome_do_app(bundle),
            "n": int(linha["n"] or 0),
        })
    return saida


def dados_dias():
    """GET /api/dias - seletor de dia (extra do contrato, so leitura)."""
    try:
        con = abrir_db()
    except ERROS_DB:
        return []
    try:
        linhas = con.execute(SQL_DIAS).fetchall()
    except ERROS_DB:
        return []
    finally:
        con.close()
    return [{"dia": l["dia"] or "", "n": int(l["n"] or 0),
             "minutos": float(l["minutos"] or 0)} for l in linhas]


def dados_historico(q, pagina, por_pagina, dia, app):
    """GET /api/historico - listagem cronologica ou busca FTS5."""
    try:
        pagina = int(pagina or 1)
    except (TypeError, ValueError):
        pagina = 1
    try:
        por_pagina = int(por_pagina or 40)
    except (TypeError, ValueError):
        por_pagina = 40
    # TETO obrigatorio, nao so piso: Python tem inteiro infinito, o SQLite
    # nao. Um `?pagina=99999999999999999999` estourava o bind do OFFSET com
    # OverflowError (fora de ERROS_DB) e a rota virava 500. 10 milhoes de
    # paginas x 200 = offset de 2e9, folgado dentro do INT64 e MUITO alem
    # de qualquer historico real.
    pagina = max(1, min(pagina, 10 ** 7))
    por_pagina = max(1, min(200, por_pagina))
    salto = (pagina - 1) * por_pagina

    vazio = {"total": 0, "pagina": pagina, "por_pagina": por_pagina,
             "itens": []}

    consulta = (q or "").strip()
    fts = sanitizar_busca(consulta) if consulta else ""
    if consulta and not fts:
        # ele digitou algo ('***', '(', '---') que nao tem nada pesquisavel:
        # busca de verdade que nao achou nada. NAO cair na lista completa.
        return vazio

    onde, valores_filtro = _clausulas(app, dia)

    try:
        con = abrir_db()
    except ERROS_DB:
        return vazio
    try:
        if fts:
            abre, fecha = marcadores()
            sql_total = ("SELECT COUNT(*) FROM transcripts "
                         "WHERE text MATCH ?" + onde)
            total = con.execute(
                sql_total, [fts] + valores_filtro).fetchone()[0]
            sql = ("SELECT " + COLUNAS + ", "
                   "snippet(transcripts, 1, ?, ?, '...', 18) AS destaque "
                   "FROM transcripts WHERE text MATCH ?" + onde +
                   " ORDER BY timestamp DESC, rowid DESC LIMIT ? OFFSET ?")
            args = ([abre, fecha, fts] + valores_filtro
                    + [por_pagina, salto])
            linhas = con.execute(sql, args).fetchall()
            itens = [_formatar_item(l, destaque_para_html(l["destaque"],
                                                          abre, fecha))
                     for l in linhas]
        else:
            sql_total = "SELECT COUNT(*) FROM transcripts WHERE 1=1" + onde
            total = con.execute(sql_total, valores_filtro).fetchone()[0]
            # ', rowid DESC' nao e enfeite: e o desempate que mantem a
            # paginacao estavel se dois ditados cairem no mesmo timestamp.
            sql = ("SELECT " + COLUNAS + ", NULL AS destaque "
                   "FROM transcripts WHERE 1=1" + onde +
                   " ORDER BY timestamp DESC, rowid DESC LIMIT ? OFFSET ?")
            linhas = con.execute(
                sql, valores_filtro + [por_pagina, salto]).fetchall()
            itens = [_formatar_item(l, None) for l in linhas]
    except ERROS_DB:
        return vazio
    finally:
        con.close()

    return {"total": int(total or 0), "pagina": pagina,
            "por_pagina": por_pagina, "itens": itens}


# =====================================================================
# 3b. INSIGHTS (agregados do banco - nada inventado, tudo de SELECT)
# =====================================================================

SQL_INSIGHTS_TOTAIS = """
SELECT COUNT(*)                        AS total,
       COALESCE(SUM(duration_s), 0)   AS fala_total_s
  FROM transcripts
"""

SQL_INSIGHTS_DIAS = """
SELECT date(timestamp, 'unixepoch', 'localtime') AS dia,
       COUNT(*)                                  AS n
  FROM transcripts
 WHERE timestamp >= ?
 GROUP BY dia
"""

SQL_INSIGHTS_PERFIS = """
SELECT COALESCE(profile, '') AS perfil, COUNT(*) AS n
  FROM transcripts
 GROUP BY perfil
"""

SQL_INSIGHTS_HORAS = """
SELECT CAST(strftime('%H', timestamp, 'unixepoch', 'localtime') AS INTEGER)
           AS hora,
       COUNT(*) AS n
  FROM transcripts
 GROUP BY hora
"""


def _pct(n, total):
    """Percentual com 1 casa. total 0 nunca divide (licao do inf de swap)."""
    if not total:
        return 0.0
    return round(n * 100.0 / total, 1)


def dados_insights():
    """GET /api/insights - agregados de TODO o banco + janela de 30 dias.

    A contagem de palavras e feita em Python, linha a linha num cursor
    (len(texto.split())): a aproximacao em SQL via REPLACE erra com espaco
    duplo e nao conta quebra de linha como separador. O cursor NAO acumula
    as linhas em memoria - so o contador.
    """
    hoje = datetime.date.today()
    inicio_janela = hoje - datetime.timedelta(days=JANELA_INSIGHTS_DIAS - 1)
    corte_epoch = time.mktime(inicio_janela.timetuple())   # meia-noite local

    vazio = {
        "total": 0, "janela_dias": JANELA_INSIGHTS_DIAS,
        "dias": [], "dias_total": 0,
        "apps": [], "apps_outros": {"n": 0, "pct": 0.0},
        "perfis": [{"perfil": p, "n": 0, "pct": 0.0}
                   for p in PERFIS_CONHECIDOS],
        "fala_total_s": 0.0, "media_s": 0.0, "palavras": 0, "ppm": 0.0,
        "palavras_hoje": 0,
        "horas": [{"hora": h, "n": 0} for h in range(24)],
        "hora_pico": None,
        "gerado_em": datetime.datetime.now().strftime("%H:%M:%S"),
    }

    try:
        con = abrir_db()
    except ERROS_DB:
        return vazio
    try:
        totais = con.execute(SQL_INSIGHTS_TOTAIS).fetchone()
        total = int(totais["total"] or 0)
        fala_total_s = float(totais["fala_total_s"] or 0.0)

        por_dia = {}
        for l in con.execute(SQL_INSIGHTS_DIAS, (corte_epoch,)):
            if l["dia"]:
                por_dia[l["dia"]] = int(l["n"] or 0)

        linhas_apps = con.execute(SQL_APPS).fetchall()

        por_perfil = {}
        for l in con.execute(SQL_INSIGHTS_PERFIS):
            por_perfil[l["perfil"] or ""] = int(l["n"] or 0)

        por_hora = {}
        for l in con.execute(SQL_INSIGHTS_HORAS):
            if l["hora"] is not None:
                por_hora[int(l["hora"])] = int(l["n"] or 0)

        # palavras: total do banco + POR DIA na janela (estatistica diaria,
        # como o "Insights" do Wispr Flow). Dia = data local do timestamp.
        palavras = 0
        palavras_dia = {}
        for (ts, texto) in con.execute("SELECT timestamp, text FROM transcripts"):
            n_pal = len((texto or "").split())
            palavras += n_pal
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if ts >= corte_epoch:
                d = datetime.date.fromtimestamp(ts).strftime("%Y-%m-%d")
                palavras_dia[d] = palavras_dia.get(d, 0) + n_pal
    except ERROS_DB:
        return vazio
    finally:
        con.close()

    # ---- janela de 30 dias, com os dias SEM ditado presentes (n=0):
    # o grafico precisa das lacunas, senao "3 dias parado" vira invisivel.
    dias = []
    dias_total = 0
    for i in range(JANELA_INSIGHTS_DIAS):
        d = (inicio_janela + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        n = por_dia.get(d, 0)
        dias_total += n
        dias.append({"dia": d, "n": n, "palavras": palavras_dia.get(d, 0)})

    # ---- apps: top 10 + resto agrupado em "outros"
    apps = []
    outros_n = 0
    for i, l in enumerate(linhas_apps):
        bundle = l["bundle"] or ""
        n = int(l["n"] or 0)
        if i < 10:
            apps.append({
                "bundle": bundle if bundle else SENTINELA_VAZIO,
                "nome": nome_do_app(bundle),
                "n": n, "pct": _pct(n, total)})
        else:
            outros_n += n

    # ---- perfis: os 3 conhecidos SEMPRE (zero explicito) + extras do banco
    perfis = []
    vistos = set()
    for p in PERFIS_CONHECIDOS:
        n = por_perfil.get(p, 0)
        perfis.append({"perfil": p, "n": n, "pct": _pct(n, total)})
        vistos.add(p)
    for p in sorted(por_perfil):
        if p not in vistos:
            perfis.append({"perfil": p if p else "(sem perfil)",
                           "n": por_perfil[p],
                           "pct": _pct(por_perfil[p], total)})

    # ---- horas: as 24 sempre presentes; pico so quando ha dado
    horas = [{"hora": h, "n": por_hora.get(h, 0)} for h in range(24)]
    hora_pico = None
    if total > 0 and por_hora:
        melhor = max(sorted(por_hora), key=lambda h: por_hora[h])
        hora_pico = {"hora": melhor, "n": por_hora[melhor]}

    minutos_falados = fala_total_s / 60.0
    return {
        "total": total,
        "janela_dias": JANELA_INSIGHTS_DIAS,
        "dias": dias,
        "dias_total": dias_total,
        "apps": apps,
        "apps_outros": {"n": outros_n, "pct": _pct(outros_n, total)},
        "perfis": perfis,
        "fala_total_s": round(fala_total_s, 1),
        "media_s": round(fala_total_s / total, 1) if total else 0.0,
        "palavras": palavras,
        "palavras_hoje": palavras_dia.get(hoje.strftime("%Y-%m-%d"), 0),
        "ppm": round(palavras / minutos_falados, 1) if minutos_falados > 0
               else 0.0,
        "horas": horas,
        "hora_pico": hora_pico,
        "gerado_em": datetime.datetime.now().strftime("%H:%M:%S"),
    }


# =====================================================================
# 3c. EXPORTACAO (o history.db e janela de 30 dias - isto salva o resto)
# =====================================================================

def _mss(seg):
    """Duracao no formato M:SS - o mesmo do timer da pilula."""
    s = max(0, int(round(seg or 0)))
    return "%d:%02d" % (s // 60, s % 60)


def _rotulo_dia_pt(dt):
    """datetime -> 'terca-feira, 05/08/2026' (sem locale, sem acento)."""
    return "%s, %s" % (DIAS_PT[dt.weekday()], dt.strftime("%d/%m/%Y"))


_RE_MD_MARCADOR = re.compile(r"^(\s{0,3})([#>*+`~=-])")
_RE_MD_LISTA_NUM = re.compile(r"^(\s{0,3}\d{1,9})([.)])(?=\s|$)")


def _escapar_linha_md(linha):
    """Neutraliza inicio de linha que viraria ESTRUTURA de Markdown.

    O texto ditado e prosa, mas uma linha comecando com '#', '-', '>' ou
    '```' quebraria o documento inteiro (viraria titulo, lista, citacao ou
    abriria um bloco de codigo que engole as secoes seguintes). A barra
    invertida escapa qualquer pontuacao ASCII no CommonMark; para lista
    numerada ('1. foo') a barra vai antes do PONTO, nao antes do digito.
    So o inicio de linha e tratado: enfase no meio da frase nao quebra a
    estrutura e mexer nela mudaria o texto do usuário mais do que protege.
    """
    m = _RE_MD_MARCADOR.match(linha)
    if m:
        i = m.end(1)
        return linha[:i] + "\\" + linha[i:]
    m = _RE_MD_LISTA_NUM.match(linha)
    if m:
        i = m.end(1)
        return linha[:i] + "\\" + linha[i:]
    return linha


def _texto_md(texto):
    return "\n".join(_escapar_linha_md(l) for l in (texto or "").split("\n"))


def _sql_exportacao(fts, onde):
    """SQL do lote (keyset por timestamp+rowid, ascendente).

    COALESCE(timestamp,0) de proposito: um timestamp NULL cairia fora de
    'timestamp > ?' (NULL > x e NULL) e o ditado sumiria do arquivo em
    silencio. O banco nao tem indice por timestamp de qualquer forma
    (coluna UNINDEXED do FTS5), entao o COALESCE nao perde indice nenhum.
    OFFSET foi descartado: OFFSET grande relê tudo de novo a cada lote.
    """
    marcha = (" AND (COALESCE(timestamp,0) > ? OR "
              "(COALESCE(timestamp,0) = ? AND rowid > ?))")
    ordem = " ORDER BY COALESCE(timestamp,0) ASC, rowid ASC LIMIT ?"
    if fts:
        return ("SELECT " + COLUNAS + " FROM transcripts WHERE text MATCH ?"
                + onde + marcha + ordem)
    return ("SELECT " + COLUNAS + " FROM transcripts WHERE 1=1"
            + onde + marcha + ordem)


def gerar_exportacao(formato, fts, consulta, onde, valores_filtro,
                     dia, app, forcar_vazio):
    """Gera o arquivo de exportacao em PEDACOS (generator de bytes).

    Nada de montar o arquivo inteiro em RAM: hoje sao 732 ditados, mas o
    teto nao e garantido. Cada lote de LOTE_EXPORTACAO linhas abre a
    propria conexao read-only, le, fecha e vira um pedaco de resposta.
    Se o banco falhar NO MEIO, o arquivo termina com um aviso explicito -
    truncamento silencioso pareceria um backup completo sem ser.
    """
    eh_md = (formato == "md")
    agora = datetime.datetime.now()

    # ---- 1) numeros do cabecalho (conexao curta propria) ----
    total = 0
    ts_min = ts_max = None
    fala_s = 0.0
    erro_db = False
    if not forcar_vazio:
        try:
            con = abrir_db()
            try:
                if fts:
                    sql = ("SELECT COUNT(*) AS n, MIN(timestamp) AS a, "
                           "MAX(timestamp) AS b, COALESCE(SUM(duration_s),0)"
                           " AS d FROM transcripts WHERE text MATCH ?" + onde)
                    linha = con.execute(sql,
                                        [fts] + valores_filtro).fetchone()
                else:
                    sql = ("SELECT COUNT(*) AS n, MIN(timestamp) AS a, "
                           "MAX(timestamp) AS b, COALESCE(SUM(duration_s),0)"
                           " AS d FROM transcripts WHERE 1=1" + onde)
                    linha = con.execute(sql, valores_filtro).fetchone()
            finally:
                con.close()
            total = int(linha["n"] or 0)
            ts_min, ts_max = linha["a"], linha["b"]
            fala_s = float(linha["d"] or 0.0)
        except ERROS_DB:
            erro_db = True

    if ts_min is not None and ts_max is not None:
        periodo = "%s a %s" % (
            datetime.datetime.fromtimestamp(float(ts_min)).strftime("%d/%m/%Y"),
            datetime.datetime.fromtimestamp(float(ts_max)).strftime("%d/%m/%Y"))
    else:
        periodo = "-"

    filtros = []
    if consulta:
        filtros.append('busca "%s"' % consulta)
    if dia:
        filtros.append("dia %s" % dia)
    if app:
        filtros.append("app %s" % (nome_do_app(app)
                                   if app != SENTINELA_VAZIO
                                   else "(desconhecido)"))
    rotulo_filtros = ", ".join(filtros) if filtros else "nenhum (tudo)"

    # ---- 2) cabecalho ----
    partes = []
    if eh_md:
        partes.append("# Historico de ditados - Botao Wispr\n\n")
        partes.append("Exportado em %s pelo Painel do Ditado, direto do "
                      "history.db local.\n\n" % agora.strftime("%d/%m/%Y %H:%M"))
        partes.append("- Periodo: %s\n" % periodo)
        partes.append("- Total: %d %s (%s falados)\n"
                      % (total, "ditado" if total == 1 else "ditados",
                         _mss(fala_s)))
        partes.append("- Filtros: %s\n" % rotulo_filtros)
    else:
        partes.append("HISTORICO DE DITADOS - BOTAO WISPR\n")
        partes.append("=" * 50 + "\n")
        partes.append("Exportado em %s\n" % agora.strftime("%d/%m/%Y %H:%M"))
        partes.append("Periodo: %s\n" % periodo)
        partes.append("Total: %d %s (%s falados)\n"
                      % (total, "ditado" if total == 1 else "ditados",
                         _mss(fala_s)))
        partes.append("Filtros: %s\n" % rotulo_filtros)
    if forcar_vazio:
        partes.append("\nA busca digitada nao tinha nada pesquisavel - "
                      "nenhum ditado corresponde.\n")
    if erro_db:
        partes.append("\nNao consegui ler o banco de ditados agora. "
                      "Tente exportar de novo.\n")
    yield "".join(partes).encode("utf-8")
    if forcar_vazio or erro_db:
        return

    # ---- 3) lotes ----
    sql = _sql_exportacao(fts, onde)
    ultimo_ts = -1.0
    ultimo_id = -1
    dia_atual = None
    while True:
        args = ([fts] if fts else []) + valores_filtro + \
            [ultimo_ts, ultimo_ts, ultimo_id, LOTE_EXPORTACAO]
        try:
            con = abrir_db()
            try:
                linhas = con.execute(sql, args).fetchall()
            finally:
                con.close()
        except ERROS_DB:
            yield ("\n\n[exportacao INTERROMPIDA: o banco parou de "
                   "responder no meio - o arquivo esta INCOMPLETO. "
                   "Tente de novo.]\n").encode("utf-8")
            return
        if not linhas:
            return

        partes = []
        for l in linhas:
            ts = float(l["timestamp"] or 0)
            ultimo_ts = ts
            ultimo_id = int(l["id"])
            dt = datetime.datetime.fromtimestamp(ts)
            chave_dia = dt.strftime("%Y-%m-%d")
            texto = l["text"] or ""
            nome_app = nome_do_app(l["app_bundle"] or "")
            if eh_md:
                if chave_dia != dia_atual:
                    partes.append("\n## %s\n" % _rotulo_dia_pt(dt))
                    dia_atual = chave_dia
                partes.append("\n### %s - %s (%s)\n\n"
                              % (dt.strftime("%H:%M"), nome_app,
                                 _mss(l["duration_s"])))
                partes.append(_texto_md(texto))
                partes.append("\n")
            else:
                if chave_dia != dia_atual:
                    partes.append("\n" + "-" * 50 + "\n")
                    partes.append(_rotulo_dia_pt(dt) + "\n")
                    partes.append("-" * 50 + "\n")
                    dia_atual = chave_dia
                partes.append("\n[%s] %s (%s)\n"
                              % (dt.strftime("%H:%M"), nome_app,
                                 _mss(l["duration_s"])))
                partes.append(texto)
                partes.append("\n")
        yield "".join(partes).encode("utf-8")
        if len(linhas) < LOTE_EXPORTACAO:
            return


# =====================================================================
# 4. CAMADA DE SAUDE (sondas isoladas, cada uma com timeout proprio)
# =====================================================================

# PORTE WINDOWS: os 6 LaunchAgents viram as 4 tarefas do Agendador.
# tarefa -> (rotulo PT-BR, residente?)
#   residente=True  -> a tarefa segura um processo o tempo todo; "Pronto"
#                      (sem processo) e problema, igual ao "parado" do Mac.
#   residente=False -> a tarefa REPETE de tempos em tempos e fica "Pronto"
#                      entre um ciclo e outro; isso e SAUDAVEL (e o papel que
#                      o warmup one-shot tinha no Mac). Quem diz se o
#                      aquecedor esta realmente aquecendo e o keepwarm.log.
# Os valores abaixo vem do GATILHO real de cada tarefa, em
# recursos/tarefas.py: App e Servidor sao ONLOGON (processo de pe o tempo
# todo); Guardiao (/SC MINUTE /MO 2) e KeepWarm (/SC MINUTE /MO 3) REPETEM e
# passam a maior parte do tempo em "Pronto". Marcar os dois ultimos como
# residentes deixaria a tela de saude vermelha quase sempre, de mentira.
AGENTES = [
    (TAREFA_APP,      "Painel do ditado", True),
    (TAREFA_SERVIDOR, "Motor de transcricao (whisper-server)", True),
    (TAREFA_KEEPWARM, "Aquecedor do motor (ping 150s)", False),
    (TAREFA_GUARDIAO, "Guardiao de memoria", False),
]

# PORTE WINDOWS: o schtasks e LOCALIZADO (pt-BR: "Em execucao"/"Pronto"/
# "Desabilitado") e a saida vem na codepage do console, lida com
# errors='replace'. Por isso a deteccao e por TRECHO de palavra que sobrevive
# ao replace ("execu", "pronto", "desabilit") e a chave da linha e comparada
# so em ASCII ("status"). Nada aqui pode virar excecao.
_CHAVES_STATUS = ("status", "situacao")


def _status_da_tarefa(saida):
    """Saida do `schtasks /FO LIST` -> 'rodando' | 'pronto' | 'desabilitada'
    | None (nao reconhecido)."""
    for linha in saida.splitlines():
        chave, sep, valor = linha.partition(":")
        if not sep:
            continue
        # a chave e comparada sem acento e sem caixa; "Hora da proxima
        # execucao" tem 'execu' na CHAVE e nao pode ser confundida com o
        # Status - por isso a comparacao e da chave inteira, nao 'in'.
        chave_limpa = "".join(
            c for c in unicodedata.normalize("NFD", chave.strip().lower())
            if unicodedata.category(c) != "Mn")
        if chave_limpa not in _CHAVES_STATUS:
            continue
        v = valor.strip().lower()
        if "execu" in v or "running" in v:
            return "rodando"
        if "pronto" in v or "ready" in v:
            return "pronto"
        if "desabilit" in v or "disabled" in v:
            return "desabilitada"
        return None
    return None


def sonda_agentes():
    """PORTE WINDOWS: `schtasks /Query /TN <nome> /FO LIST` por tarefa.

    Uma chamada por tarefa (4 no total, timeout de 8s cada) porque o /TN
    filtra no proprio schtasks - listar TUDO traria centenas de tarefas do
    sistema para peneirar aqui.

    REGRA QUE NAO PODE SE PERDER: sonda que FALHOU nao e vermelho.
    schtasks ausente, timeout ou acesso negado = "desconhecido" com ok=True
    (cinza). So "tarefa nao existe" e vermelho, porque isso e um fato.
    """
    itens = []
    for tarefa, rotulo, residente in AGENTES:
        rc, saida = _run(["schtasks", "/Query", "/TN", tarefa, "/FO", "LIST"],
                         timeout=8.0, juntar_erro=True)
        base = {"rotulo": rotulo, "label": tarefa, "pid": None}

        if rc < 0:
            # -1 = schtasks nem rodou; -2 = estourou o timeout. Sem sinal,
            # nao acusa (foi tratar "sem resposta" como "parado" que gerou
            # alarme falso em agente saudavel no Mac).
            base.update({"estado": "desconhecido", "ok": True,
                         "detalhe": "schtasks nao respondeu (sem leitura)"})
            itens.append(base)
            continue

        baixa = saida.lower()
        if rc != 0:
            if "negado" in baixa or "denied" in baixa or "acesso" in baixa:
                base.update({"estado": "desconhecido", "ok": True,
                             "detalhe": "sem permissao para consultar "
                                        "(sem leitura)"})
            else:
                base.update({"estado": "descarregado", "ok": False,
                             "detalhe": "a tarefa nao existe no Agendador"})
            itens.append(base)
            continue

        estado = _status_da_tarefa(saida)
        if estado == "rodando":
            base.update({"estado": "rodando", "ok": True,
                         "detalhe": "tarefa em execucao"})
        elif estado == "desabilitada":
            base.update({"estado": "parado", "ok": False,
                         "detalhe": "tarefa DESABILITADA no Agendador"})
        elif estado == "pronto":
            if residente:
                base.update({"estado": "parado", "ok": False,
                             "detalhe": "cadastrada, mas sem processo agora "
                                        "(Pronto)"})
            else:
                base.update({"estado": "sob demanda", "ok": True,
                             "detalhe": "cadastrada, aguardando o proximo "
                                        "ciclo (Pronto)"})
        else:
            base.update({"estado": "desconhecido", "ok": True,
                         "detalhe": "status nao reconhecido (sem leitura)"})
        itens.append(base)
    return itens


def sonda_server():
    """GET / na 7777. NUNCA POST /inference: roubaria GPU do ditado."""
    inicio = time.time()
    try:
        req = urllib.request.Request(URL_MOTOR, method="GET")
        resp = urllib.request.urlopen(req, timeout=1.5)
        try:
            resp.read(1024)                   # drena um pedaco, nao o body
        finally:
            resp.close()
        ms = round((time.time() - inicio) * 1000, 1)
        return {"ok": True, "http": resp.status, "ms": ms,
                "detalhe": "respondeu em %sms" % ms}
    except urllib.error.HTTPError as e:
        # 404/500 = processo VIVO atendendo socket, so a rota mudou.
        ms = round((time.time() - inicio) * 1000, 1)
        return {"ok": True, "http": e.code, "ms": ms,
                "detalhe": "respondeu HTTP %d em %sms (vivo)" % (e.code, ms)}
    except Exception as e:
        ms = round((time.time() - inicio) * 1000, 1)
        return {"ok": False, "http": None, "ms": ms,
                "detalhe": "sem resposta na porta 7777 (%s)"
                           % type(e).__name__}


def _contar_imagem(imagem):
    """`tasklist /FI "IMAGENAME eq <img>" /FO CSV` -> quantos processos.

    Devolve None quando NAO DEU PRA SABER (tasklist ausente, timeout, filtro
    recusado) - None e diferente de 0, e a rota inteira depende disso.
    Filtro sem nenhum processo: o tasklist imprime 'INFO: ...' e sai com
    rc 0, entao 0 e uma resposta legitima, nao um erro.
    """
    rc, saida = _run(["tasklist", "/FI", "IMAGENAME eq %s" % imagem,
                      "/FO", "CSV"], timeout=8.0)
    if rc != 0:
        return None
    n = 0
    for linha in saida.splitlines():
        linha = linha.strip()
        # linhas de dado comecam com o nome da imagem entre aspas; o
        # cabecalho vem como "Nome da imagem"/"Image Name" e e descartado
        # pela mesma comparacao.
        if linha.lower().startswith('"%s"' % imagem.lower()):
            n += 1
    return n


def sonda_processos():
    """PORTE WINDOWS: substitui a sonda de Core ML (que nao existe aqui).

    `pgrep` do Mac vira `tasklist`. E uma sonda INFORMATIVA: ela conta os
    interpretadores Python vivos (o proprio painel e um deles, e isso esta
    dito no detalhe). Quem diz se o ditado esta de pe e a sonda de tarefas;
    esta aqui nunca fica vermelha por conta propria - so cinza quando o
    tasklist nao responde.
    """
    n_py = _contar_imagem("python.exe")
    n_pyw = _contar_imagem("pythonw.exe")
    if n_py is None and n_pyw is None:
        return {"ok": False,
                "detalhe": "tasklist nao respondeu (sem leitura)"}
    a = n_py or 0
    b = n_pyw or 0
    return {"ok": True,
            "python": a, "pythonw": b,
            "detalhe": "%d python.exe (o painel e um deles) e %d pythonw.exe"
                       % (a, b)}


def _ultimas_linhas(caminho, n=5, teto_bytes=262144):
    """Ultimas n linhas nao vazias, lendo so o rabo do arquivo."""
    try:
        tam = os.path.getsize(caminho)
    except OSError:
        return None
    try:
        with open(caminho, "rb") as f:
            f.seek(max(0, tam - teto_bytes))
            dados = f.read()
    except OSError:
        return None
    linhas = dados.decode("utf-8", "replace").splitlines()
    if tam > teto_bytes and linhas:
        linhas = linhas[1:]                   # a 1a veio cortada no meio
    return [l for l in linhas if l.strip()][-n:]


def _idade_e_tamanho(caminho):
    """PORTE WINDOWS: (segundos desde o mtime, tamanho em bytes) ou (None, None).

    E a leitura que o porte usa para OS TRES logs do ditado
    (ditado-mic.log, keepwarm.log, guardiao.log): mtime + tamanho, como no
    Mac. Arquivo ausente devolve (None, None) - "nao sei", nao "quebrado".
    """
    try:
        st = os.stat(caminho)
    except OSError:
        return (None, None)
    try:
        return (int(time.time() - st.st_mtime), int(st.st_size))
    except (TypeError, ValueError, OSError):
        return (None, int(st.st_size))


def _kb(n):
    if n is None:
        return "?"
    if n < 1024:
        return "%d B" % n
    return "%d KB" % (n // 1024)


def segundos_desde_boot():
    """PORTE WINDOWS: o analogo do `sysctl kern.waketime` do Mac.

    GetTickCount64 devolve milissegundos desde o boot. Nao existe no Windows
    um "segundos desde o ultimo wake" barato e confiavel, entao a guarda usa
    BOOT: maquina que acabou de subir ainda nao teve tempo de o aquecedor
    rodar o primeiro ciclo, e isso NAO e defeito. Falhou = None ("nao sei").
    """
    try:
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return None


def sonda_keepwarm():
    """PORTE WINDOWS: o Mac parseava o TEXTO do keepwarm.log; aqui e so
    mtime + tamanho, como manda o porte. O log do aquecedor do Windows ainda
    nao tem contrato de texto com ninguem - inventar um aqui seria criar uma
    segunda fonte de verdade."""
    ha_s, tam = _idade_e_tamanho(LOG_KEEPWARM)
    if ha_s is None:
        # ausente = NAO SEI. A frase carrega "sem leitura" de proposito: e o
        # que faz a interface pintar cinza em vez de vermelho.
        return {"ok": False, "ultima_linha": "", "ha_s": None,
                "detalhe": "sem leitura: o log do aquecedor ainda nao existe"}

    if ha_s <= KEEPWARM_PARADO_S:
        return {"ok": True, "ultima_linha": "", "ha_s": ha_s,
                "detalhe": "Aquecedor ativo (%s no log)" % _kb(tam)}

    # GUARDA DE BOOT: maquina recem-ligada com log velho e NORMAL - e e
    # exatamente quando o usuário abre o painel. Sem esta guarda o painel
    # repete os 9 alarmes falsos de 20/07 do Mac.
    uptime = segundos_desde_boot()
    if uptime is not None and uptime < JANELA_WAKE_S:
        return {"ok": True, "ultima_linha": "", "ha_s": ha_s,
                "detalhe": ("Windows subiu ha %ds - o proximo ping vem em "
                            "ate 150s (nao e defeito)" % int(uptime))}
    return {"ok": False, "ultima_linha": "", "ha_s": ha_s,
            "detalhe": ("Aquecedor parado ha %dmin (esperado a cada 2,5min)"
                        % (ha_s // 60))}


def sonda_log_ditado():
    """PORTE WINDOWS: o terceiro log (ditado-mic.log), mtime + tamanho.

    Entra na lista de agentes como uma linha INFORMATIVA. Nunca fica
    vermelha: log velho so quer dizer que ele nao ditou hoje, nao que algo
    quebrou.
    """
    ha_s, tam = _idade_e_tamanho(LOG_APP)
    base = {"rotulo": "Registro do ditado (log)", "label": "log-ditado-mic",
            "pid": None}
    if ha_s is None:
        base.update({"estado": "desconhecido", "ok": True,
                     "detalhe": "sem leitura: o log do ditado ainda "
                                "nao existe"})
        return base
    if ha_s < 3600:
        quando = "ha %d min" % (ha_s // 60)
    else:
        quando = "ha %d h" % (ha_s // 3600)
    base.update({"estado": "rodando", "ok": True,
                 "detalhe": "escrito %s - %s" % (quando, _kb(tam))})
    return base


class MEMORYSTATUSEX(ctypes.Structure):
    """PORTE WINDOWS: o struct do GlobalMemoryStatusEx.

    Substitui o `sysctl vm.swapusage` do Mac. NAO se usa `wmic` aqui: ele
    esta sendo removido do Windows e abriria um processo por leitura.
    ctypes puro, zero dependencia nova.
    dwLength TEM que ser preenchido antes da chamada, senao a API recusa.
    """
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def sonda_swap():
    """PORTE WINDOWS: memoria pelo GlobalMemoryStatusEx.

    O que o painel chama de "swap" aqui e o LIMITE DE CONFIRMACAO
    (ullTotalPageFile), que e o analogo pratico do swap do Mac: e ele que
    acaba antes de tudo travar. ullAvailPhys entra no detalhe - foi falta de
    RAM que travou a maquina em 14/07/2026.
    Sonda que falhou devolve ok=True com "sem leitura" (mesmo contrato do
    Mac quando o sysctl nao respondia): "nao sei" nao e "quebrado".
    """
    zerado = {"total_mb": 0.0, "usado_mb": 0.0, "livre_mb": 0.0,
              "pct": 0.0, "ok": True}
    try:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            zerado["detalhe"] = "sem leitura: GlobalMemoryStatusEx recusou"
            return zerado
    except Exception:
        zerado["detalhe"] = "sem leitura: GlobalMemoryStatusEx indisponivel"
        return zerado

    mb = 1024.0 * 1024.0
    total = st.ullTotalPageFile / mb
    livre = st.ullAvailPageFile / mb
    usado = max(0.0, total - livre)
    ram_livre = st.ullAvailPhys / mb

    # Mesma armadilha do Mac (incidentes de 04/08): total 0 dava divisao por
    # zero e virava "infMB livres". total 0 nao e defeito.
    pct = round((usado / total) * 100.0, 1) if total > 0 else 0.0
    ok = True
    detalhe = "RAM livre: %d MB" % int(ram_livre)
    if total <= 0:
        detalhe = "sem limite de confirmacao lido - " + detalhe
    elif livre < SWAP_LIVRE_MIN_MB or pct >= 90.0:
        ok = False
    return {"total_mb": round(total, 1), "usado_mb": round(usado, 1),
            "livre_mb": round(livre, 1), "pct": pct, "ok": ok,
            "ram_livre_mb": round(ram_livre, 1),
            "detalhe": detalhe}


# O log de incidentes tem DUAS variantes de linha e a documentada e a
# MINORIA: 152 linhas tem '[tipo]', 194 NAO tem (so o que passa por
# notificar() ganha colchete). Grupo do tipo OPCIONAL e ancorado logo apos o
# carimbo - assim 'pids=[870, 929]' nunca vira tipo.
# PORTE WINDOWS: o carimbo entre colchetes e o do Mac; o log do guardiao
# daqui escreve "YYYY-MM-DD HH:MM:SS msg" (sem colchete), como o
# recursos/log.py do app. Os dois formatos sao aceitos - linha que nao casa
# com nenhum continua entrando inteira como mensagem, nunca some.
RE_INCIDENTE = re.compile(
    r"^\[?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]?\s*"
    r"(?:\[([^\]]+)\]\s*)?(.*)$")


def sonda_incidentes(n=20):
    """Ultimos n incidentes, mais novo primeiro.

    INCIDENTES SAO HISTORICO, NUNCA ALERTA. O log acumula desde julho (185
    kickstart, 123 mem_aviso); se qualquer um virasse alerta, o painel
    nasceria vermelho para sempre.
    """
    linhas = _ultimas_linhas(LOG_INCIDENTES, n=n)
    if not linhas:
        return []
    itens = []
    for linha in reversed(linhas):
        m = RE_INCIDENTE.match(linha)
        if not m:
            itens.append({"quando": "", "tipo": "outro",
                          "mensagem": linha.strip()})
            continue
        quando, tipo, msg = m.group(1), m.group(2), m.group(3)
        if not tipo:
            bruto = msg.strip()
            if bruto.startswith("app:"):
                tipo = "app"
            elif bruto.startswith("MATANDO"):
                tipo = "kill"
            elif bruto.startswith("kickstart"):
                tipo = "kickstart"
            elif bruto.startswith("bootstrap"):
                tipo = "bootstrap"
            else:
                tipo = "outro"
        itens.append({"quando": quando, "tipo": tipo,
                      "mensagem": msg.strip()})
    return itens


def dados_saude():
    """GET /api/saude - as sondas EM PARALELO.

    Em serie o pior caso soma dezenas de segundos (4 schtasks de 8s + o
    server de 1,5s + o tasklist). E o pior caso e justamente o mais provavel
    de o usuário abrir esta tela: whisper-server travado aceita TCP e nunca
    responde, custando o timeout inteiro.
    PORTE WINDOWS: o teto do result() subiu de 5s para 20s porque schtasks e
    tasklist sao mais lentos que launchctl/pgrep (8s de timeout cada, e a
    sonda de tarefas faz 4 chamadas em sequencia dentro da propria thread).
    """
    resultado = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futuros = {
            "agentes":    executor.submit(sonda_agentes),
            "server":     executor.submit(sonda_server),
            "processos":  executor.submit(sonda_processos),
            "keepwarm":   executor.submit(sonda_keepwarm),
            "swap":       executor.submit(sonda_swap),
            "incidentes": executor.submit(sonda_incidentes),
        }
        for chave, futuro in futuros.items():
            try:
                resultado[chave] = futuro.result(timeout=20.0)
            except Exception as e:
                resultado[chave] = None
                registrar("sonda %s falhou: %s" % (chave, type(e).__name__))

    agentes = resultado.get("agentes")
    if not isinstance(agentes, list):
        agentes = [{"rotulo": r, "label": l, "pid": None,
                    "estado": "desconhecido", "ok": True,
                    "detalhe": "sonda nao respondeu"}
                   for l, r, _ in AGENTES]
    # PORTE WINDOWS: o terceiro log entra como linha informativa da lista.
    try:
        agentes = list(agentes) + [sonda_log_ditado()]
    except Exception:
        pass

    def bloco(chave, reserva):
        valor = resultado.get(chave)
        if not isinstance(valor, dict):
            return reserva
        return valor

    server = bloco("server", {"ok": False, "http": None, "ms": None,
                              "detalhe": "sonda nao respondeu"})
    processos = bloco("processos", {"ok": False,
                                    "detalhe": "sonda nao respondeu"})
    keepwarm = bloco("keepwarm", {"ok": False, "ultima_linha": "",
                                  "ha_s": None,
                                  "detalhe": "sonda nao respondeu"})
    swap = bloco("swap", {"total_mb": 0.0, "usado_mb": 0.0, "livre_mb": 0.0,
                          "pct": 0.0, "ok": True,
                          "detalhe": "sonda nao respondeu"})
    incidentes = resultado.get("incidentes")
    if not isinstance(incidentes, list):
        incidentes = []

    # ordem = gravidade decrescente na interface
    alertas = []
    for agente in agentes:
        if not agente.get("ok"):
            alertas.append("%s: %s" % (agente.get("rotulo", "?"),
                                       agente.get("detalhe", "")))
    if not server.get("ok"):
        alertas.append("Motor de transcricao fora do ar - "
                       "o ditado NAO vai funcionar.")
    if not processos.get("ok"):
        alertas.append("Processos: %s" % processos.get("detalhe", ""))
    if not keepwarm.get("ok"):
        alertas.append("Aquecedor: %s" % keepwarm.get("detalhe", ""))
    if not swap.get("ok"):
        alertas.append("Memoria apertada: so %.0fMB de swap livre "
                       "(%.0f%% usado)." % (swap.get("livre_mb", 0.0),
                                            swap.get("pct", 0.0)))

    # ok GLOBAL = lista de alertas vazia. Nada mais: se houver segunda fonte
    # de verdade, painel e alertas divergem.
    return {"ok": len(alertas) == 0,
            "alertas": alertas,
            "agentes": agentes,
            "server": server,
            "processos": processos,
            "keepwarm": keepwarm,
            "swap": swap,
            "incidentes": incidentes,
            "gerado_em": datetime.datetime.now().strftime("%H:%M:%S")}


def reiniciar_motor():
    """UNICA acao do painel no sistema.

    PORTE WINDOWS: `launchctl kickstart -k` vira `schtasks /End` seguido de
    `schtasks /Run`. Os DOIS passos sao obrigatorios e nessa ordem: /Run
    sozinho em tarefa ja rodando e no-op SILENCIOSO (mesma armadilha do
    kickstart sem -k), e o painel diria "reiniciado" sem ter reiniciado nada.
    O /End em tarefa que ja esta parada devolve rc != 0 e isso e NORMAL - so
    o /Run decide o resultado.
    O nome da tarefa e CONSTANTE aqui; JAMAIS aceitar nome vindo da request.
    E o painel NUNCA toca na tarefa do APP do ditado: reiniciar aquilo
    derrubaria o gancho de teclado do usuário.
    """
    rc_end, _ = _run(["schtasks", "/End", "/TN", TAREFA_SERVIDOR],
                     timeout=10.0, juntar_erro=True)
    rc_run, saida = _run(["schtasks", "/Run", "/TN", TAREFA_SERVIDOR],
                         timeout=10.0, juntar_erro=True)
    if rc_run == 0:
        registrar("acao: schtasks /End+/Run %s (end rc=%d, run rc=0)"
                  % (TAREFA_SERVIDOR, rc_end))
        return {"ok": True,
                "mensagem": ("Motor de transcricao reiniciado. A primeira "
                             "ditada pode levar alguns segundos ate o modelo "
                             "reaquecer.")}
    registrar("acao: schtasks /Run %s FALHOU (rc=%d)"
              % (TAREFA_SERVIDOR, rc_run))
    return {"ok": False,
            "mensagem": "Nao consegui reiniciar (schtasks rc=%d)." % rc_run}


# =====================================================================
# 5. CAMADA HTTP (blindagem, sessao, rotas)
# =====================================================================

_tickets = {}
_trava_ticket = threading.Lock()
_ultimo_acesso = [time.time()]


def novo_ticket():
    """Ticket descartavel de 90s, UM uso so.

    E o unico segredo que aparece na URL, e URL de --app= VAZA em `ps`
    (reproduzido: '--app=http://127.0.0.1:7911/?t=...' visivel para qualquer
    processo). Por isso ele e curto, one-shot e trocado por cookie no ato.
    """
    t = secrets.token_urlsafe(24)
    agora = time.time()
    with _trava_ticket:
        for chave in list(_tickets):
            if _tickets[chave] < agora:
                del _tickets[chave]
        _tickets[t] = agora + VALIDADE_TICKET
    return t


def queimar_ticket(t):
    if not t:
        return False
    with _trava_ticket:
        validade = _tickets.pop(t, None)
    return validade is not None and validade >= time.time()


def _igual_seguro(recebido, esperado):
    """compare_digest a prova de byte alto - comparacao SEMPRE em bytes.

    secrets.compare_digest com duas `str` exige ASCII puro: um caractere
    acentuado levanta TypeError. E o valor comparado vem de CABECALHO do
    cliente, que o http.client decodifica em latin-1 - ou seja, dava para
    derrubar o painel inteiro (todas as rotas em 500 "falha interna",
    inclusive / e /app.js) mandando `X-Painel-Sessao: cao` com cedilha.
    Em bytes o compare_digest aceita qualquer conteudo e a protecao de
    timing continua igual. Segredo vazio nunca casa (falha fechada).
    """
    if not esperado:
        return False
    try:
        return secrets.compare_digest(
            (recebido or "").encode("utf-8", "replace"),
            esperado.encode("utf-8"))
    except Exception:
        return False


def _decodificar_consulta(consulta):
    """Query string -> dict de strings (primeiro valor de cada chave).

    O import de urllib.parse fica no TOPO de proposito: `import x` dentro de
    uma funcao torna o nome local na funcao INTEIRA e estoura
    UnboundLocalError em usos anteriores (a licao da 'sombra de import').
    """
    bruto = urllib.parse.parse_qs(consulta, keep_blank_values=True)
    return dict((k, v[0] if v else "") for k, v in bruto.items())


class Manipulador(http.server.BaseHTTPRequestHandler):

    server_version = "PainelWispr"
    sys_version = ""                    # nao anuncia a versao do Python
    protocol_version = "HTTP/1.1"

    # OBRIGATORIO. Sem este atributo o socketserver nunca chama settimeout()
    # na conexao, e uma conexao que abre e NAO manda nada (ou que anuncia um
    # Content-Length maior que o corpo real) prende uma thread PARA SEMPRE.
    # Medido antes do fix: 300 conexoes ociosas = 301 threads que nao voltavam
    # nem depois de 27s, +11MB de RSS. Numa maquina que vive com swap em 85%
    # isso e hostil. O handle_one_request do http.server ja trata
    # socket.timeout fechando a conexao, entao 20s basta.
    timeout = 20

    # OBRIGATORIO: o log_message padrao imprime a linha de request COMPLETA
    # em stderr - com stderr indo pro arquivo, isso gravaria em disco a busca
    # medica (?q=dispneia) e o ticket de sessao.
    def log_message(self, formato, *args):
        pass

    # ---------------- infraestrutura de resposta ----------------

    def _responder(self, codigo, tipo, corpo, extras=()):
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        try:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            # NUNCA um Access-Control-Allow-Origin aqui.
            for chave, valor in extras:
                self.send_header(chave, valor)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(corpo)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, dados, codigo=200):
        corpo = json.dumps(dados, ensure_ascii=False)
        self._responder(codigo, "application/json; charset=utf-8", corpo)

    def _responder_fluxo(self, tipo, nome_arquivo, pedacos):
        """Resposta em PEDACOS (download da exportacao).

        Sem Content-Length de proposito: o tamanho so se conhece no fim, e
        montar tudo na RAM para medir e exatamente o que esta rota evita.
        Por isso 'Connection: close' e OBRIGATORIO: em HTTP/1.1 sem
        Content-Length o corpo termina quando a conexao fecha - sem o
        cabecalho, o cliente ficaria esperando keep-alive para sempre.
        nome_arquivo NUNCA vem cru da request (so data validada por regex
        + extensao de lista fixa) - nada de injecao de cabecalho.
        Os cabecalhos de seguranca sao os MESMOS do _responder.
        """
        try:
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % nome_arquivo)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            for pedaco in pedacos:
                self.wfile.write(pedaco)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _negar(self, codigo, motivo, html_corpo=None):
        if html_corpo is not None:
            self._responder(codigo, "text/html; charset=utf-8", html_corpo)
        else:
            self._json({"ok": False, "erro": motivo}, codigo)

    def send_error(self, code, message=None, explain=None):
        """TODO erro do http.server sai pela nossa infraestrutura.

        Metodo nao declarado (TRACE, ou um verbo inventado) nao chega no
        _despachar: o BaseHTTPRequestHandler responde o send_error(501)
        padrao antes. Essa resposta saia SEM Content-Security-Policy, SEM
        X-Content-Type-Options, SEM Referrer-Policy e SEM Cache-Control -
        justamente os cabecalhos que o resto do arquivo padroniza - e ainda
        ECOAVA o nome do metodo na linha de status. Delegando para o _negar,
        nao sobra rota nenhuma fora do padrao, e nada do que o cliente
        mandou volta refletido.
        """
        self.close_connection = True
        try:
            codigo = int(code)
        except (TypeError, ValueError):
            codigo = 400
        if codigo < 400 or codigo > 599:
            codigo = 400
        self._negar(codigo, "requisicao recusada")

    def _drenar_corpo(self):
        """Le e devolve o corpo do POST (com teto). None = corpo grande."""
        try:
            tamanho = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            tamanho = 0
        if tamanho > TETO_CORPO_POST:
            # le e joga fora para nao envenenar o keep-alive
            restante = tamanho
            while restante > 0:
                pedaco = self.rfile.read(min(65536, restante))
                if not pedaco:
                    break
                restante -= len(pedaco)
            return None
        if tamanho <= 0:
            return b""
        return self.rfile.read(tamanho)

    # ---------------- portao de blindagem ----------------

    def portao(self):
        """None = pode seguir; string = motivo da recusa. Roda ANTES de tudo."""
        # 1) DNS rebinding: o Host tem que ser o literal (nem 'localhost' passa)
        if self.headers.get("Host", "") != HOST_ESPERADO:
            return "host"
        # 2) Origin: ausente (navegacao/GET) OU exatamente a nossa.
        #    ATENCAO: "rejeitar se houver Origin" DERRUBA os POST do proprio
        #    painel - o Chrome manda Origin em POST mesma-origem (medido).
        origem = self.headers.get("Origin")
        if origem is not None and origem != ORIGEM_ESPERADA:
            return "origin"
        # 3) Sec-Fetch-Site tem que ser LISTA DE PERMITIDOS. Outra porta do
        #    mesmo 127.0.0.1 manda "same-site", NAO "cross-site" (medido:
        #    uma pagina em :7912 fez o handler de :7913 executar). E "none"
        #    precisa entrar, senao a navegacao do --app e barrada.
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs not in ("same-origin", "none"):
            return "sec-fetch-site"
        return None

    def _sessao_valida(self):
        """A sessao vem por CABECALHO. NUNCA voltar a usar cookie aqui.

        Cookie e cego a PORTA - a RFC 6265 nao isola cookie por porta, e um
        cookie host-only de 127.0.0.1 e mandado para QUALQUER porta de
        127.0.0.1. Enquanto o painel estivesse aberto, todo outro servidor
        local que o Chrome do usuário visitasse (7900 do pipeline, 3000 do Dose
        Certa, TOEFL, planejador, simulado...) RECEBIA o segredo de sessao
        nos headers e passava a poder ler o historico medico inteiro.
        Reproduzido: servidor-isca em :7919 recebeu o painel_wispr e com ele
        leu os 732 ditados, com o portao (Host/Origin/Sec-Fetch-Site)
        satisfeito - isso contornava history.db 0600, painel/ 0700 e .mestre
        0600 sem precisar ler arquivo nenhum.

        O sessionStorage do navegador, ao contrario, e escopado por ORIGEM
        (esquema + host + PORTA). Entao o token e entregue UMA vez dentro do
        HTML (so na request que apresenta um ticket valido), o app.js guarda
        no sessionStorage daquela porta e o manda em X-Painel-Sessao. Outra
        porta nao recebe nada e nao tem como forjar.
        """
        return _igual_seguro(self.headers.get(CABECALHO_SESSAO) or "",
                             TOKEN_SESSAO)

    # ---------------- despacho ----------------

    def do_GET(self):
        self._despachar("GET")

    def do_HEAD(self):
        self._despachar("HEAD")

    def do_POST(self):
        self._despachar("POST")

    # Os metodos abaixo existem so para NAO cairem no send_error(501) padrao
    # do http.server, que responde uma pagina de erro sem os nossos
    # cabecalhos de seguranca. Aqui eles passam pelo portao e viram 405.
    def do_PUT(self):
        self._despachar("PUT")

    def do_DELETE(self):
        self._despachar("DELETE")

    def do_PATCH(self):
        self._despachar("PATCH")

    def do_OPTIONS(self):
        # nunca responder preflight de CORS: o painel nao tem cross-origin.
        self._despachar("OPTIONS")

    def _despachar(self, metodo):
        # PRIMEIRA linha: renova o relogio de ociosidade.
        _ultimo_acesso[0] = time.time()

        caminho, _, consulta = self.path.partition("?")

        motivo = self.portao()
        if motivo is not None:
            # NAO drenar o corpo aqui. Quem o portao barra (Host de outro
            # nome, Origin de outra pagina) nunca e cliente legitimo do
            # painel, e era exatamente esse drenar que permitia a uma
            # conexao SEM SESSAO e ate com Host errado prender uma thread
            # anunciando um Content-Length que nunca chegava. Responde e
            # fecha a conexao em vez de reaproveitar.
            self.close_connection = True
            registrar("%s %s -> 403 (portao: %s)" % (metodo, caminho, motivo))
            self._negar(403, motivo, PAGINA_403)
            return

        try:
            codigo = self._rotear(metodo, caminho, consulta)
        except Exception as e:
            registrar("ERRO em %s %s: %s: %s"
                      % (metodo, caminho, type(e).__name__, e))
            try:
                self._json({"ok": False, "erro": "falha interna"}, 500)
            except Exception:
                pass
            codigo = 500
        registrar("%s %s -> %s" % (metodo, caminho, codigo))

    def _rotear(self, metodo, caminho, consulta):
        params = _decodificar_consulta(consulta)

        # --- rota interna do PAINEL.bat: autentica pelo MESTRE ---
        if caminho == "/api/sessao/ticket" and metodo == "POST":
            self._drenar_corpo()
            dado = self.headers.get("X-Painel-Mestre") or ""
            if not _igual_seguro(dado, MESTRE):
                self._json({"ok": False}, 403)
                return 403
            self._json({"ok": True, "t": novo_ticket()})
            return 200

        # --- casca e estaticos ---
        # NAO exigem sessao, de proposito: o navegador nao consegue por
        # cabecalho em <link>/<script>/<img>, e nenhum destes arquivos tem
        # dado do usuário dentro (sao a marcacao, o CSS, o JS e o icone).
        # O portao continua barrando pagina de outra origem, e o dado
        # medico continua so atras do /api/*, que exige o cabecalho.
        if metodo in ("GET", "HEAD") and caminho in ESTATICOS:
            if caminho == "/":
                return self._servir_casca(params.get("t") or "", metodo)
            return self._servir_estatico(caminho)

        # --- daqui pra baixo (todo /api/*), exige o cabecalho de sessao ---
        if not self._sessao_valida():
            if metodo == "POST":
                self._drenar_corpo()
            self._negar(403, "sem sessao")
            return 403

        if metodo in ("GET", "HEAD"):
            return self._rotas_get(caminho, params)
        if metodo == "POST":
            return self._rotas_post(caminho)
        self._negar(405, "metodo nao suportado")
        return 405

    # ---------------- casca e estaticos ----------------

    def _servir_casca(self, t, metodo):
        """Serve a interface e entrega o token DENTRO do HTML.

        O token so e embutido na request que apresenta um ticket valido (e
        so em GET - HEAD nao queima ticket). Sem ticket a pagina sobe
        "cega": o app.js nao acha token, nao tenta nada e mostra a caixa de
        sessao expirada mandando abrir pelo PAINEL.bat.
        """
        com_sessao = bool(t) and metodo == "GET" and queimar_ticket(t)
        try:
            with open(os.path.join(PASTA_PAINEL, "interface.html"), "rb") as f:
                corpo = f.read().decode("utf-8", "replace")
        except OSError:
            self._responder(200, "text/html; charset=utf-8",
                            PAGINA_SEM_INTERFACE)
            return 200
        # token_urlsafe = so [A-Za-z0-9_-], nao existe caractere que escape
        # do atributo. Ainda assim escapa, para o dia em que alguem trocar
        # a geracao do token.
        corpo = corpo.replace(
            MARCA_SESSAO,
            html.escape(TOKEN_SESSAO, quote=True) if com_sessao else "")
        self._responder(200, "text/html; charset=utf-8", corpo)
        return 200

    def _servir_estatico(self, caminho):
        # estaticos: comparacao EXATA numa tabela fixa. Nao existe caminho
        # vindo da request - e por isso que /../../etc/passwd, /..%2f.. e
        # //etc/passwd dao 404 sem tocar o disco.
        nome, tipo = ESTATICOS[caminho]
        try:
            with open(os.path.join(PASTA_PAINEL, nome), "rb") as f:
                corpo = f.read()
        except OSError:
            if caminho == "/favicon.svg":
                self._responder(200, "image/svg+xml", FAVICON_RESERVA)
                return 200
            self._negar(404, "arquivo ausente")
            return 404
        self._responder(200, tipo, corpo)
        return 200

    # ---------------- rotas GET ----------------

    def _rotas_get(self, caminho, params):
        if caminho == "/api/resumo":
            self._json(dados_resumo())
            return 200

        if caminho == "/api/historico":
            self._json(dados_historico(
                q=params.get("q", ""),
                pagina=params.get("pagina", "1"),
                por_pagina=params.get("por_pagina", "40"),
                dia=params.get("dia", ""),
                app=params.get("app", "")))
            return 200

        if caminho == "/api/apps":
            self._json(dados_apps())
            return 200

        if caminho == "/api/dias":
            self._json(dados_dias())
            return 200

        if caminho == "/api/saude":
            self._json(dados_saude())
            return 200

        if caminho == "/api/insights":
            self._json(dados_insights())
            return 200

        if caminho == "/api/exportar":
            return self._exportar(params)

        self._negar(404, "rota inexistente")
        return 404

    def _exportar(self, params):
        """GET /api/exportar?formato=md|txt&q=&dia=&app= - download.

        Mesmos filtros do /api/historico (o botao exporta O QUE A TELA
        MOSTRA). Exige sessao como todo /api/* (ja garantido no _rotear).
        O log registra so a rota - nem q, nem dia, nem app (a busca do
        usuário pode ser sintoma de paciente; regra do arquivo inteiro).
        """
        formato = (params.get("formato") or "md").strip().lower()
        if formato not in ("md", "txt"):
            self._negar(400, "formato invalido (use md ou txt)")
            return 400

        consulta = (params.get("q") or "").strip()
        fts = sanitizar_busca(consulta) if consulta else ""
        # mesmo contrato do /api/historico: busca sem nada pesquisavel
        # ('***', '(') = ZERO resultados, nunca a lista inteira.
        forcar_vazio = bool(consulta and not fts)

        dia = (params.get("dia") or "").strip()
        app = (params.get("app") or "").strip()
        onde, valores_filtro = _clausulas(app, dia)

        # nome do arquivo: data validada por regex OU a de hoje. Nada da
        # request entra cru num cabecalho HTTP.
        nome_dia = dia if RE_DIA_ISO.match(dia) else \
            datetime.date.today().strftime("%Y-%m-%d")
        nome_arquivo = "ditados-%s.%s" % (nome_dia, formato)
        tipo = ("text/markdown; charset=utf-8" if formato == "md"
                else "text/plain; charset=utf-8")

        self._responder_fluxo(
            tipo, nome_arquivo,
            gerar_exportacao(formato, fts, consulta, onde, valores_filtro,
                             dia, app, forcar_vazio))
        return 200

    # ---------------- rotas POST ----------------

    def _rotas_post(self, caminho):
        corpo = self._drenar_corpo()
        if corpo is None:
            self._negar(413, "corpo grande")
            return 413

        # Exigir application/json sozinho ja mata o <form> cross-site:
        # form so consegue os 3 tipos simples.
        tipo = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if tipo != "application/json":
            self._negar(415, "use application/json")
            return 415

        try:
            dados = json.loads(corpo.decode("utf-8")) if corpo else {}
        except Exception:
            dados = None
        if not isinstance(dados, dict):
            dados = {}

        if caminho == "/api/servidor/reiniciar":
            if dados.get("confirmar") is not True:
                self._json({"ok": False,
                            "mensagem": 'faltou {"confirmar": true}'}, 400)
                return 400
            self._json(reiniciar_motor())
            return 200

        if caminho == "/api/sair":
            self._json({"ok": True, "mensagem": "Painel encerrado."})
            # shutdown() de dentro do handler trava (deadlock com
            # serve_forever). Responder primeiro, encerrar de outra thread.
            registrar("saida pedida pela interface")
            threading.Thread(target=_encerrar_depois,
                             args=(self.server, 0.3)).start()
            return 200

        self._negar(404, "rota inexistente")
        return 404


class ServidorPainel(http.server.ThreadingHTTPServer):
    """PORTE WINDOWS: SO_REUSEADDR DESLIGADO - e nao e detalhe de estilo.

    MEDIDO nesta maquina, com a porta ja ocupada por outro socket:
      allow_reuse_address = 1 (padrao do http.server) -> OSError errno 13
      allow_reuse_address = False (aqui)              -> OSError errno 10048
    O errno 13 e EACCES; o 10048 e errno.EADDRINUSE. O main() so imprime a
    mensagem "a porta ja esta ocupada" quando o errno E EADDRINUSE - com o
    padrao herdado do http.server, o usuário levaria um traceback cru no lugar
    da frase que o LEIA-ME promete. No Mac isso nao aparecia porque la o bind
    ocupado ja devolve EADDRINUSE com ou sem SO_REUSEADDR.
    """
    allow_reuse_address = False


def _encerrar_depois(servidor, espera):
    time.sleep(espera)
    try:
        servidor.shutdown()
    except Exception:
        pass


def vigia_ocioso(servidor):
    """Auto-encerramento: a maquina do usuário vive com swap apertado, entao
    processo residente e hostil. 20 min sem nenhuma request e o painel sai."""
    while True:
        time.sleep(5)
        if time.time() - _ultimo_acesso[0] > SEGUNDOS_OCIOSO:
            registrar("ocioso ha %d min: encerrando" % (SEGUNDOS_OCIOSO // 60))
            sys.stderr.write("Painel ocioso: encerrando.\n")
            sys.stderr.flush()
            try:
                servidor.shutdown()
            except Exception:
                pass
            return


def abrir_no_chrome(url):
    """Abre a janela sem barra de endereco. Falha aqui nunca derruba o painel.

    PORTE WINDOWS: os caminhos do Chrome e o fallback (`os.startfile` no
    lugar do `/usr/bin/open`). Em teste isto NUNCA roda: o painel so abre
    navegador quando --sem-navegador esta ausente.
    """
    try:
        for chrome in CHROMES:
            if chrome and os.path.exists(chrome):
                subprocess.Popen([chrome, "--app=" + url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=CREATE_NO_WINDOW)
                return True
        os.startfile(url)                     # navegador padrao do Windows
        return True
    except Exception as e:
        sys.stderr.write("Nao consegui abrir o navegador (%s). Abra na mao.\n"
                         % type(e).__name__)
        return False


# =====================================================================
# 6. MAIN
# =====================================================================

def main(argv=None):
    global PORTA, HOST_ESPERADO, ORIGEM_ESPERADA, MESTRE, TOKEN_SESSAO
    global _mapa_pwa

    # PORTE WINDOWS: `os.umask(0o077)` era a protecao de TODO arquivo novo no
    # Mac. NTFS nao tem umask - a chamada existe no Windows mas nao protege
    # nada, entao virou no-op de proposito. Quem protege arquivo aqui e o
    # _restringir_acl (icacls), aplicado ao .mestre.

    analisador = argparse.ArgumentParser(
        description="Painel do Ditado - servidor local somente leitura.")
    analisador.add_argument(
        "--porta", type=int,
        default=int(os.environ.get("PAINEL_PORTA") or PORTA_PADRAO),
        help="porta local (padrao 7910)")
    analisador.add_argument(
        "--token", default="",
        help=("segredo mestre da sessao. PREFIRA a variavel de ambiente "
              "PAINEL_MESTRE: argumento de linha de comando aparece pra quem "
              "consultar Win32_Process; variavel de ambiente nao."))
    analisador.add_argument(
        "--sem-navegador", action="store_true",
        default=bool(os.environ.get("PAINEL_SEM_NAVEGADOR")),
        help="nao abre o navegador (use quando o PAINEL.bat abre por conta)")
    analisador.add_argument(
        "--garantir-mestre", action="store_true",
        help=("PORTE WINDOWS: cria (se faltar) o painel\\.mestre com ACL "
              "restrita, imprime o segredo e sai. E como o PAINEL.bat "
              "descobre o segredo sem passa-lo por argv."))
    args = analisador.parse_args(argv)

    if args.garantir_mestre:
        sys.stdout.write(ler_ou_criar_mestre() + "\n")
        return 0

    PORTA = args.porta
    HOST_ESPERADO = "127.0.0.1:%d" % PORTA
    ORIGEM_ESPERADA = "http://127.0.0.1:%d" % PORTA
    # PORTE WINDOWS: sem env e sem --token, o segredo vem do painel\.mestre
    # (criado na hora se faltar). No Mac quem criava o arquivo era o
    # PAINEL.command; aqui o painel sabe faze-lo sozinho, entao um .mestre
    # apagado nao quebra nada - ele nasce de novo no clique seguinte.
    MESTRE = args.token or os.environ.get("PAINEL_MESTRE") or \
        ler_ou_criar_mestre()
    TOKEN_SESSAO = secrets.token_urlsafe(32)   # nunca aparece em URL nenhuma

    try:
        _mapa_pwa = carregar_mapa_pwa()
    except Exception:
        _mapa_pwa = {}

    try:
        # bind EXPLICITO em 127.0.0.1. Nunca "" nem "0.0.0.0".
        servidor = ServidorPainel(("127.0.0.1", PORTA), Manipulador)
    except OSError as erro:
        if erro.errno == errno.EADDRINUSE:
            sys.stderr.write(
                "\n  A porta %d ja esta ocupada por outro programa.\n"
                "  O painel NAO foi aberto (e nada foi alterado).\n"
                "  Feche o outro painel e tente de novo, ou veja quem ocupa:\n"
                "      netstat -ano | findstr :%d\n\n" % (PORTA, PORTA))
            registrar("porta %d ocupada: nao subi" % PORTA)
            sys.exit(1)
        raise
    servidor.daemon_threads = True

    registrar("boot: painel no ar em 127.0.0.1:%d (pid %d)"
              % (PORTA, os.getpid()))
    sys.stderr.write("Painel do Ditado no ar em http://127.0.0.1:%d\n" % PORTA)
    sys.stderr.flush()

    vigia = threading.Thread(target=vigia_ocioso, args=(servidor,))
    vigia.daemon = True
    vigia.start()

    if not args.sem_navegador:
        abrir_no_chrome("http://127.0.0.1:%d/?t=%s" % (PORTA, novo_ticket()))

    def encerrar(_sinal, _quadro):
        registrar("sinal recebido: encerrando")
        threading.Thread(target=_encerrar_depois,
                         args=(servidor, 0.0)).start()

    try:
        signal.signal(signal.SIGTERM, encerrar)
        signal.signal(signal.SIGINT, encerrar)
    except Exception:
        pass

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            servidor.server_close()
        except Exception:
            pass
        registrar("encerrado")


if __name__ == "__main__":
    main()
