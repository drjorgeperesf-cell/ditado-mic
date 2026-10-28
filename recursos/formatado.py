# -*- coding: utf-8 -*-
"""Formatado tardio: o CLI do Claude estrutura o texto DEPOIS (M3).

REGRA DE OURO (literal do usuário): o texto cru OFICIAL sai na hora, mesmo
errado, e **NUNCA** ha substituicao automatica do que ja foi colado.

CONTAMINACAO ENTRE PEDIDOS — a razao de existir deste arquivo:
um processo do CLI e' UMA conversa; o pedido N ve os N-1 anteriores. Com texto
medico (dose, via de administracao) isso e' contaminacao real, e a razao de
TAMANHO passa limpa numa resposta do tamanho certo com o conteudo errado.
Defesa DUPLA, as duas sempre ligadas:
  (a) **reciclar o worker a cada 3 pedidos**;
  (b) **guarda de identidade** por sobreposicao de palavras >= 0,70, CEGA A
      NUMEROS (cardinais e ordinais por extenso saem dos dois lados, senao
      "trezentos miligramas" -> "300 mg" derrubava o overlap).
Reprovou -> entrega o texto cru. Devolver o texto do pedido anterior e' o pior
erro possivel deste app.

Timeout do pedido OBRIGA a matar o worker — senao a resposta pendente vaza pro
proximo pedido.

Privacidade: o texto do usuario vai SEMPRE por **stdin** (nunca por argumento
de processo: argv e' legivel por qualquer processo da maquina). E o cwd e'
dedicado, porque o CLI grava toda sessao como .jsonl em texto claro sob
`~/.claude/projects/<slug do cwd>/` — uma segunda copia do texto medico.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

import config
from log import base_dados, log
from prompts import (FORMATADO_FILA_MAX, FORMATADO_MIN_PALAVRAS,
                     FORMATADO_MIN_SOBREPOSICAO, FORMATADO_PEDIDO,
                     FORMATADO_RECICLA, FORMATADO_TIMEOUT_S,
                     formatado_envelopar)
from proc import CREATE_NO_WINDOW

MORTE_ESPERA_S = 1.0


# ════════════════════════════════════════════════════════════════════
# Guarda de identidade
_PALAVRAS_NUMERO = frozenset(
    "zero um uma dois duas tres quatro cinco seis sete oito nove dez onze doze "
    "treze catorze quatorze quinze dezesseis dezessete dezoito dezenove vinte "
    "trinta quarenta cinquenta sessenta setenta oitenta noventa cem cento "
    "duzentos duzentas trezentos trezentas quatrocentos quatrocentas quinhentos "
    "quinhentas seiscentos seiscentas setecentos setecentas oitocentos "
    "oitocentas novecentos novecentas mil milhao milhoes meio meia e "
    "primeiro primeira segundo segunda terceiro terceira quarto quarta "
    "quinto quinta sexto sexta setimo setima oitavo oitava nono nona "
    "decimo decima vigesimo vigesima trigesimo trigesima quadragesimo "
    "quadragesima quinquagesimo quinquagesima sexagesimo sexagesima "
    "septuagesimo septuagesima octogesimo octogesima nonagesimo nonagesima "
    "centesimo centesima milesimo milesima".split())


def _palavras(txt: str) -> set:
    """O formatado ACRESCENTA acento e maiuscula — comparar cru contra
    formatado exige tirar as duas coisas dos dois lados."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", txt or "")
        if unicodedata.category(c) != "Mn")
    palavras = set(re.findall(r"[a-z]+", sem_acento.lower()))
    return palavras - _PALAVRAS_NUMERO


