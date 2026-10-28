# -*- coding: utf-8 -*-
"""Abre o app e seu motor, sem console e sem duplicar o gancho."""
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))
from proc import CREATE_NO_WINDOW
import transcricao


def main():
    pyw = RAIZ / ".venv" / "Scripts" / "pythonw.exe"
    if not transcricao.servidor_vivo(timeout=2.0):
        subprocess.Popen([str(pyw), str(RAIZ / "_acoes" / "subir_servidor.py")],
                         creationflags=CREATE_NO_WINDOW, cwd=str(RAIZ))
    subprocess.Popen([str(pyw), str(RAIZ / "recursos" / "app.py")],
                     creationflags=CREATE_NO_WINDOW, cwd=str(RAIZ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
