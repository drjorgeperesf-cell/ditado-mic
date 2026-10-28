# -*- coding: utf-8 -*-
"""M0 — seletor da tecla de ditar. Roda COM O USUARIO na frente do PC.

No Mac: Command direito dita, Control direito formata/corrige.
Aqui o Control direito continua igual. O Command direito nao existe e
**Alt direito e' PROIBIDO**: e' o AltGr do teclado ABNT2 — usar quebraria
`/ ? ç`.

O seletor:
 - poe o gancho `WH_KEYBOARD_LL` em modo PASSIVO (nao engole tecla nenhuma);
 - RECUSA AltGr/Alt direito, os outros modificadores e QUALQUER tecla que
   produza caractere (testado com `ToUnicodeEx` no layout ATIVO, nao por lista);
 - sugere Menu, Windows direito ou uma F livre;
 - grava a escolha no config.

Roda num console (INSTALAR.bat / CONFIGURAR-TECLA.bat). Nada de janela pulando.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

import config
import winapi

user32 = winapi.user32


def produz_caractere(vk: int, scancode: int) -> bool:
    """A tecla gera caractere no layout ATIVO?

    `ToUnicodeEx` com o estado de teclado zerado. Testa por COMPORTAMENTO, nao
    por lista: se o usuário trocar para o layout US-Internacional (ele tem os
    dois), a resposta muda junto. Um buffer nao-vazio = a tecla digita algo e
    nao serve como hotkey de ditado.
    """
    layout = user32.GetKeyboardLayout(0)
    estado = (ctypes.c_ubyte * 256)()
    buf = ctypes.create_unicode_buffer(8)
    n = user32.ToUnicodeEx(wintypes.UINT(vk), wintypes.UINT(scancode),
                           estado, buf, 8, wintypes.UINT(0), layout)
    if n > 0:
        # Limpa o estado de dead-key que o ToUnicodeEx possa ter deixado.
        user32.ToUnicodeEx(wintypes.UINT(vk), wintypes.UINT(scancode),
                           estado, buf, 8, wintypes.UINT(0), layout)
        return any(ch.isprintable() and not ch.isspace() for ch in buf.value)
    return False


def avaliar(vk: int, scancode: int, extended: bool):
    """(aceita: bool, motivo: str). O motivo e' o que o usuário le na tela."""
    if vk in config.VK_PROIBIDOS:
        return False, config.VK_PROIBIDOS[vk]
    if vk == 0xA5 or (vk == 0x12 and extended):
        return False, ("Alt direito (AltGr) quebraria / ? ç no teclado ABNT2")
    if produz_caractere(vk, scancode):
        return False, "essa tecla digita um caractere — voce perderia ela"
    return True, ""


def nome_da_tecla(vk: int, scancode: int, extended: bool) -> str:
    conhecidos = {0x5D: "Menu (aplicacao)", 0x5C: "Windows direito",
                  0x5B: "Windows esquerdo", 0x91: "Scroll Lock",
                  0x13: "Pause", 0x2D: "Insert", 0x2C: "Print Screen"}
    if vk in conhecidos:
        return conhecidos[vk]
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    buf = ctypes.create_unicode_buffer(64)
    lparam = (scancode << 16) | (0x01000000 if extended else 0)
    if user32.GetKeyNameTextW(lparam, buf, 64):
        return buf.value
    return f"VK 0x{vk:02X}"


def escolher(timeout_s=180.0):
    """Roda o seletor no console. Devolve dict com a escolha, ou None.

    Sem console interativo (rodado por tarefa, pipe ou script), NAO fica 3
    minutos pendurado esperando uma tecla que ninguem vai apertar: avisa e
    devolve None. Escolher a tecla e' uma acao DELE, com ele na frente do PC.
    """
    if not sys.stdin or not sys.stdin.isatty():
        print()
        print("  O seletor de tecla precisa de voce na frente do PC.")
        print("  Abra o CONFIGURAR-TECLA.bat com dois cliques.")
        return None
    print()
    print("  ESCOLHA A TECLA DE DITAR")
    print("  " + "-" * 56)
    print("  No Mac voce usava o Command direito. Aqui ele nao existe.")
    print("  O Control DIREITO continua sendo o botao de FORMATAR/CORRIGIR.")
    print()
    print("  Boas opcoes (nenhuma delas digita nada):")
    for vk, nome in config.VK_SUGERIDOS:
        print(f"    - {nome}")
    print()
    print("  Aperte AGORA a tecla que voce quer usar para DITAR.")
    print("  (Enter sem apertar nada = cancelar)")
    print()

    escolhido = {}
    pronto = threading.Event()

    def ao_evento(vk, pressionada, extended, scancode=0, quando_ms=None):
        if not pressionada or pronto.is_set():
            return
        sc = user32.MapVirtualKeyW(vk, 0)
        ok, motivo = avaliar(vk, sc, extended)
        nome = nome_da_tecla(vk, sc, extended)
        if not ok:
            print(f"  [recusada] {nome}: {motivo}")
            print("  Tente outra.")
            return
        escolhido.update({"vk": vk, "nome": nome, "extended": bool(extended)})
        pronto.set()

    g = winapi.GanchoTeclado(ao_evento)      # PASSIVO: nao suprime nada
    if not g.iniciar():
        print("  ERRO: nao consegui instalar o gancho de teclado.")
        return None
    try:
        limite = time.monotonic() + timeout_s
        while not pronto.is_set() and time.monotonic() < limite:
            time.sleep(0.05)
    finally:
        g.parar()
    if not escolhido:
        print("  Nenhuma tecla escolhida.")
        return None
    print()
    print(f"  Tecla de ditar: {escolhido['nome']}")
    return escolhido


def escolha_alt_direito() -> dict:
    """O caminho AUTORIZADO para o Alt direito (decisão do usuário, 20/09/2026).

    O seletor interativo continua RECUSANDO a tecla para quem não sabe que ela
    é o AltGr; este é o caminho explícito, usado pelo
    `CONFIGURAR-TECLA.bat --alt-direito` e pelo instalador não interativo.
    """
    return {"vk": 0xA5, "nome": "Alt direito", "extended": True,
            "altgr_autorizado": True}


def gravar(escolha: dict, suprimir=None):
    """Grava no config. `suprimir=True` faz o app ENGOLIR a tecla (util no
    Windows direito, que abriria o Menu Iniciar)."""
    vk = int(escolha["vk"])
    if suprimir is None:
        suprimir = vk in (0x5B, 0x5C)              # Win esq/dir
    if vk == 0xA5:
        # O Alt direito NUNCA pode ser engolido: AltGr+tecla tem que passar.
        suprimir = False
    mudancas = {
        "hotkey_ditar_vk": vk,
        "hotkey_ditar_nome": str(escolha["nome"]),
        "hotkey_ditar_extended": bool(escolha.get("extended", True)),
        "hotkey_ditar_suprimir": bool(suprimir),
    }
    if escolha.get("altgr_autorizado"):
        mudancas["tecla_ditar_altgr_autorizado"] = True
    config.gravar(mudancas)


def main():
    if "--alt-direito" in sys.argv:
        e = escolha_alt_direito()
        gravar(e)
        print()
        print("  Tecla de ditar: Alt direito (AltGr) — caminho autorizado.")
        print("  Regra: Alt direito SOZINHO dita. AltGr+tecla continua")
        print("  digitando acento normalmente.")
        print(f"  Gravado em {config.caminho_config()}")
        return 0
    e = escolher()
    if not e:
        return 1
    gravar(e)
    print(f"  Gravado em {config.caminho_config()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