def parece_do_ditado(cru: str, formatado: str) -> bool:
    """O formatado e' MESMO uma versao DESTE ditado?

    Resposta com conteudo de OUTRO ditado (ou um preambulo/recusa do modelo)
    despenca aqui — a razao de tamanho sozinha nao pega isso, porque o texto
    errado pode ter exatamente o tamanho certo.
    Texto com menos de 8 palavras distintas nao tem amostra: nao julga.
    """
    if len(re.findall(r"(?m)^\s*\d+\.\s+", formatado)) >= 2:
        cru = re.sub(r"\b(?:primeiro|segundo|terceiro|depois|em seguida|"
                     r"por [úu]ltimo|por fim)\b", "", cru, flags=re.IGNORECASE)
    p_cru = _palavras(cru)
    if len(p_cru) < FORMATADO_MIN_PALAVRAS:
        return True
    p_fmt = _palavras(formatado)
    if not p_fmt:
        return False
    mantidas = len(p_cru & p_fmt) / float(len(p_cru))
    return mantidas >= FORMATADO_MIN_SOBREPOSICAO


def tamanho_ok(cru: str, formatado: str) -> bool:
    """Com cru >= 40 chars, exige 0,55x <= len(fmt) <= 2,2x."""
    if len(cru) < 40:
        return True
    return 0.55 * len(cru) <= len(formatado) <= 2.2 * len(cru)


def classificar_erro(msg: str) -> str:
    """Reduz erro externo a codigos fixos — nunca leva o erro cru ao log."""
    m = (msg or "").lower()
    for t in ("spend limit", "usage limit", "rate limit", "quota",
              "limit reached", "too many requests", "hit your limit",
              "reached your limit", "limit exceeded"):
        if t in m:
            return "fail:limite"
    # O CLI atual descreve o teto do plano como "... limit ... resets ...",
    # sem nenhuma das frases antigas. O horario nunca precisa ir ao log.
    if re.search(r"\blimit\b[^\n]*\bresets?\b", m):
        return "fail:limite"
    for t in ("not logged in", "unauthorized", "authentication",
              "login required", "token expired"):
        if t in m:
            return "fail:login"
    return "fail:result-erro"


def classificar_resultado(obj: dict) -> str:
    """Erro stream-json -> codigo fixo, sem gravar mensagens externas.

    No CLI 2.1.284, o teto real do plano vem com api_error_status=429 mesmo
    quando subtype diz success. Versoes SDK tambem usam errors[] em vez de
    result; ignorar esses campos fazia repetir o pedido inutilmente.
    """
    if str(obj.get("api_error_status") or "") == "429":
        return "fail:limite"
    if str(obj.get("api_error_status") or "") == "401":
        return "fail:login"
    mensagens = []
    for chave in ("result", "error", "errors"):
        valor = obj.get(chave)
        if isinstance(valor, str):
            mensagens.append(valor)
        elif isinstance(valor, (list, dict)):
            mensagens.append(json.dumps(valor, ensure_ascii=False))
    return classificar_erro("\n".join(mensagens))


# ════════════════════════════════════════════════════════════════════
# Credito da API do usuário (variavel de usuario ANTHROPIC_API_KEY)
#
# O `claude` usa a chave sozinho, e o usuário QUER gastar o credito mensal da API
# primeiro (renova dia 21). Quando acaba, a falha vem como linha JSON `result`
# com is_error ("Credit balance is too low"). Medido no CLI real com chave
# inventada: stderr vazio e o processo CONTINUA vivo. Como o ambiente de um
# processo e' fixado no nascimento, o desvio MATA o worker, liga a flag abaixo
# e repete o MESMO pedido UMA vez; dai em diante todo CLI nasce sem a chave e
# cai no plano (ver `ambiente`). So' em memoria: reiniciar o app volta a tentar
# a API. O VALOR da chave nunca e' logado nem gravado.
_RE_SEM_CREDITO = re.compile(
    r"credit balance|balance is too low|purchase credits|add funds|billing|"
    r"insufficient[^\n]{0,20}(credit|fund|quota)", re.IGNORECASE)
_API_SEM_CREDITO = False


