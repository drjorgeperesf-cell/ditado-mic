# -*- coding: utf-8 -*-
"""Sobe o whisper-server com os MESMOS flags do Mac.

    whisper-server -m <modelo q8> --host 127.0.0.1 --port 7777 \
        -l pt -t 4 -mc 224 -bo 5 -bs 5 -sns

`-l pt` (portugues), `-t 4` (threads), `-bo 5` (best-of), `-bs 5` (beam size),
`-sns` (suppress non-speech), `-mc 224` (sobreposto pelo `max_context` de cada
request).

**`--vad` esta BANIDO** (veto 4): vazava estado entre requests
(req01 = 0,4 s -> req20 = 7,5 s) e ficava surdo (audio de 13 s -> so 2,1 s de
fala detectada = ditado inteiro perdido). Foi removido do plist, do template,
do INSTALAR e do rollback no Mac justamente PRA NAO VOLTAR NUMA REINSTALACAO.
Nao acrescente aqui.

Beam 5 e' medido, nao gosto: audio real de 114 s -> beam5 = 6-8 s E melhor
texto; greedy = 8,4 s e pior (cai em temperature-fallback e fica mais LENTO).

NOTA DESTA MAQUINA: a PRIMEIRA execucao apos instalar/atualizar driver NVIDIA
demora ~25 s (JIT dos kernels CUDA); depois fica em cache. O keep-warm cobre.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "recursos"))

import config          # noqa: E402
from log import dir_logs, log   # noqa: E402
from proc import CREATE_NO_WINDOW  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
SERVIDOR = RAIZ / "whisper" / "Release" / "whisper-server.exe"
MODELO = RAIZ / "modelos" / "ggml-small-q5_1.bin"


def comando():
    return [
        str(SERVIDOR),
        "-m", str(MODELO),
        "--host", str(config.get("server_host", "127.0.0.1")),
        "--port", str(config.get("server_port", 7777)),
        "-l", str(config.get("language", "pt")),
        "-t", str(max(2, (os.cpu_count() or 4) // 2)),
        "-mc", "224",
        "-bo", "5",
        "-bs", "5",
        "-sns",
    ]


def main():
    if not SERVIDOR.exists():
        log(f"servidor: binario ausente ({SERVIDOR})")
        return 2
    if not MODELO.exists():
        log(f"servidor: modelo ausente ({MODELO})")
        return 3
    import transcricao
    if transcricao.servidor_vivo(timeout=2.0):
        log("servidor: ja esta de pe — nada a fazer")
        return 0
    saida = dir_logs() / "whisper-server.log"
    log("servidor: subindo")
    with open(saida, "a", encoding="utf-8", errors="replace") as f:
        subprocess.Popen(comando(), stdout=f, stderr=subprocess.STDOUT,
                         creationflags=CREATE_NO_WINDOW,
                         cwd=str(SERVIDOR.parent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
