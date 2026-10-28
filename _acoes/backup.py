# -*- coding: utf-8 -*-
"""BACKUP — o unico jeito de nao perder o historico.

Espelha o BACKUP.command do Mac no que importa:
 - **snapshot SQLite ONLINE** (`.backup` + `integrity_check`), nunca `copy` do
   banco vivo (copiar arquivo aberto pode gerar banco corrompido);
 - escreve num `.part` e so renomeia depois de conferir o zip;
 - MANIFESTO.txt com versoes, sha256 e tamanhos;
 - rotacao mantendo 10 backups, e a ROTACAO pede `SIM` maiusculo antes de
   apagar qualquer coisa (qualquer outra resposta preserva TUDO).
`input()` com stdin fechado conta como CANCELAR.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))
sys.path.insert(0, str(RAIZ / "_acoes"))

import config      # noqa: E402
from log import base_dados   # noqa: E402
from _acoes_util import perguntar  # noqa: E402

DESTINO = Path.home() / "Backups" / "Microfone"
MAX_BACKUPS = 10


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def snapshot_banco(destino: Path) -> tuple[bool, str]:
    """Snapshot ONLINE + integrity_check. Nunca copia o arquivo vivo."""
    origem = config.caminho_banco()
    if not origem.exists():
        return False, "nao existe"
    try:
        src = sqlite3.connect(f"file:{origem.as_posix()}?mode=ro", uri=True,
                              timeout=10)
        dst = sqlite3.connect(str(destino))
        with dst:
            src.backup(dst)
        r = dst.execute("PRAGMA integrity_check").fetchone()
        src.close()
        dst.close()
        if r and r[0] == "ok":
            return True, "ok"
        return False, f"integrity_check: {r}"
    except sqlite3.Error as e:
        return False, type(e).__name__


def main():
    print()
    print("  BACKUP do Microfone")
    print("  Guarda: historico (banco + arquivos por dia) e config.")
    print(f"  Destino: {DESTINO}")
    print()
    if perguntar("  ENTER para continuar (qualquer outra coisa cancela): ").strip():
        print("  Cancelado.")
        return 1

    DESTINO.mkdir(parents=True, exist_ok=True)
    carimbo = time.strftime("%Y%m%d-%H%M%S")
    stage = DESTINO / f".stage-{carimbo}"
    stage.mkdir()
    itens = []
    try:
        ok, detalhe = snapshot_banco(stage / "history.db")
        print(f"  banco: {detalhe}")
        if ok:
            itens.append("history.db")
        hist = base_dados() / "history"
        if hist.exists():
            shutil.copytree(hist, stage / "history")
            itens.append("history/")
        if config.caminho_config().exists():
            shutil.copy2(config.caminho_config(), stage / "config.json")
            itens.append("config.json")
        mv = config.dir_config() / "medical_vocab.json"
        if mv.exists():
            shutil.copy2(mv, stage / "medical_vocab.json")
            itens.append("medical_vocab.json")

        manifesto = {
            "carimbo": carimbo,
            "maquina": __import__("socket").gethostname(),
            "python": sys.version,
            "itens": itens,
            "sha256": {p.name: sha256(p) for p in stage.iterdir() if p.is_file()},
        }
        (stage / "MANIFESTO.json").write_text(
            json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

        parte = DESTINO / f"microfone-{carimbo}.zip.part"
        with zipfile.ZipFile(parte, "w", zipfile.ZIP_DEFLATED) as z:
            for p in stage.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(stage).as_posix())
        # so renomeia depois de CONFERIR o zip
        with zipfile.ZipFile(parte) as z:
            ruim = z.testzip()
        if ruim:
            print(f"  ABORTADO: zip corrompido em {ruim}")
            parte.unlink(missing_ok=True)
            return 2
        final = parte.with_suffix("")
        parte.replace(final)
        print(f"  backup pronto: {final} ({final.stat().st_size // 1024} KB)")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # rotacao — pede SIM maiusculo
    zips = sorted(DESTINO.glob("microfone-*.zip"))
    if len(zips) > MAX_BACKUPS:
        velhos = zips[:len(zips) - MAX_BACKUPS]
        print()
        print(f"  Ha {len(zips)} backups (o limite e {MAX_BACKUPS}).")
        for v in velhos:
            print(f"    - {v.name}")
        r = perguntar(f"  Digite SIM para apagar esses {len(velhos)}: ")
        if r == "SIM":
            for v in velhos:
                v.unlink(missing_ok=True)
            print("  apagados.")
        else:
            print("  Nada foi apagado — todos os backups continuam ai.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