def _e_falta_de_credito(obj: dict) -> bool:
    """O `result` de erro do CLI e' falta de credito da API? Olha o objeto
    inteiro (result/error/errors variam entre versoes do CLI)."""
    return bool(_RE_SEM_CREDITO.search(json.dumps(obj, ensure_ascii=False)))


def _desviar_da_api() -> bool:
    """Liga o desvio pelo resto da vida do processo. False = repetir nao
    adianta (a chave ja nao esta no ambiente): o pedido segue como falha."""
    global _API_SEM_CREDITO
    if _API_SEM_CREDITO or "ANTHROPIC_API_KEY" not in os.environ:
        return False
    _API_SEM_CREDITO = True
    log("formatado: credito da API acabou — repetindo no plano (sem a chave) "
        "ate o app reiniciar")
    return True


# ════════════════════════════════════════════════════════════════════
def cli_bin() -> str | None:
    """`claude` pode ser .exe ou .cmd — subprocess com lista nao acha .cmd
    sozinho, por isso shutil.which()."""
    p = os.environ.get("MICROFONE_CLAUDE_BIN")
    if p:
        return p
    for nome in ("claude", "claude.exe", "claude.cmd"):
        achou = shutil.which(nome)
        if achou:
            return achou
    padrao = Path.home() / ".local" / "bin" / "claude.exe"
    return str(padrao) if padrao.exists() else None


def cwd_sessao() -> Path:
    """cwd DEDICADO: o CLI grava TODA sessao (inclusive `-p`) como .jsonl em
    texto claro sob ~/.claude/projects/<slug do cwd>/ — uma segunda copia do
    texto medico que escaparia do retention e do DESINSTALAR."""
    d = base_dados() / "cli-sessao"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ambiente() -> dict:
    env = dict(os.environ)
    # CRITICO: sem isso o modelo "pensa" antes de pontuar (9.273 tokens de
    # saida e 83 s num ditado de 420 c; com thinking zerado, 154 tokens e 2,7 s).
    env["MAX_THINKING_TOKENS"] = "0"
    env["PYTHONUTF8"] = "1"
    if _API_SEM_CREDITO:
        env.pop("ANTHROPIC_API_KEY", None)   # sem a chave o CLI cai no plano
    return env


def comando() -> list[str] | None:
    b = cli_bin()
    if not b:
        return None
    return [
        b, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--model", str(config.get("formatado_modelo", "claude-haiku-5-5")),
        "--effort", str(config.get("formatado_claude_esforco", "medium")),
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
        "--no-session-persistence",
        "--tools", "",
        "--disable-slash-commands",
        "--system-prompt", FORMATADO_PEDIDO,
        "--max-turns", "1",
    ]


