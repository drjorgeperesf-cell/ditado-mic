# -*- coding: utf-8 -*-
"""Corretor PT (M4) — porte de `~/Desktop/CORRETOR PT/corretor.py`.

Voce digita sem acento em QUALQUER app, aperta **Control direita**, e o que
acabou de digitar e' apagado e redigitado revisado. O texto digitado fica **so
na memoria** — nunca em disco nem no log (so o TAMANHO vai pro log).

No Mac eram dois processos conversando por notificacao distribuida; aqui os
dois vivem no MESMO processo e o IPC some (o mapa macOS->Windows do inventario
ja previa isso). A bifurcacao continua identica:
  - buffer digitado VAZIO   -> pede FORMATAR SELECAO (e a pilula vem pra frente)
  - buffer NAO-vazio        -> corrige o que ele acabou de digitar, e a pilula
                               **NAO aparece** nesse ramo, de proposito.

O que o Mac fazia e continua valendo:
  - buffer `list[str]` em RAM, MAX_CHARS = 800 (trunca pela frente);
  - Backspace/Delete fazem pop;
  - reset por: atalho com Ctrl/Win, setas/Enter/Tab/Esc/Home/End/PgUp/PgDn,
    clique do mouse (esquerdo OU direito), troca de app, e ociosidade de 300 s
    — descartada na proxima tecla **E no proprio aperto** (memoria de horas
    atras reescrevia texto velho);
  - `_geracao` incrementa a cada tecla/clique REAL: e' o detector de "mexeu
    durante a correcao";
  - eventos sinteticos NOSSOS levam `dwExtraInfo = MARCA_SINTETICO` e o gancho
    os ignora (o Ctrl+C do microfone nao pode contar como "tecla real" e
    abortar a correcao em curso);
  - guarda de sanidade `parece_revisao`: recusa se apareceu `\\n` que nao
    existia; se a razao de tamanho sair de 0,6 <= r/o <= 1,6; exige
    SequenceMatcher sobre palavras sem acento >= 0,60 (com >= 4 palavras) ou
    >= 0,34 (texto curto). Recusa -> "Correcao descartada (IA fugiu do texto) —
    nada foi mudado";
  - preserva espacos de inicio/fim do original;
  - reescrita: N backspaces com 0,004 s entre eles -> 0,05 s -> digita em
    blocos de 16 chars com 0,008 s por bloco.

DIVERGENCIA DO MAC CORRIGIDA AQUI (inventario 10.6): no Mac o texto do usuario
ia como ARGUMENTO de processo (visivel em `ps`). Aqui vai por **stdin**.
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import threading
import time
import unicodedata

import config
import winapi
from log import log
from prompts import CORRETOR_SYSTEM_PROMPT, corretor_envelopar
from proc import rodar as proc_rodar

MAX_CHARS = 800
IDLE_RESET_S = 300
BLOCO_UNICODE = 16

# Teclas que ZERAM o buffer (setas, Enter, Tab, Esc, Home/End/PgUp/PgDn)
VK_RESET = frozenset({0x25, 0x26, 0x27, 0x28, 0x0D, 0x09, 0x1B,
                      0x24, 0x23, 0x21, 0x22, 0x2E})
VK_BACKSPACE = 0x08
VK_DELETE = 0x2E


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if not unicodedata.combining(c))


def _palavras(s: str) -> list:
    return re.findall(r"\w+", sem_acento(s).lower())


def parece_revisao(original: str, revisado: str) -> bool:
    """Barra resposta do modelo (em vez de revisao) e reescrita demais."""
    if "\n" in revisado and "\n" not in original:
        return False
    tam_o, tam_r = len(original.strip()), len(revisado.strip())
    if tam_o and not (0.6 <= tam_r / tam_o <= 1.6):
        return False
    razao = difflib.SequenceMatcher(None, _palavras(original),
                                    _palavras(revisado)).ratio()
    return razao >= 0.6 if len(_palavras(original)) >= 4 else razao >= 0.34


def comando_cli():
    from formatado import cli_bin
    b = cli_bin()
    if not b:
        return None
    # --safe-mode e' o que da a velocidade (2,3-3,7 s com ele x 11,7 s sem).
    # `--bare` exigiria API key = PROIBIDO.
    return [b, "-p", "--safe-mode", "--tools", "", "--no-chrome",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--no-session-persistence",
            "--model", str(config.get("corretor_modelo", "sonnet")),
            "--system-prompt", CORRETOR_SYSTEM_PROMPT]


def _ambiente_cli() -> dict:
    """Ambiente do app; sem a chave da API quando o desvio do formatado (flag
    em memoria, compartilhada) ja esta ligado. O valor da chave nunca e' lido."""
    import formatado
    env = dict(os.environ)
    if formatado._API_SEM_CREDITO:
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def _credito_da_api_acabou(r, original: str) -> bool:
    """Saida de texto do CLI e' falta de credito? Com erro de saida: casa o
    padrao do formatado. Com saida 0: so' se for curta e NAO parecer revisao
    (uma correcao legitima que cite 'billing' nao pode desligar a API)."""
    from formatado import _RE_SEM_CREDITO
    saida = str(getattr(r, "stdout", "") or "")
    if not _RE_SEM_CREDITO.search(saida):
        return False
    if getattr(r, "returncode", 1) != 0:
        return True
    return len(saida) < 400 and not parece_revisao(original, saida)


