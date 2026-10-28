# -*- coding: utf-8 -*-
"""Area de transferencia: fotografar TODOS os formatos, escrever, restaurar.

Licoes 6.5 do inventario, uma a uma:
- `setString` pode devolver False SEM excecao (um gerenciador de clipboard —
  aqui, o Ditto do usuário — tomou ownership). Sem checar, voce manda Ctrl+V com
  o clipboard vazio e loga "colado". Aqui TODA escrita confere relendo.
- **Nunca deixar o clipboard vazio**: se a troca falhar depois do clear,
  devolve o conteudo anterior.
- `changeCount` -> `GetClipboardSequenceNumber`: se o numero mudou na janela
  de restore, o restore e' CANCELADO (o usuário copiou outra coisa).
- So trocar o clipboard se ele ainda for EXATAMENTE o texto que colocamos la.

A foto guarda TODOS os formatos (CF_BITMAP/CF_DIB/CF_HDROP/HTML/RTF...), nao
so texto: o aceite exige que uma IMAGEM no clipboard volte intacta byte a byte.
Formatos delegados (renderizados sob demanda pelo dono, ponteiro NULL) sao
materializados na foto — senao a imagem "volta" vazia.
"""
from __future__ import annotations

import ctypes
import time
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_SYLK = 4
CF_DIF = 5
CF_TIFF = 6
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_PENDATA = 10
CF_RIFF = 11
CF_WAVE = 12
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17

GMEM_MOVEABLE = 0x0002

# Formatos que NAO se copiam como bloco de memoria (sao HANDLE de objeto GDI).
# CF_DIB/CF_DIBV5 cobrem o mesmo conteudo do CF_BITMAP como bytes, entao a
# imagem sobrevive a foto sem precisar clonar HBITMAP.
_NAO_COPIAVEIS = {CF_BITMAP, CF_PALETTE, CF_METAFILEPICT, CF_ENHMETAFILE,
                  CF_PENDATA}

user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardFormatNameW.argtypes = (wintypes.UINT, wintypes.LPWSTR, ctypes.c_int)
user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
user32.RegisterClipboardFormatW.restype = wintypes.UINT


class ClipboardIndisponivel(Exception):
    pass


# ARMADILHA MEDIDA AQUI (20/09/2026): abrir o clipboard com handle de janela
# NULL faz `EmptyClipboard` "dar certo" mas deixar o DONO do clipboard NULL —
# e ai **todo `SetClipboardData` falha**, silenciosamente. Era exatamente o
# modo de falha que o Mac descreve como "`setString` devolve False sem
# excecao": escrita recusada, Ctrl+V colando nada e log dizendo "colado".
# Por isso o modulo tem a sua propria janela message-only (HWND_MESSAGE) e
# TODO OpenClipboard usa ela.
_HWND_DONO = None
_DONO_THREAD = None
_DONO_PRONTO = threading.Event()
_DONO_LOCK = threading.Lock()
_CLIP_LOCK = threading.RLock()
HWND_MESSAGE = -3


def _loop_dono():
    global _HWND_DONO
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _HWND_DONO = user32.CreateWindowExW(
        0, "STATIC", "MicrofoneClipboard", 0, 0, 0, 0, 0,
        wintypes.HWND(HWND_MESSAGE), None, None, None)
    _DONO_PRONTO.set()
    if not _HWND_DONO:
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _hwnd_dono():
    """A janela pertence a uma thread permanente, nao ao primeiro ditado.

    O Windows destroi janelas quando a thread dona sai. Guardar o HWND da
    thread temporaria do pipeline fazia TODO ditado seguinte falhar (1400).
    """
    global _DONO_THREAD
    with _DONO_LOCK:
        if not _DONO_THREAD or not _DONO_THREAD.is_alive():
            _DONO_PRONTO.clear()
            _DONO_THREAD = threading.Thread(target=_loop_dono,
                                            name="clipboard-dono", daemon=True)
            _DONO_THREAD.start()
    if not _DONO_PRONTO.wait(timeout=2.0) or not _HWND_DONO:
        raise ClipboardIndisponivel("Janela dona do clipboard nao iniciou")
    return _HWND_DONO


class _Aberto:
    """OpenClipboard com retentativa: outro app pode estar segurando.

    (O usuário usa o Ditto como gerenciador de clipboard — ele abre o clipboard
    a cada copia, entao a retentativa nao e' teorica.)
    """

    def __init__(self, tentativas=12, espera=0.02):
        self.t, self.e = tentativas, espera

    def __enter__(self):
        _CLIP_LOCK.acquire()
        try:
            h = _hwnd_dono()
            for _ in range(self.t):
                if user32.OpenClipboard(h):
                    return self
                time.sleep(self.e)
            raise ClipboardIndisponivel("OpenClipboard falhou")
        except Exception:
            _CLIP_LOCK.release()
            raise

    def __exit__(self, *a):
        try:
            user32.CloseClipboard()
        finally:
            _CLIP_LOCK.release()
        return False


def numero_de_sequencia() -> int:
    """GetClipboardSequenceNumber — o `changeCount` do Mac."""
    return int(user32.GetClipboardSequenceNumber())