class Worker:
    """Subprocess persistente do CLI falando stream-json por stdin/stdout.

    Sobe na PRIMEIRA necessidade (nunca no boot). Morre ocioso. Reciclado a
    cada FORMATADO_RECICLA pedidos. Timeout MATA o worker.
    """

    def __init__(self, fabrica_processo=None):
        self._p = None
        self._pedidos = 0
        self._ultimo_uso = 0.0
        self._lock = threading.Lock()
        self._linhas = None
        self._leitor = None
        self._sem_credito = False        # o ultimo pedido falhou por credito
        self._fabrica = fabrica_processo or self._subir_real

    # -- ciclo de vida -------------------------------------------------
    def _subir_real(self):
        cmd = comando()
        if not cmd:
            return None
        return subprocess.Popen(
            cmd, cwd=str(cwd_sessao()), env=ambiente(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,       # pipe que ninguem le TRAVA o filho
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW)

    def _ler_stdout(self, p, fila):
        try:
            for linha in p.stdout:
                fila.append(linha)
        except Exception:
            pass
        finally:
            fila.append(None)                # sentinela de EOF

    def _garantir(self):
        ocioso = float(config.get("formatado_worker_ocioso_s", 3600))
        if self._p is not None:
            morreu = self._p.poll() is not None
            velho = (time.monotonic() - self._ultimo_uso) > ocioso
            if morreu or velho or self._pedidos >= FORMATADO_RECICLA:
                self.encerrar("reciclagem" if not morreu else "morto")
        if self._p is None:
            self._p = self._fabrica()
            if self._p is None:
                return False
            self._pedidos = 0
            self._linhas = []
            self._leitor = threading.Thread(
                target=self._ler_stdout, args=(self._p, self._linhas),
                name="formatado-stdout", daemon=True)
            self._leitor.start()
        return True

    def encerrar(self, motivo=""):
        p, self._p = self._p, None
        self._linhas = None
        if p is None:
            return
        log(f"formatado: worker encerrado ({motivo})")
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=MORTE_ESPERA_S)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        # Fechar stdin na thread da UI pode travar a UI (pipe cheio): vai pra
        # thread daemon.
        def _fechar():
            for f in (getattr(p, "stdin", None), getattr(p, "stdout", None)):
                try:
                    if f:
                        f.close()
                except Exception:
                    pass
        threading.Thread(target=_fechar, daemon=True).start()

    # -- um pedido -----------------------------------------------------
    def pedir(self, texto: str, prazo_s: float, conservador=False):
        """Devolve (texto|None, motivo). Motivo "ok" quando deu certo.

        Credito da API acabou (ANTHROPIC_API_KEY no ambiente): repete o MESMO
        pedido UMA vez, com o CLI nascendo sem a chave (cai no plano)."""
        fim = time.monotonic() + max(1.0, prazo_s)
        saida, motivo = self._pedir_uma_vez(texto, prazo_s, conservador)
        if self._sem_credito and _desviar_da_api():
            self.encerrar("api sem credito")     # este CLI nasceu com a chave
            saida, motivo = self._pedir_uma_vez(
                texto, fim - time.monotonic(), conservador)
        return saida, motivo

    def _pedir_uma_vez(self, texto: str, prazo_s: float, conservador=False):
        self._sem_credito = False
        with self._lock:
            if not self._garantir():
                return None, "fail:sem-cli"
            envelope = formatado_envelopar(texto, conservador)
            turno = json.dumps({"type": "user", "message": {
                "role": "user",
                "content": [{"type": "text", "text": envelope}]}},
                ensure_ascii=False)
            try:
                self._p.stdin.write(turno + "\n")
                self._p.stdin.flush()
            except Exception:
                self.encerrar("stdin quebrado")
                return None, "fail:result-erro"
            self._pedidos += 1
            self._ultimo_uso = time.monotonic()

            limite = time.monotonic() + max(1.0, prazo_s)
            while time.monotonic() < limite:
                if not self._linhas:
                    time.sleep(0.02)
                    continue
                linha = self._linhas.pop(0)
                if linha is None:
                    self.encerrar("eof")
                    return None, "fail:result-erro"
                try:
                    obj = json.loads(linha)
                except ValueError:
                    continue
                if obj.get("type") != "result":
                    continue
                if obj.get("is_error"):
                    self._sem_credito = _e_falta_de_credito(obj)
                    return None, classificar_resultado(obj)
                saida = (obj.get("result") or "").strip()
                if not saida:
                    return None, "fail:vazio"
                return saida, "ok"
            # TIMEOUT OBRIGA A MATAR: senao a resposta pendente vaza pro
            # proximo pedido.
            self.encerrar("timeout")
            return None, "fail:timeout"


