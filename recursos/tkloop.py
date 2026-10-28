# -*- coding: utf-8 -*-
"""UMA thread de tkinter para o processo inteiro.

POR QUE ISTO EXISTE (bug real, pego pelo teste da pilula em 20/09/2026):
a pilula e a janelinha ≡ sao duas janelas. Se cada uma criar o seu proprio
`tk.Tk()` numa thread propria, o Tcl fica com dois interpretadores em threads
diferentes e, no fim do processo, derruba com
`Tcl_AsyncDelete: async handler deleted by the wrong thread`. Em teste isso
vira falha fantasma; em producao, o app morre ao sair.

Desenho: uma raiz ESCONDIDA numa thread dedicada. Toda janela e' um
`Toplevel` dela. Qualquer thread agenda trabalho com `executar(fn)`, que cai
numa fila drenada pela raiz a cada 10 ms. Nenhum chamador externo guarda a
raiz para agendar trabalho: ate' a ultima referencia Python ao interpretador
deve ser liberada na thread dona, depois de destruir as janelas.
"""
from __future__ import annotations

import gc
import queue
import sys
import threading

_LOCK = threading.Lock()
_RAIZ = None
_THREAD = None
_PRONTA = threading.Event()
_COMANDOS = queue.Queue()
_ENCERRANDO = threading.Event()
_PARAR_LOOP = threading.Event()


def _loop():
    global _RAIZ
    import tkinter as tk
    raiz_tk = None
    try:
        raiz_tk = tk.Tk()
        _RAIZ = raiz_tk
        raiz_tk.withdraw()                 # a raiz nunca aparece
        raiz_tk.overrideredirect(True)

        def processar():
            # A fila impede que o chamador mantenha referencias ao objeto Tk
            # enquanto espera a thread dona encerrar. `after` tambem so e'
            # chamado na thread que criou o interpretador Tcl.
            for _ in range(32):
                try:
                    fn, args, kwargs = _COMANDOS.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(*args, **kwargs)
                except Exception:
                    raiz_tk.report_callback_exception(*sys.exc_info())
                finally:
                    del fn, args, kwargs
                if _PARAR_LOOP.is_set():
                    return
            raiz_tk.after(10, processar)

        raiz_tk.after(0, processar)
        _PRONTA.set()
        raiz_tk.mainloop()
    finally:
        _PRONTA.clear()
        if raiz_tk is not None:
            try:
                raiz_tk.destroy()
            except Exception:
                pass
        # A destruicao da janela nao libera o interpretador. A ULTIMA
        # referencia Python ao Tk tambem precisa morrer na thread dona.
        _RAIZ = None
        raiz_tk = None
        while True:
            try:
                comando = _COMANDOS.get_nowait()
            except queue.Empty:
                break
            del comando
        gc.collect()


def iniciar(timeout=10.0) -> bool:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _PRONTA.wait(timeout=timeout)
        _PRONTA.clear()
        _ENCERRANDO.clear()
        _PARAR_LOOP.clear()
        _THREAD = threading.Thread(target=_loop, name="tkloop", daemon=True)
        _THREAD.start()
    return _PRONTA.wait(timeout=timeout)


def raiz():
    iniciar()
    return _RAIZ


def executar(fn, *a, **kw):
    """Agenda `fn` na thread do tkinter. Nao espera resultado."""
    if _RAIZ is None:
        return False
    try:
        if threading.current_thread() is _THREAD:
            fn(*a, **kw)
            return True
        if _ENCERRANDO.is_set():
            return False
        _COMANDOS.put((fn, a, kw))
        return True
    except Exception:
        return False


def executar_e_esperar(fn, timeout=8.0):
    """Roda `fn()` na thread do tkinter e devolve o resultado (ou levanta)."""
    if threading.current_thread() is _THREAD:
        return fn()
    iniciar()
    fila: queue.Queue = queue.Queue(maxsize=1)

    def alvo():
        try:
            fila.put(("ok", fn()))
        except Exception as e:                   # noqa: BLE001
            fila.put(("erro", e))
    if not executar(alvo):
        raise RuntimeError("tkloop nao esta de pe")
    tipo, valor = fila.get(timeout=timeout)
    if tipo == "erro":
        raise valor
    return valor


def encerrar(timeout=3.0):
    """Derruba a raiz DENTRO da thread dona e espera ela sair."""
    global _THREAD
    t = _THREAD
    if _RAIZ is None:
        return
    def _fim():
        _PARAR_LOOP.set()
        try:
            _RAIZ.quit()
            _RAIZ.destroy()
        except Exception:
            pass
    if t is threading.current_thread():
        _ENCERRANDO.set()
        _fim()
        return
    if not _ENCERRANDO.is_set():
        _ENCERRANDO.set()
        _COMANDOS.put((_fim, (), {}))
    if t and t.is_alive():
        t.join(timeout=timeout)
    # Se o timeout venceu, a thread continua dona de tudo ate' terminar.
    if t is not None and not t.is_alive():
        _THREAD = None
