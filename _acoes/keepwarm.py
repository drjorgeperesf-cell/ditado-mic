# -*- coding: utf-8 -*-
"""Keep-warm: ping de silencio no whisper-server.

MOTIVO MEDIDO (no Mac): servidor ocioso ESFRIA. 3,5 min parado = 1,9 s na 1a
ditada; ~10 min parado = **8,1 s**. Com o ping, a 1a ditada fica ~1 s SEMPRE.

Nesta maquina ha um custo a mais na PRIMEIRA execucao depois de instalar ou
atualizar o driver NVIDIA: ~25 s de JIT dos kernels CUDA. Depois fica em cache.
O keep-warm e' justamente o que faz esse custo nunca cair em cima de um ditado.

TODA ITERACAO LOGA UMA LINHA COM O MOTIVO — guarda de script NUNCA da `exit 0`
silencioso (senao o guardiao nao consegue distinguir "pulei" de "nao rodei").

O GET de liveness roda ANTES do check do WAV: um `silencio_1s.wav` sumido nao
pode cegar a auto-cura.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))

import transcricao     # noqa: E402
from log import dir_logs, log  # noqa: E402

WAV = RAIZ / "testes" / "fixtures" / "silencio_1s.wav"
ALERTA_S = 30.0


def registrar(msg):
    """Linha do keep-warm no log proprio. CONTRATO FRAGIL: o guardiao parseia
    o TEXTO destas linhas ("ping ok", "ALERTA: timeout no POST",
    "PULADO: ...", "server fora (GET sem resposta"). Mudar aqui exige mudar o
    guardiao junto."""
    p = dir_logs() / "keepwarm.log"
    try:
        if p.exists() and p.stat().st_size > 100_000:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def main():
    if not transcricao.servidor_vivo(timeout=3.0):
        registrar("server fora (GET sem resposta)")
        return 1
    if not WAV.exists():
        registrar("PULADO: silencio_1s.wav ausente (server respondeu ao GET)")
        return 0
    t0 = time.monotonic()
    texto, erro, dt = transcricao.via_servidor(WAV, 1.0, "")
    if texto is None:
        registrar(f"ALERTA: timeout no POST ({erro}, {dt:.1f}s)")
        return 2
    if dt > ALERTA_S:
        registrar(f"ALERTA: server degradado (POST levou {dt:.1f}s)")
        return 0
    registrar(f"ping ok ({dt:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