def nome_do_formato(fmt: int) -> str:
    padrao = {CF_TEXT: "CF_TEXT", CF_BITMAP: "CF_BITMAP", CF_DIB: "CF_DIB",
              CF_DIBV5: "CF_DIBV5", CF_UNICODETEXT: "CF_UNICODETEXT",
              CF_HDROP: "CF_HDROP", CF_OEMTEXT: "CF_OEMTEXT",
              CF_LOCALE: "CF_LOCALE", CF_ENHMETAFILE: "CF_ENHMETAFILE",
              CF_PALETTE: "CF_PALETTE", CF_METAFILEPICT: "CF_METAFILEPICT",
              CF_WAVE: "CF_WAVE", CF_TIFF: "CF_TIFF"}
    if fmt in padrao:
        return padrao[fmt]
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClipboardFormatNameW(fmt, buf, 256)
    return buf.value if n else f"fmt:{fmt}"


def _formatos_disponiveis() -> list[int]:
    fmts, f = [], 0
    while True:
        f = user32.EnumClipboardFormats(f)
        if not f:
            break
        fmts.append(int(f))
    return fmts


def fotografar() -> dict:
    """Foto de TODOS os formatos do clipboard, como bytes.

    Devolve {"seq": n, "dados": {fmt: bytes}, "ignorados": [fmt...]}.
    Formatos delegados (handle NULL) sao materializados aqui: chamar
    GetClipboardData forca o dono a renderizar. Se mesmo assim vier NULL,
    o formato entra em "ignorados" e a restauracao diz que foi parcial.
    """
    dados, ignorados = {}, []
    with _Aberto():
        seq = numero_de_sequencia()
        for fmt in _formatos_disponiveis():
            if fmt in _NAO_COPIAVEIS:
                ignorados.append(fmt)
                continue
            h = user32.GetClipboardData(fmt)
            if not h:
                ignorados.append(fmt)
                continue
            p = kernel32.GlobalLock(h)
            if not p:
                ignorados.append(fmt)
                continue
            try:
                n = kernel32.GlobalSize(h)
                dados[fmt] = ctypes.string_at(p, n) if n else b""
            finally:
                kernel32.GlobalUnlock(h)
    return {"seq": seq, "dados": dados, "ignorados": ignorados}


def _por_bloco(dados: bytes):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(dados)))
    if not h:
        return None
    p = kernel32.GlobalLock(h)
    if not p:
        return None
    try:
        if dados:
            ctypes.memmove(p, dados, len(dados))
    finally:
        kernel32.GlobalUnlock(h)
    return h


def restaurar(foto: dict) -> bool:
    """Devolve a foto ao clipboard. True se TODOS os formatos da foto voltaram.

    Nunca chamado sem foto: `escrever_texto` guarda a foto antes de limpar.
    """
    dados = (foto or {}).get("dados") or {}
    if not dados:
        return False
    ok = True
    with _Aberto():
        if not user32.EmptyClipboard():
            return False
        for fmt, b in dados.items():
            h = _por_bloco(b)
            if h is None or not user32.SetClipboardData(fmt, h):
                ok = False
    return ok


def ler_texto():
    """Texto unicode do clipboard, ou None (clipboard sem texto / indisponivel)."""
    try:
        with _Aberto():
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            p = kernel32.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
    except ClipboardIndisponivel:
        return None


def escrever_texto(texto: str) -> bool:
    """Escreve texto e CONFERE relendo.

    O retorno de SetClipboardData nao basta: com um gerenciador de clipboard
    ativo, a escrita pode "dar certo" e o conteudo ser outro. Quem chama
    PRECISA saber se de fato ficou la, porque o proximo passo e' mandar Ctrl+V.
    """
    try:
        with _Aberto():
            if not user32.EmptyClipboard():
                return False
            bruto = (texto + "\0").encode("utf-16-le")
            h = _por_bloco(bruto)
            if h is None:
                return False
            if not user32.SetClipboardData(CF_UNICODETEXT, h):
                return False
    except ClipboardIndisponivel:
        return False
    return ler_texto() == texto


def disponivel() -> tuple[bool, str]:
    """Da pra abrir o clipboard AGORA? (True, "") ou (False, motivo).

    Existe um caso em que NENHUM processo da sessao consegue: com a estacao de
    trabalho BLOQUEADA (Win+L), a area de trabalho ativa vira a do Winlogon e
    `OpenClipboard` devolve ERROR_ACCESS_DENIED (5) para todo mundo em
    `WinSta0\\Default` — nao e' bug do app nem gerenciador de clipboard.
    Quem chama usa isto para PULAR o teste com motivo, em vez de acusar falha
    que nao existe ("nao sei" != "quebrado").
    """
    try:
        with _Aberto(tentativas=3, espera=0.05):
            return True, ""
    except ClipboardIndisponivel:
        err = ctypes.get_last_error()
        if err == 5:
            return False, ("OpenClipboard negado (erro 5). A sessao do Windows "
                           "provavelmente esta BLOQUEADA: com a tela travada "
                           "nenhum processo da sessao abre o clipboard.")
        return False, f"OpenClipboard falhou (erro {err})"


def tem_exatamente(texto: str) -> bool:
    """O clipboard ainda e' EXATAMENTE este texto? (guarda do formatado tardio:
    so troca se ninguem mexeu desde a nossa escrita)."""
    return ler_texto() == texto
