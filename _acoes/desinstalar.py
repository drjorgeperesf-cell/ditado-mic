# -*- coding: utf-8 -*-
"""DESINSTALAR — remove TUDO o que o INSTALAR criou, e deixa a pasta como estava.

Exige `SIM` maiusculo, comparacao exata. `input()` com stdin fechado levanta
EOFError: isso conta como CANCELAR.

PRESERVA OS DADOS PRIMEIRO: copia `history.db`, `history/` e o `config.json`
para `_backup_desinstalado-<carimbo>` ANTES de qualquer remocao. Se a copia
falhar, ABORTA — nada e' apagado.

REGRA REGISTRADA: arquivo novo que guarda ditado entra no DESINSTALAR na
MESMA mudanca que o cria. Por isso a lista inclui `cli-sessao` (onde o CLI do
Claude grava a sessao em texto claro), `rec/` (WAVs) e os logs.

NAO apaga: o modelo do whisper, o binario do whisper.cpp, os backups.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))

import config          # noqa: E402
import tarefas         # noqa: E402
from log import base_dados  # noqa: E402
from _acoes_util import perguntar  # noqa: E402

A_REMOVER = ("cli-sessao", "rec", "logs", "history", "guardiao-estado.json",
             "pos.txt", "history.db", "history.db-wal", "history.db-shm")


def main():
    print()
    print("  DESINSTALAR o Ditado Mic")
    print("  " + "-" * 56)
    print("  Vai:")
    print("   - apagar as 4 tarefas DitadoMic-*")
    print("   - encerrar o app")
    print("   - apagar os dados do app (historico, WAVs, logs, sessao do CLI)")
    print()
    print("  NAO vai tocar: o modelo do whisper,")
    print("  o whisper.cpp, nem os backups que voce ja tiver feito.")
    print()
    print("  Antes de apagar, tudo e COPIADO para uma pasta de backup aqui.")
    print()
    r = perguntar("  Digite SIM (maiusculo) para desinstalar: ")
    if r != "SIM":
        print("  Cancelado. Nada foi mudado.")
        return 1

    # 1) PRESERVAR OS DADOS PRIMEIRO — se falhar, aborta antes de remover
    carimbo = time.strftime("%Y%m%d-%H%M%S")
    backup = RAIZ / f"_backup_desinstalado-{carimbo}"
    try:
        backup.mkdir(parents=True, exist_ok=False)
        base = base_dados()
        if (base / "history.db").exists():
            shutil.copy2(base / "history.db", backup / "history.db")
        if (base / "history").exists():
            shutil.copytree(base / "history", backup / "history")
        if config.caminho_config().exists():
            shutil.copy2(config.caminho_config(), backup / "config.json")
        print(f"  dados copiados para: {backup}")
    except OSError as e:
        print(f"  ABORTADO: nao consegui preservar os dados ({type(e).__name__}).")
        print("  NADA foi apagado.")
        return 2

    # 2) tarefas
    for nome, ok, detalhe in tarefas.apagar_todas():
        print(f"  {'[ok]  ' if ok else '[aviso]'} tarefa {nome}")

    # 3) dados do app
    base = base_dados()
    for item in A_REMOVER:
        p = base / item
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except OSError:
            pass
    print(f"  dados do app removidos de: {base}")

    print()
    print("  PRONTO. A pasta desta instalacao continua aqui — voce pode apaga-la")
    print("  a mao quando quiser (o backup acima esta dentro dela).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
