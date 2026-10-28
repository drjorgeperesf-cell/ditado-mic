# -*- coding: utf-8 -*-
"""Formatar o texto SELECIONADO (M4) — porte de selecao.py do Mac.

Modulo SEPARADO de proposito: o app importa em try/except e falha aqui NAO
derruba o ditado.

PONTO UNICO DE ENTRADA: `acionar(origem)`, com lock NAO-BLOQUEANTE (pedido
concorrente e' ignorado com log) e thread daemon — nunca bloqueia quem chamou.

LEITURA DA SELECAO, 2 vias nesta ordem:
 1. UI Automation (teto de 0,35 s). None = "o app nao informa".
 2. Ctrl+C de reserva, SO se a via 1 veio vazia E `pode_usar_ctrl_c()`:
    foto de TODO o clipboard + numero de sequencia; marca `ignorar_ate` pro
    proprio Ctrl+C nao contar como "tecla real do usuário"; envia Ctrl+C; poll
    ate 0,7 s em passos de 0,03 s; quando o numero muda, espera mais 0,04 s
    ("o app ainda pode estar escrevendo os tipos") e le; restaura o clipboard
    SO se o numero mudou.

COMPORTAMENTO POR APP — `COPIA_LINHA_SEM_SELECAO`: em editor de codigo, Ctrl+C
sem selecao copia a LINHA INTEIRA. Como "atalho sem selecao" virou uso normal,
o Ctrl+C de reserva formataria e colaria codigo que ninguem selecionou. Regra:
`pode_usar_ctrl_c = not (origem == "atalho" and copia_linha_sem_selecao(exe))`.
Varinha e menu CONTINUAM usando Ctrl+C (clique = selecionou de proposito).

TERMINAIS: **nunca cola sozinho** (quebra de linha = Enter) — vira clipboard +
aviso.

SUCESSO E' MUDO. Pedido literal do usuário (18/09): "nao quero que nada fique
aparecendo — sai direto no local e tambem no cachezinho das 3 barrinhas".
Sem painel, sem notificacao. **So falha avisa.**

BOTAO FLUTUANTE AO SELECIONAR TEXTO: VETADO (veto 1). Nao existe codigo dele
aqui — nem atras de flag.
"""
from __future__ import annotations

import threading
import time

import clipboard
import colar
import config
import uia
import winapi
from log import log
from prompts import MOTIVO_PADRAO, MOTIVOS

AX_TIMEOUT_S = 0.35
COPIA_ESPERA_S = 0.7
COPIA_PASSO_S = 0.03
COPIA_FOLGA_S = 0.04

# Editores em que Ctrl+C SEM selecao copia a linha inteira (analogo da lista
# de bundles do Mac: VSCode, VSCodeInsiders, Cursor, VSCodium, Windsurf,
# Sublime 3/4, Zed).
COPIA_LINHA_SEM_SELECAO = frozenset({
    "code.exe", "code - insiders.exe", "cursor.exe", "codium.exe",
    "vscodium.exe", "windsurf.exe", "sublime_text.exe", "zed.exe",
})

# Em terminal NUNCA cola sozinho: quebra de linha vira Enter.
TERMINAIS = frozenset({
    "windowsterminal.exe", "wt.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "conhost.exe", "alacritty.exe", "wezterm-gui.exe", "kitty.exe",
    "hyper.exe", "mintty.exe", "putty.exe",
})

_OCUPADO = threading.Lock()
_ATIVIDADE = {"n": 0, "ignorar_ate": 0.0}


def registrar_atividade():
    """Chamado pelo gancho de teclado/mouse. Eventos dentro da janela
    `ignorar_ate` NAO contam (sao os nossos proprios)."""
    if time.monotonic() < _ATIVIDADE["ignorar_ate"]:
        return
    _ATIVIDADE["n"] += 1


def copia_linha_sem_selecao(exe: str) -> bool:
    return (exe or "").lower() in COPIA_LINHA_SEM_SELECAO


def e_terminal(exe: str) -> bool:
    return (exe or "").lower() in TERMINAIS


def pode_usar_ctrl_c(origem: str, exe: str) -> bool:
    return not (origem == "atalho" and copia_linha_sem_selecao(exe))


def _ler_via_ctrl_c():
    """Ctrl+C de reserva, com foto e cancelamento por numero de sequencia."""
    try:
        foto = clipboard.fotografar()
    except clipboard.ClipboardIndisponivel:
        return None, "clipboard-ocupado"
    seq0 = foto["seq"]
    _ATIVIDADE["ignorar_ate"] = time.monotonic() + COPIA_ESPERA_S + 0.3
    if not winapi.atalho_ctrl(winapi.VK_C):
        return None, "sendinput-recusado"
    limite = time.monotonic() + COPIA_ESPERA_S
    while time.monotonic() < limite:
        time.sleep(COPIA_PASSO_S)
        if clipboard.numero_de_sequencia() != seq0:
            time.sleep(COPIA_FOLGA_S)   # o app ainda pode estar escrevendo os tipos
            texto = clipboard.ler_texto()
            if foto["dados"]:
                clipboard.restaurar(foto)   # so restaura se o numero MUDOU
            return texto, "ctrl-c"
    return None, "ctrl-c-sem-resposta"


