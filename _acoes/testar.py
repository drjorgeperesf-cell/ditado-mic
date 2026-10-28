# -*- coding: utf-8 -*-
"""Teste rapido da instalacao. Sai 0 = OK. Nao grava, nao mexe no teclado."""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))


def main() -> int:
    falhas = []

    def conferir(nome, ok, detalhe=""):
        print(f"  [{'ok' if ok else 'FALHOU'}] {nome}" + (f" — {detalhe}" if detalhe and not ok else ""))
        if not ok:
            falhas.append(nome)

    print()
    for arq in sorted(RAIZ.glob("recursos/*.py")) + sorted(RAIZ.glob("_acoes/*.py")):
        try:
            py_compile.compile(str(arq), doraise=True)
        except py_compile.PyCompileError as e:
            conferir(f"sintaxe {arq.name}", False, str(e)[:120])
    conferir("todos os arquivos .py compilam", not falhas)

    import config
    conferir("tecla de ditar = Alt direito", config.get("hotkey_ditar_vk") == 0xA5)
    conferir("tecla de formatar = Ctrl direito", config.get("hotkey_formatar_vk") == 0xA3)
    conferir("servidor so em 127.0.0.1", config.get("server_host") == "127.0.0.1")

    import texto
    conferir("limpeza de texto roda", isinstance(texto.__name__, str))

    conferir("whisper-server instalado", (RAIZ / "whisper" / "Release" / "whisper-server.exe").exists(),
             "rode o INSTALAR.bat")
    conferir("modelo instalado", (RAIZ / "modelos" / "ggml-small-q5_1.bin").exists(),
             "rode o INSTALAR.bat")

    import transcricao
    conferir("motor de voz responde", transcricao.servidor_vivo(timeout=3.0),
             "abra o ABRIR-MICROFONE.bat e espere 20 s")

    print()
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
