# -*- coding: utf-8 -*-
"""Utilidades compartilhadas pelos .bat (INSTALAR, DESINSTALAR, BACKUP...)."""
from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def pasta_inicializar() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
            "Start Menu" / "Programs" / "Startup")


def perguntar(texto: str) -> str:
    """input() que trata stdin fechado (sem console) como CANCELAR.

    Confirmacao destrutiva nunca pode virar "sim" por acidente de ambiente.
    """
    try:
        return input(texto)
    except (EOFError, KeyboardInterrupt):
        print("\n  (sem console interativo — tratando como CANCELAR)")
        return ""
