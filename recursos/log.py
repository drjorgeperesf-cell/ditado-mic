# -*- coding: utf-8 -*-
"""Log do Microfone.

REGRA INEGOCIAVEL (inventario secao 5, "Preferencias FIRMES"):
**texto de ditado NUNCA entra no log** — so tamanhos, latencias e estados.
`testes/teste_privacidade_log.py` dita uma frase-sentinela e faz grep no log.

Rotacao simples em LOG_MAX_B (espelha app.py:99 / app.py:1161).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

LOG_MAX_B = 1_000_000

_LOCK = threading.Lock()
_N = 0


def base_dados() -> Path:
    """%LOCALAPPDATA%\\BotaoWispr — o 'Application Support' daqui."""
    raiz = os.environ.get("MICROFONE_DADOS")
    if raiz:
        return Path(raiz)
    la = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(la) / "BotaoWispr"


def dir_logs() -> Path:
    d = base_dados() / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def caminho() -> Path:
    return dir_logs() / "ditado-mic.log"


def _rotacionar(p: Path):
    try:
        if p.exists() and p.stat().st_size > LOG_MAX_B:
            velho = p.with_suffix(p.suffix + ".1")
            try:
                if velho.exists():
                    velho.unlink()
            except OSError:
                pass
            p.replace(velho)
    except OSError:
        pass


def log(msg: str):
    """Uma linha no log. NUNCA passe texto de ditado aqui."""
    global _N
    linha = time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg)
    with _LOCK:
        _N += 1
        p = caminho()
        if _N % 500 == 1:
            _rotacionar(p)
        try:
            with open(p, "a", encoding="utf-8", errors="replace") as f:
                f.write(linha + "\n")
        except OSError:
            pass
    if os.environ.get("MICROFONE_LOG_STDERR"):
        try:
            sys.stderr.write(linha + "\n")
        except Exception:
            pass
