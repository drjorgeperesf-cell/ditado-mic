# -*- coding: utf-8 -*-
"""RESTAURAR — lista os backups, mostra o que tem dentro e NUNCA copia sozinho.

Igual ao Mac: extrai para uma pasta ao lado e mostra a diferenca; quem copia
por cima e' o usuário, com um `SIM` explicito.
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))
sys.path.insert(0, str(RAIZ / "_acoes"))

import config       # noqa: E402
from log import base_dados  # noqa: E402
from _acoes_util import perguntar  # noqa: E402

DESTINO = Path.home() / "Backups" / "Microfone"


def main():
    zips = sorted(DESTINO.glob("microfone-*.zip"), reverse=True)
    print()
    if not zips:
        print(f"  Nenhum backup em {DESTINO}.")
        print("  Rode o BACKUP.bat primeiro.")
        return 1
    print("  BACKUPS DISPONIVEIS (o mais novo primeiro):")
    for i, z in enumerate(zips, 1):
        print(f"   {i:2d}. {z.name}  ({z.stat().st_size // 1024} KB)")
    print()
    r = perguntar("  Numero do backup (q cancela): ").strip()
    if not r or r.lower() == "q":
        print("  Cancelado.")
        return 1
    try:
        escolhido = zips[int(r) - 1]
    except (ValueError, IndexError):
        print("  Numero invalido.")
        return 1

    saida = DESTINO / f"extraido-{escolhido.stem}"
    if saida.exists():
        shutil.rmtree(saida, ignore_errors=True)
    with zipfile.ZipFile(escolhido) as z:
        z.extractall(saida)
    print(f"\n  extraido em: {saida}")
    man = saida / "MANIFESTO.json"
    if man.exists():
        m = json.loads(man.read_text(encoding="utf-8"))
        print(f"  feito em {m.get('carimbo')} na maquina {m.get('maquina')}")
        print(f"  contem: {', '.join(m.get('itens', []))}")

    print()
    print("  COMPARACAO com o que esta valendo agora:")
    pares = [(saida / "history.db", config.caminho_banco()),
             (saida / "config.json", config.caminho_config())]
    for do_backup, atual in pares:
        if not do_backup.exists():
            continue
        a = do_backup.stat().st_size
        b = atual.stat().st_size if atual.exists() else 0
        print(f"   {do_backup.name:<20} backup {a:>9} B   atual {b:>9} B"
              f"   {'IGUAIS' if a == b else 'DIFERENTES'}")
    print()
    print("  NADA foi copiado por cima. Para restaurar de verdade:")
    print("   1. feche o Microfone (bandeja -> Sair)")
    print(f"   2. copie {saida}\\history.db por cima de {config.caminho_banco()}")
    print(f"   3. copie {saida}\\config.json por cima de {config.caminho_config()}")
    print("   4. abra o ABRIR-MICROFONE.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