# ════════════════════════════════════════════════════════════════════
class Fila:
    """1 pedido por vez, teto de 8. Ditado novo NUNCA espera.

    O CALLBACK E' CHAMADO SEMPRE, inclusive no item descartado da fila: esvaziar
    a fila sem chamar o `ao_terminar` do item parado nela deixou `_OCUPADO`
    preso ~190 s (botoes mudos) e um "Demorou demais" falso no fim.
    """

    def __init__(self, worker=None):
        self.worker = worker or Worker()
        self._fila: list[dict] = []
        self._ativos: set = set()
        self._lock = threading.Lock()
        self._acorda = threading.Event()
        self._thread = None
        self.geracao = 0
        self._parar = False

    def _chave(self, item):
        return (item.get("ref"), hashlib.sha256(
            (item.get("texto") or "").encode("utf-8")).hexdigest())

    def enfileirar(self, texto, ref=None, ao_terminar=None) -> str:
        if not texto or len(texto) < 1:
            return "vazio"
        item = {"texto": texto, "ref": ref, "ao_terminar": ao_terminar,
                "geracao": self.geracao}
        k = self._chave(item)
        with self._lock:
            if k in self._ativos:                 # deduplicacao
                return "duplicado"
            if len(self._fila) >= FORMATADO_FILA_MAX:
                log(f"formatado: fila cheia ({FORMATADO_FILA_MAX}) — pedido recusado")
                if ao_terminar:
                    ao_terminar(None, "fail:fila")
                return "fail:fila"
            self._ativos.add(k)
            self._fila.append(item)
        self._garantir_thread()
        self._acorda.set()
        return "ok"

    def _garantir_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._rodar, name="formatado-fila",
                                        daemon=True)
        self._thread.start()

    def cancelar_tudo(self, motivo="fail:cancelado"):
        """Cancelamento nao pode deixar NINGUEM esperando."""
        with self._lock:
            self.geracao += 1
            parados, self._fila = self._fila, []
            self._ativos.clear()
        for item in parados:
            cb = item.get("ao_terminar")
            if cb:
                try:
                    cb(None, motivo)
                except Exception:
                    pass
        self.worker.encerrar("cancelamento")

    def _rodar(self):
        while not self._parar:
            with self._lock:
                item = self._fila.pop(0) if self._fila else None
            if item is None:
                self._acorda.wait(timeout=5.0)
                self._acorda.clear()
                continue
            try:
                self._processar(item)
            finally:
                with self._lock:
                    self._ativos.discard(self._chave(item))

    def _processar(self, item):
        cru = item.get("texto") or ""
        cb = item.get("ao_terminar")
        if item.get("geracao") != self.geracao:
            if cb:
                cb(None, "fail:cancelado")
            return
        t0 = time.monotonic()
        try:
            teto = max(15.0, min(600.0, float(
                config.get("formatado_timeout_s", FORMATADO_TIMEOUT_S))))
        except (TypeError, ValueError):
            teto = float(FORMATADO_TIMEOUT_S)
        prazo = t0 + teto
        saida, motivo = None, "fail:timeout"
        for tentativa in range(2):
            if item.get("geracao") != self.geracao:
                if cb:
                    cb(None, "fail:cancelado")
                return
            restante = teto if tentativa == 0 else prazo - time.monotonic()
            if restante < 1:
                break
            if tentativa:
                # Nova sessao, mesmo modelo: nao repetir resposta contaminada.
                self.worker.encerrar("revisao conservadora")
            saida, motivo = self.worker.pedir(cru, restante, conservador=bool(tentativa))
            if motivo == "fail:cancelado":
                if cb:
                    cb(None, motivo)
                return
            if saida:
                if not tamanho_ok(cru, saida):
                    motivo = "fail:tamanho"
                elif not parece_do_ditado(cru, saida):
                    motivo = "fail:identidade"
                else:
                    motivo = "ok"
                    break
            saida = None
            log(f"formatado: tentativa {tentativa + 1}/2 recusada ({motivo}, {len(cru)}c)")
            if motivo in ("fail:limite", "fail:login", "fail:sem-cli", "fail:provedor"):
                break
        dt = time.monotonic() - t0
        if saida:
            log(f"formatado: pronto ({dt:.1f}s, {len(cru)}c -> {len(saida)}c)")
        else:
            log(f"formatado: pedido falhou ({motivo}, {dt:.1f}s, {len(cru)}c)")
        if cb:
            try:
                cb(saida, motivo)
            except Exception as e:
                log(f"formatado: callback quebrou ({type(e).__name__})")
