# -*- coding: utf-8 -*-
"""Colagem — o coracao do "nao perder texto" (app.py:3712-3803, secao 2.16).

SEQUENCIA EXATA, na ordem, cada passo pago com um incidente:
 1. **Foto** de TODOS os items+tipos do clipboard.
 2. Escreve o texto e **CONFERE relendo**. (`setString` devolvia False sem
    excecao quando um gerenciador de clipboard tomava ownership; sem o check,
    o Ctrl+V colava NADA e o log dizia "colado".)
 3. Escrita falhou -> **aborta ANTES do Ctrl+V** e devolve a foto (nunca
    deixar o clipboard vazio).
 4. `sleep(0.04)`.
 5. **Ctrl+V** via SendInput. `SendInput` NAO alcanca janela de processo
    ELEVADO (UIPI): detecta e AVISA, em vez de falhar calado — e sem som.
 6. **Restore** apos 0,6 s (1,2 s em apps Electron, que processam o paste
    DEPOIS do restore e colavam o conteudo ANTIGO).
 7. **Numero de sequencia antes do restore:** se alguem mexeu no clipboard na
    janela de restore, o restore e' CANCELADO.
 8. Paste falhou mas o texto esta no clipboard: **NAO restaura** — deixa la e
    avisa "cole com Ctrl+V".
 9. Nem o clipboard tem o texto: restaura o antigo e manda recuperar no
    historico — **sem mentir que "esta no clipboard"**.
10. Devolve bool: quem chama precisa saber se o Ctrl+V realmente saiu.
"""
from __future__ import annotations

import os
import threading
import time

import clipboard
import config
import winapi
from log import log
from proc import integridade_do_processo

# Resultado: (colou, mensagem_ou_None, detalhe)
MSG_ELEVADO = ("A janela em foco roda como administrador — o Windows nao deixa "
               "colar nela. O texto esta no clipboard: cole com Ctrl+V.")
MSG_CLIPBOARD = "Nao deu pra colar. O texto esta no clipboard: cole com Ctrl+V."
MSG_PERDEU = "Nao deu pra colar nem copiar. O texto esta no historico (botao ≡)."


def _demora_do_restore(exe: str) -> float:
    lentos = [str(x).lower() for x in (config.get("slow_paste_exes") or [])]
    if (exe or "").lower() in lentos:
        return float(config.get("paste_restore_delay_slow_s", 1.2))
    return float(config.get("paste_restore_delay_s", 0.6))


def janela_alvo_e_elevada() -> bool | None:
    """A janela em foco pertence a um processo de integridade mais alta que a
    nossa? None = nao sei (nunca tratar "nao sei" como "quebrado")."""
    try:
        hwnd = winapi.janela_em_foco()
        if not hwnd:
            return None
        alvo = integridade_do_processo(winapi.pid_da_janela(hwnd))
        eu = integridade_do_processo(os.getpid())
        if alvo is None or eu is None:
            return None
        return alvo > eu
    except Exception:
        return None


def colar_texto(texto: str, exe_alvo: str = "", enviar_atalho=None,
                dormir=time.sleep):
    """Coreografia completa. Devolve (colou: bool, aviso: str|None).

    `enviar_atalho` e' injetavel em teste (default: Ctrl+V de verdade).
    """
    if not texto:
        return False, None
    enviar = enviar_atalho or (lambda: winapi.atalho_ctrl(winapi.VK_V))

    # 1) foto
    try:
        foto = clipboard.fotografar()
    except clipboard.ClipboardIndisponivel:
        foto = {"seq": clipboard.numero_de_sequencia(), "dados": {}, "ignorados": []}
    restaurar = bool(config.get("paste_restore_clipboard", True)) and foto["dados"]

    # 2) escreve e CONFERE
    if not clipboard.escrever_texto(texto):
        # 3) nunca deixar o clipboard vazio
        if foto["dados"]:
            clipboard.restaurar(foto)
        log(f"colar: escrita no clipboard recusada ({len(texto)}c) — Ctrl+V nao enviado")
        return False, MSG_PERDEU

    # 5) elevacao: avisa em vez de falhar calado
    elevada = janela_alvo_e_elevada()
    if elevada:
        log("colar: janela em foco e' de processo elevado — SendInput nao alcanca")
        return False, MSG_ELEVADO

    dormir(0.04)
    seq_nossa = clipboard.numero_de_sequencia()
    ok_envio = enviar()
    if not ok_envio:
        log(f"colar: SendInput recusado ({len(texto)}c) — texto fica no clipboard")
        return False, MSG_CLIPBOARD

    # 6+7) restore com cancelamento por mudanca de sequencia
    if restaurar:
        def _restore():
            time.sleep(_demora_do_restore(exe_alvo))
            agora = clipboard.numero_de_sequencia()
            if agora != seq_nossa:
                log("colar: clipboard mudou na janela de restore — restore CANCELADO")
                return
            if not clipboard.tem_exatamente(texto):
                log("colar: clipboard nao e' mais o nosso texto — restore cancelado")
                return
            if clipboard.restaurar(foto):
                log(f"colar: clipboard restaurado ({len(foto['dados'])} formatos)")
            else:
                log("colar: restore parcial do clipboard")

        threading.Thread(target=_restore, name="clip-restore", daemon=True).start()
    log(f"colar: texto colado ({len(texto)}c, app={exe_alvo or '?'}, "
        f"restore={'sim' if restaurar else 'nao'})")
    return True, None