def ler_selecao(origem: str, exe: str):
    """(texto|None, via). `None` = nao consegui ler (diferente de "")."""
    t = uia.ler_selecao(AX_TIMEOUT_S)
    if t:
        return t, "uia"
    if not pode_usar_ctrl_c(origem, exe):
        # Barrado: zero chamadas de Ctrl+C, nada colado, nada no clipboard.
        return (t if t is not None else None), "uia-so"
    return _ler_via_ctrl_c()


def decidir_entrega(exe_inicial: str, exe_agora: str, atividade_inicial: int) -> str:
    """"clipboard" quando: `selecao_substituir` desligado; o app inicial e'
    terminal; trocou de app; OU o usuário clicou/digitou durante a formatacao."""
    if not config.get("selecao_substituir", True):
        return "clipboard"
    if e_terminal(exe_inicial):
        return "clipboard"
    if (exe_agora or "").lower() != (exe_inicial or "").lower():
        return "clipboard"
    if _ATIVIDADE["n"] != atividade_inicial:
        return "clipboard"
    return "colar"


def mensagem_de_falha(motivo: str) -> str:
    return MOTIVOS.get(motivo or "", MOTIVO_PADRAO)


class Selecao:
    """Fluxo de formatar a selecao. `avisar(msg)` e `pilula` sao injetaveis."""

    def __init__(self, fila_formatado, avisar=None, pilula=None,
                 historico_inserir=None):
        self.fila = fila_formatado
        self.avisar = avisar or (lambda m: None)
        self.pilula = pilula
        self.inserir = historico_inserir

    def acionar(self, origem="atalho"):
        """Ponto unico de entrada. Nao bloqueia quem chamou."""
        if not config.get("formatar_selecao", True):
            return "desligado"
        if not _OCUPADO.acquire(blocking=False):
            log(f"selecao: pedido concorrente ignorado (origem={origem})")
            return "ocupado"
        threading.Thread(target=self._fluxo, args=(origem,),
                         name="selecao-fluxo", daemon=True).start()
        return "ok"

    def _fluxo(self, origem):
        t0 = time.monotonic()
        try:
            hwnd = winapi.janela_em_foco()
            exe = winapi.exe_da_janela(hwnd)
            atividade0 = _ATIVIDADE["n"]

            pilula_estava_visivel = True
            if self.pilula is not None and config.get("selecao_atalho_traz_painel", True):
                pilula_estava_visivel = not self.pilula._escondido_por_usuario
                self.pilula.trazer_pra_frente()

            texto, via = ler_selecao(origem, exe)
            n = len(texto or "")
            if not texto:
                # REGRA: todo aperto tem exatamente UMA resposta visivel.
                # Sem selecao + pilula estava escondida -> so traz a pilula, MUDO.
                if not pilula_estava_visivel:
                    log(f"selecao: sem selecao, pilula trazida (mudo, via={via})")
                    return
                self.avisar("Selecione o texto primeiro e aperte de novo.")
                return
            minimo = int(config.get("selecao_min_chars", 20))
            maximo = int(config.get("selecao_max_chars", 12000))
            if n < minimo:
                if not pilula_estava_visivel:
                    return
                self.avisar("Selecione o texto primeiro e aperte de novo.")
                return
            if n > maximo:
                self.avisar(f"Selecao grande demais ({n} caracteres; maximo {maximo}).")
                return

            # O ORIGINAL vai pro historico ANTES de formatar — e' o "desfazer" do ≡.
            if self.inserir:
                self.inserir(texto, duracao_s=0, transcricao_s=0,
                             perfil="selecao", app=exe)

            pronto = threading.Event()
            caixa = {}

            def terminou(saida, motivo):
                caixa["saida"], caixa["motivo"] = saida, motivo
                pronto.set()

            if self.fila.enfileirar(texto, ref=None, ao_terminar=terminou) != "ok":
                return
            teto = float(config.get("formatado_timeout_s", 180)) + 10.0
            if not pronto.wait(timeout=teto):
                self.avisar(mensagem_de_falha("fail:timeout"))
                return
            saida, motivo = caixa.get("saida"), caixa.get("motivo")
            if not saida:
                self.avisar(mensagem_de_falha(motivo))
                return

            entrega = decidir_entrega(exe, winapi.app_em_foco(), atividade0)
            if entrega == "colar":
                _ATIVIDADE["ignorar_ate"] = time.monotonic() + 0.4
                colou, aviso = colar.colar_texto(saida, exe_alvo=exe)
                if not colou:
                    entrega = "clipboard"
                    if aviso:
                        self.avisar(aviso)
            if entrega == "clipboard":
                if clipboard.escrever_texto(saida):
                    self.avisar("Formatado copiado — Ctrl+V cola onde quiser.")
                else:
                    self.avisar(mensagem_de_falha(""))
                    return
            # SUCESSO E' MUDO: nenhum aviso aqui, de proposito.
            log(f"selecao: pronto ({time.monotonic() - t0:.1f}s, {n}c -> "
                f"{len(saida)}c, via={via}, origem={origem}, entrega={entrega}, "
                f"app={exe})")
        except Exception as e:
            log(f"selecao: fluxo quebrou ({type(e).__name__})")
        finally:
            try:
                _OCUPADO.release()
            except RuntimeError:
                pass
