# -*- coding: utf-8 -*-
"""Atalhos globais por combinacao (recursos do Wispr Flow, 27/10/2026).

  - "colar o ultimo ditado de novo"  (padrao Win+Alt+V)
  - "instrucao falada sobre a selecao" (padrao Win+Alt+C)

POR QUE RegisterHotKey E NAO O GANCHO: o gancho WH_KEYBOARD_LL do app e'
PASSIVO (nunca engole tecla, senao um modificador fica PRESO). Uma
combinacao registrada pelo Windows e' entregue so para nos, sem engolir nada
pela metade e sem injetar tecla. Thread propria com bomba de mensagens.

REGRAS DA COMBINACAO (`interpretar`):
  - exige Win ou Ctrl (Alt+letra e' atalho de menu; letra sozinha roubaria
    a digitacao);
  - **Ctrl+Alt sem Win e' RECUSADO**: Ctrl+Alt = AltGr. Registrar
    Ctrl+Alt+Q roubaria o "/" do ABNT2 (AltGr+Q) e os acentos dele.
  - so letra, numero ou F1-F24.

Silencio total: registro que falha (outro app ja tem a combinacao) vira so
uma linha de log. Nenhuma janela, nenhum som.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from log import log

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_NOMES_MOD = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN,
}


def _vk_da_tecla(nome: str) -> int | None:
    n = nome.strip().lower()
    if len(n) == 1 and ("a" <= n <= "z"):
        return ord(n.upper())
    if len(n) == 1 and n.isdigit():
        return ord(n)
    if n.startswith("f") and n[1:].isdigit():
        i = int(n[1:])
        if 1 <= i <= 24:
            return 0x70 + i - 1
    return None


def interpretar(texto) -> tuple[int, int] | None:
    """"win+alt+v" -> (mods, vk). None = invalida ou PERIGOSA (ver topo)."""
    if not isinstance(texto, str) or not texto.strip():
        return None
    partes = [p.strip().lower() for p in texto.split("+") if p.strip()]
    if len(partes) < 2:
        return None
    mods = 0
    for p in partes[:-1]:
        m = _NOMES_MOD.get(p)
        if m is None:
            return None
        mods |= m
    vk = _vk_da_tecla(partes[-1])
    if vk is None:
        return None
    if not (mods & (MOD_WIN | MOD_CONTROL)):
        return None
    # Ctrl+Alt = AltGr: roubaria os simbolos e acentos do ABNT2/US-Intl.
    if (mods & MOD_CONTROL) and (mods & MOD_ALT) and not (mods & MOD_WIN):
        return None
    return mods, vk


def esperar_soltar_modificadores(teto_s=3.0, presos=None, dormir=time.sleep,
                                 relogio=time.monotonic) -> bool:
    """Espera ele SOLTAR todo modificador fisico. True = soltou.

    Colar com o Win ainda segurado: o Ctrl+V sintetico solta e devolve o Win,
    e o keyup fisico seguinte, "sozinho", abre o menu Iniciar (janela vindo
    pra frente = o que mais o incomoda). Entao a gente espera."""
    if presos is None:
        import winapi
        presos = winapi.modificadores_presos_agora
    fim = relogio() + max(0.0, float(teto_s))
    while True:
        if not presos():
            return True
        if relogio() >= fim:
            return False
        dormir(0.02)


class AtalhosGlobais:
    """Registra combinacoes numa thread propria e chama o callback em thread
    daemon (nunca na bomba de mensagens: ela tem que voltar rapido)."""

    def __init__(self):
        self._pedidos: list[tuple[int, int, int, object, str]] = []
        self._registrados: dict[int, tuple[object, str]] = {}
        self._thread = None
        self._tid = 0
        self._pronto = threading.Event()
        self._prox_id = 0x4D31          # ids proprios, longe de 0

    def adicionar(self, combinacao: str, callback, nome: str) -> bool:
        """Antes de `iniciar()`. False = combinacao invalida/perigosa."""
        r = interpretar(combinacao)
        if r is None:
            log(f"atalho: '{combinacao}' recusado para {nome} (precisa de Win "
                f"ou Ctrl; Ctrl+Alt sem Win e' o AltGr)")
            return False
        mods, vk = r
        self._prox_id += 1
        self._pedidos.append((self._prox_id, mods, vk, callback, nome))
        return True

    def iniciar(self, espera_s=2.0) -> int:
        """Sobe a thread. Devolve quantos atalhos o Windows aceitou."""
        if not self._pedidos:
            return 0
        self._thread = threading.Thread(target=self._loop, name="atalhos-globais",
                                        daemon=True)
        self._thread.start()
        self._pronto.wait(timeout=espera_s)
        return len(self._registrados)

    @property
    def registrados(self) -> list[str]:
        return [nome for _, nome in self._registrados.values()]

    def _loop(self):
        u = ctypes.windll.user32
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                     wintypes.UINT, wintypes.UINT]
        u.RegisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # garante a fila de mensagens desta thread antes de registrar
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        for ident, mods, vk, cb, nome in self._pedidos:
            if u.RegisterHotKey(None, ident, mods | MOD_NOREPEAT, vk):
                self._registrados[ident] = (cb, nome)
                log(f"atalho: {nome} registrado")
            else:
                log(f"atalho: {nome} NAO registrado (combinacao ja e' de "
                    f"outro app) — o resto do app segue normal")
        self._pronto.set()
        try:
            while True:
                r = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    alvo = self._registrados.get(int(msg.wParam))
                    if alvo:
                        cb, nome = alvo
                        threading.Thread(target=_chamar, args=(cb, nome),
                                         name=f"atalho-{nome}", daemon=True).start()
        finally:
            for ident in list(self._registrados):
                try:
                    u.UnregisterHotKey(None, ident)
                except Exception:
                    pass
            self._registrados.clear()

    def parar(self):
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)


def _chamar(cb, nome):
    try:
        cb()
    except Exception as e:
        log(f"atalho: {nome} falhou ({type(e).__name__})")