def revisar(texto: str, rodar=None):
    """Chama o CLI. Texto SEMPRE por stdin. Devolve (texto|None, motivo)."""
    cmd = comando_cli()
    if not cmd:
        return None, "fail:sem-cli"
    tmo = float(config.get("corretor_timeout_s", 20))
    exec_ = rodar or (lambda: proc_rodar(
        cmd, entrada=corretor_envelopar(texto), timeout=tmo,
        env=_ambiente_cli(), cwd=str(formatado.cwd_sessao())))
    try:
        r = exec_()
        if _credito_da_api_acabou(r, texto):
            # mesmo desvio do formatado: repete UMA vez sem a chave (plano)
            import formatado
            if formatado._desviar_da_api():
                r = exec_()
    except subprocess.TimeoutExpired:
        return None, "fail:timeout"
    except Exception:
        return None, "fail:result-erro"
    if getattr(r, "returncode", 1) != 0:
        from formatado import classificar_erro
        return None, classificar_erro(str(getattr(r, "stdout", "") or ""))
    # SEM --output-format: le a saida de texto padrao (r.stdout.strip("\n"))
    saida = (getattr(r, "stdout", "") or "").strip("\n")
    return (saida or None), ("ok" if saida else "fail:vazio")


class Corretor:
    """Buffer do que foi digitado + o aperto do Control direito."""

    def __init__(self, ao_pedir_selecao=None, avisar=None):
        self.ao_pedir_selecao = ao_pedir_selecao or (lambda: None)
        self.avisar = avisar or (lambda m: None)
        self._buffer: list = []
        self._geracao = 0
        self._ultima_tecla = 0.0
        self._app = ""
        self._lock = threading.Lock()

    # -- alimentacao do buffer -----------------------------------------
    def resetar(self, motivo: str):
        if self._buffer:
            log(f"corretor: buffer zerado ({motivo}), tinha {len(self._buffer)}c")
        self._buffer.clear()
        self._geracao += 1

    def _checar_ociosidade(self):
        """Descartado na proxima tecla E no proprio aperto (fix de 19/09: a
        memoria de horas atras reescrevia texto velho)."""
        idle = float(config.get("corretor_idle_reset_s", IDLE_RESET_S))
        if self._ultima_tecla and (time.monotonic() - self._ultima_tecla) > idle:
            self.resetar("ocioso")

    def tecla(self, vk: int, char: str | None = None):
        """Chamado pelo gancho para cada tecla REAL (os nossos sinteticos ja
        foram filtrados pelo dwExtraInfo no gancho)."""
        if not config.get("corretor_enabled", True):
            return
        self._checar_ociosidade()
        self._ultima_tecla = time.monotonic()
        self._geracao += 1
        app = winapi.app_em_foco()
        if app and app != self._app:
            self._app = app
            self.resetar("troca de app")
        # AltGr injeta um LCtrl FANTASMA. Sem esta ressalva, digitar um acento
        # no US-Internacional (AltGr+e) seria lido como "atalho com Ctrl" e
        # jogaria fora tudo o que ele acabou de escrever.
        altgr = (winapi.user32.GetAsyncKeyState(winapi.VK_RMENU) & 0x8000) != 0
        ctrl_esq = (winapi.user32.GetAsyncKeyState(winapi.VK_LCONTROL) & 0x8000) != 0
        ctrl_dir = (winapi.user32.GetAsyncKeyState(winapi.VK_RCONTROL) & 0x8000) != 0
        ctrl = ctrl_dir or (ctrl_esq and not altgr)
        win = ((winapi.user32.GetAsyncKeyState(winapi.VK_LWIN) & 0x8000) or
               (winapi.user32.GetAsyncKeyState(winapi.VK_RWIN) & 0x8000))
        if ctrl or win:
            self.resetar("atalho")
            return
        if vk in VK_RESET:
            self.resetar("tecla de navegacao")
            return
        if vk == VK_BACKSPACE:
            if self._buffer:
                self._buffer.pop()
            return
        if char and char.isprintable():
            self._buffer.append(char)
            if len(self._buffer) > MAX_CHARS:
                del self._buffer[:len(self._buffer) - MAX_CHARS]

    def clique(self):
        self._geracao += 1
        self.resetar("clique do mouse")

    @property
    def buffer(self) -> str:
        return "".join(self._buffer)

    # -- o aperto -------------------------------------------------------
    def acionar(self):
        """Control direito. Nao bloqueia quem chamou."""
        if not self._lock.acquire(blocking=False):
            return "ocupado"
        threading.Thread(target=self._fluxo, name="corretor-fluxo",
                         daemon=True).start()
        return "ok"

    def _fluxo(self):
        try:
            self._checar_ociosidade()
            original = self.buffer
            if not original.strip():
                # Buffer VAZIO -> ponte com o formatar-selecao.
                log("corretor: aperto sem texto digitado -> formatar selecao")
                self.ao_pedir_selecao()
                return
            # NESTE RAMO A PILULA NAO APARECE, de proposito (veto 3).
            geracao0 = self._geracao
            app0 = winapi.app_em_foco()
            log(f"corretor: revisando {len(original)}c")
            saida, motivo = revisar(original)
            if not saida:
                from prompts import MOTIVO_PADRAO, MOTIVOS
                self.avisar(MOTIVOS.get(motivo, MOTIVO_PADRAO))
                return
            if not parece_revisao(original, saida):
                log(f"corretor: revisao recusada pela sanidade ({len(original)}c -> "
                    f"{len(saida)}c)")
                self.avisar("Correção descartada (IA fugiu do texto) — nada foi mudado")
                return
            # preserva espacos de inicio/fim do original
            esq = original[:len(original) - len(original.lstrip())]
            dir_ = original[len(original.rstrip()):]
            final = esq + saida.strip() + dir_
            if self._geracao != geracao0 or winapi.app_em_foco() != app0:
                self.avisar("Você mexeu enquanto corrigia — aperte Control de novo")
                return
            winapi.apagar_atras(len(original))
            time.sleep(0.05)
            winapi.digitar_unicode(final, bloco=BLOCO_UNICODE, pausa=0.008)
            self.resetar("reescrito")
            log(f"corretor: reescrito ({len(original)}c -> {len(final)}c)")
        except Exception as e:
            log(f"corretor: fluxo quebrou ({type(e).__name__})")
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass
