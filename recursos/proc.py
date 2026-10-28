# -*- coding: utf-8 -*-
"""Processos no Windows — sem `os.kill`.

ARMADILHA QUE MATA PROCESSO (regra de ouro 7 do PROMPT-INSTALACAO):
`os.kill(pid, 0)` no POSIX so pergunta "esse PID existe?". No Windows o CPython
traduz QUALQUER sinal que nao seja CTRL_C/CTRL_BREAK em `TerminateProcess` — a
checagem de trava MATA o dono da trava, em silencio e com codigo 0.
Aqui a checagem e `pid_vivo()`, feita com OpenProcess +
PROCESS_QUERY_LIMITED_INFORMATION + GetExitCodeProcess == STILL_ACTIVE.
`testes/teste_proc.py` prova que `pid_vivo(os.getpid())` nao mata o processo.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259

_k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_k32.GetExitCodeProcess.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.CloseHandle.restype = wintypes.BOOL
_k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
_k32.TerminateProcess.restype = wintypes.BOOL
_k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
_k32.CreateMutexW.restype = wintypes.HANDLE

CREATE_NO_WINDOW = 0x08000000


class InstanciaUnica:
    """Mantem um mutex nomeado aberto; o Windows o libera mesmo apos crash."""

    def __init__(self, nome):
        self.nome = nome
        self.handle = None

    def __enter__(self):
        ctypes.set_last_error(0)
        self.handle = _k32.CreateMutexW(None, False, self.nome)
        erro = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(erro)
        return erro != 183  # ERROR_ALREADY_EXISTS: outro app mantem o mutex

    def __exit__(self, *args):
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


def pid_vivo(pid) -> bool:
    """O processo `pid` existe e ainda esta rodando? NAO o mata, nunca."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def matar(pid) -> bool:
    """Encerra de verdade (so onde a INTENCAO e matar)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    h = _k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(_k32.TerminateProcess(h, 1))
    finally:
        _k32.CloseHandle(h)


def popen_kwargs(**extra):
    """kwargs padrao de subprocess: sem janela preta, utf-8, stderr fora do pipe.

    stderr em PIPE que ninguem le ENCHE (~64 KB) e TRAVA o filho (licao 6.12).
    Por isso o default e' `stderr=DEVNULL`.

    ARMADILHA MEDIDA AQUI (20/09/2026): como isto define `stderr`, quem chamar
    `subprocess.run(..., capture_output=True, **popen_kwargs())` recebe
    `ValueError: stdout and stderr arguments may not be used with
    capture_output` — e, dentro de um `except Exception`, isso vira falha
    MUDA. Foi assim que o fallback do whisper-cli e o corretor "nao
    funcionavam" sem dizer por que. **Use `rodar()` abaixo**, ou peca
    `stdout=subprocess.PIPE` explicitamente; nunca `capture_output=True`.
    """
    kw = dict(
        creationflags=CREATE_NO_WINDOW,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    kw.update(extra)
    return kw


def rodar(args, timeout=30, entrada=None, juntar_erro=False, **extra):
    """`subprocess.run` do jeito certo: stdout no pipe, stderr fora dele.

    `juntar_erro=True` manda o stderr para o MESMO pipe do stdout (util para
    ler mensagem de erro de ferramenta que escreve no stderr, como o
    `schtasks` e o `--help` do whisper-cli).
    """
    kw = popen_kwargs(
        stderr=(subprocess.STDOUT if juntar_erro else subprocess.DEVNULL),
        **extra)
    if entrada is not None:
        kw["input"] = entrada
    return subprocess.run(args, stdout=subprocess.PIPE, timeout=timeout, **kw)


def integridade_do_processo(pid) -> int | None:
    """Nivel de integridade do token do processo (SECURITY_MANDATORY_*_RID).

    0x2000 = Medium (processo normal), 0x3000 = High (elevado/admin).
    `SendInput` de um processo Medium NAO alcanca janela de processo High
    (UIPI) — quem cola usa isto pra AVISAR em vez de falhar calado.
    Devolve None quando nao da pra saber ("nao sei" != "quebrado", licao 6.11).
    """
    PROCESS_QUERY_INFORMATION = 0x0400
    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid))
    if not h:
        return None
    tok = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        need = wintypes.DWORD()
        adv.GetTokenInformation(tok, TokenIntegrityLevel, None, 0, ctypes.byref(need))
        if not need.value:
            return None
        buf = ctypes.create_string_buffer(need.value)
        if not adv.GetTokenInformation(tok, TokenIntegrityLevel, buf,
                                       need.value, ctypes.byref(need)):
            return None

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TOKEN_MANDATORY_LABEL(ctypes.Structure):
            _fields_ = [("Label", SID_AND_ATTRIBUTES)]

        tml = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        n = adv.GetSidSubAuthorityCount(tml.Label.Sid).contents.value
        return int(adv.GetSidSubAuthority(tml.Label.Sid, n - 1).contents.value)
    except Exception:
        return None
    finally:
        try:
            if tok:
                _k32.CloseHandle(tok)
        except Exception:
            pass
        _k32.CloseHandle(h)


def eu_sou_elevado() -> bool:
    lvl = integridade_do_processo(os.getpid())
    return bool(lvl is not None and lvl >= 0x3000)


# ── "Sair (descarregar)" na bandeja: o Guardiao nao pode ressuscitar o app ──
def _marca_saida():
    from log import base_dados
    return base_dados() / "saida-intencional.flag"


def marcar_saida_intencional():
    try:
        _marca_saida().write_text("1", encoding="utf-8")
    except OSError:
        pass


def saida_intencional() -> bool:
    try:
        return _marca_saida().exists()
    except OSError:
        return False


def limpar_saida_intencional():
    try:
        _marca_saida().unlink()
    except OSError:
        pass
