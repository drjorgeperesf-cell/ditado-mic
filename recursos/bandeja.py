# -*- coding: utf-8 -*-
"""Icone na bandeja + menu — o lugar da barra de menus 🎤 do Mac.

Silencio total: nenhum item abre pasta, Explorer ou PDF. O painel web e' a
unica coisa que abre janela, e so quando o usuário clica.

SUBMENU "Microfone" (11/10/2026): os MESMOS itens do seletor da pilula
(`seletor_mic.itens_do_seletor`): automatico recomendado, um por aparelho
fisico, Bluetooth cinza, radio no que vale; escolher grava a CHAVE em
`manual_mic_nome`. A lista e' a da MEMORIA (`atualizar=False`): o pystray
avalia o submenu 5 vezes por montagem (__bool__/__iter__/visible) e remonta
depois de TODO clique no menu — com `atualizar=True` eram 5 Pa_Terminate +
Pa_Initialize a cada montagem. Recarregar de verdade so pelo "Atualizar
lista", que so ENFILEIRA na fila do app (so roda com o app ocioso ou em erro).

O MENU E' REMONTADO NO CLIQUE DIREITO, antes de abrir, NA THREAD DO PYSTRAY.
O pystray do Windows mostra um HMENU montado antes (uma foto): sem isso, a
escolha feita na pilula nao apareceria no radio. Remontar de OUTRA thread seria
corrida (o `_update_menu` destroi o HMENU velho, que pode estar aberto).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import config
import seletor_mic
from log import base_dados, log
from proc import CREATE_NO_WINDOW

CACHE_MICS_S = 1.0          # uma leitura por montagem (nao 5)


def _imagem():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 6, 56, 40), radius=20, fill=(235, 56, 56, 255))
    d.rectangle((29, 40, 35, 52), fill=(235, 56, 56, 255))
    d.rectangle((20, 52, 44, 57), fill=(235, 56, 56, 255))
    return img


def _abrir_painel():
    """Sobe o painel e abre o navegador — so por clique dele."""
    pasta = Path(__file__).resolve().parent.parent
    bat = pasta / "PAINEL.bat"
    if bat.exists():
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(pasta),
                         creationflags=CREATE_NO_WINDOW)
    else:
        log("bandeja: PAINEL.bat nao encontrado")


def _ler_mics():
    """A lista EM MEMORIA do PortAudio. NUNCA `atualizar=True` aqui."""
    import captura
    return captura.listar_microfones(atualizar=False)


def rotulo_do_item(it) -> str:
    """Texto do item no menu da bandeja (o detalhe vai junto, numa linha)."""
    detalhe = it.get("detalhe") or ""
    if not detalhe:
        return it["rotulo"]
    if detalhe == seletor_mic.DETALHE_BT:
        return f"{it['rotulo']}  ({detalhe})"
    return f"{it['rotulo']} — {detalhe}"


def remontar_ao_abrir(icone, antes_de_abrir) -> bool:
    """Embrulha o tratador de WM_NOTIFY do pystray: no clique DIREITO no icone,
    chama `antes_de_abrir()` (remonta o menu) e SO ENTAO deixa o pystray abrir.
    Roda na thread do proprio pystray, entao nunca cruza com o menu aberto.
    Clique esquerdo e o resto passam intocados. O tratador original fica em
    `.original` (o teste troca por um duble: nada de menu na tela).
    Devolve False (e loga) se o pystray nao tiver o que embrulhar."""
    try:
        from pystray._util import win32 as w32
        tabela = icone._message_handlers
        original = tabela[w32.WM_NOTIFY]
        clique_direito = w32.WM_RBUTTONUP
    except Exception:
        log("bandeja: menu sem remontagem no clique direito (pystray diferente)")
        return False

    def ao_notificar(wparam, lparam):
        if lparam == clique_direito:
            try:
                antes_de_abrir()
            except Exception as e:
                log(f"bandeja: remontagem do menu falhou ({type(e).__name__})")
        return ao_notificar.original(wparam, lparam)
    ao_notificar.original = original
    tabela[w32.WM_NOTIFY] = ao_notificar
    return True


def criar(app):
    import pystray
    from pystray import Menu, MenuItem as Item

    def acionar(icone=None, item=None):
        app.acionar_gravacao()

    def mostrar(icone=None, item=None):
        if app.pilula:
            app.pilula.trazer_pra_frente()

    def esconder(icone=None, item=None):
        if app.pilula:
            app.pilula.esconder(por_usuario=True)

    def formatar(icone=None, item=None):
        if app.selecao:
            app.selecao.acionar("menu")

    def recolar(icone=None, item=None):
        if app.ultimo_texto:
            app.colar_fn(app.ultimo_texto)

    def historico_janela(icone=None, item=None):
        import janelinha
        janelinha.alternar(app)

    def painel(icone=None, item=None):
        threading.Thread(target=_abrir_painel, daemon=True).start()

    cache = {"t": 0.0, "mics": None}

    def mics_do_menu():
        agora = time.monotonic()
        if cache["mics"] is None or agora - cache["t"] > CACHE_MICS_S:
            try:
                cache["mics"] = _ler_mics()
            except Exception as e:
                log(f"bandeja: lista de microfones ilegivel ({type(e).__name__})")
                cache["mics"] = []
            cache["t"] = agora
        return cache["mics"]

    def escolher_mic(chave):
        def fn(icone=None, item=None):
            # SINCRONO: o pystray remonta o menu logo depois deste clique e o
            # radio ja tem que mostrar a escolha nova
            seletor_mic.gravar_escolha(chave, app)
        return fn

    def atualizar_lista(icone=None, item=None):
        """So ENFILEIRA (a fila do app decide se pode) e volta na hora: a
        bandeja nunca espera o PortAudio. A lista nova aparece no proximo
        clique direito (o menu e' remontado antes de abrir)."""
        try:
            app.pedir_recarga_mics()
        except Exception as e:
            log(f"bandeja: atualizar lista falhou ({type(e).__name__})")
        cache["mics"] = None

    def menu_mic():
        itens = []
        for it in seletor_mic.itens_do_seletor(mics_do_menu()):
            itens.append(Item(
                rotulo_do_item(it),
                escolher_mic(it["chave"]) if it["habilitado"] else None,
                checked=(lambda m: lambda _i: m)(bool(it["marcado"])),
                radio=True, enabled=bool(it["habilitado"])))
        itens += [Menu.SEPARATOR, Item("Atualizar lista", atualizar_lista)]
        return Menu(*itens)

    def sair(icone, item=None):
        log("bandeja: sair (descarregar)")
        from proc import marcar_saida_intencional
        marcar_saida_intencional()   # o Guardiao nao religa o que ele fechou
        app.encerrar()
        icone.stop()

    menu = Menu(
        Item("Acionar gravacao agora", acionar, default=True),
        Item("Mostrar painel", mostrar),
        Item("Esconder painel", esconder),
        Menu.SEPARATOR,
        Item("Textos ditados (≡)", historico_janela),
        Item("Formatar selecao", formatar),
        Item("Recolar ultima transcricao", recolar),
        Item("Painel web (historico e saude)", painel),
        Menu.SEPARATOR,
        Item("Microfone", Menu(lambda: menu_mic().items)),
        Menu.SEPARATOR,
        Item("Sair (descarregar)", sair),
    )
    icone = pystray.Icon("microfone", _imagem(), "Microfone", menu)

    def antes_de_abrir():
        """Clique direito no icone: le a lista (em memoria) e o config de novo
        e remonta — o radio mostra a escolha feita na pilula."""
        cache["mics"] = None
        icone.update_menu()
    remontar_ao_abrir(icone, antes_de_abrir)
    return icone
