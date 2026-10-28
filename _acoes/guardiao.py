# -*- coding: utf-8 -*-
"""Guardiao — a cada 120 s, sobe o que caiu.

Substitui o `KeepAlive` do launchd. Duas regras herdadas do Mac:

- **APP VIVO NUNCA E' TOCADO** — religar derrubaria o gancho de teclado no meio
  de um ditado. Mas com o processo confirmado MORTO (crash 0xC0000374 de
  07/10/2026) ele religa sozinho, em silencio (`schtasks /Run`), com backoff
  0 -> 5 min -> 15 min -> 1 h. "Sair" na bandeja deixa uma marca e nao e'
  ressuscitado.
- **Detector que acusa e nao conserta vira tempestade:** BACKOFF por strikes e
  UM retrato do estado no log, nao uma linha por ciclo repetida.
- **"nao sei" != "quebrado"** (licao 6.11): `schtasks` com rc != 0 devolve
  `desconhecido`, e estado desconhecido NAO dispara acao nenhuma. Foi
  exatamente assim que, no Mac, `launchctl_estado` devolvendo "descarregado"
  para um COMANDO QUE FALHOU virou bootstrap espurio em agente saudavel.

Toda iteracao loga uma linha com o motivo.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))

import tarefas         # noqa: E402
import transcricao     # noqa: E402
from log import base_dados, dir_logs  # noqa: E402
from proc import (limpar_saida_intencional, marcar_saida_intencional,  # noqa: E402,F401
                  pid_vivo, rodar, saida_intencional)

ESTADO = base_dados() / "guardiao-estado.json"
STRIKES_ANTES_DE_AGIR = 2
BACKOFF = (0, 300, 900, 3600)     # s: 0 -> 5 min -> 15 min -> 1 h


def registrar(msg):
    p = dir_logs() / "guardiao.log"
    try:
        if p.exists() and p.stat().st_size > 200_000:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def ler_estado():
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gravar_estado(d):
    try:
        ESTADO.parent.mkdir(parents=True, exist_ok=True)
        ESTADO.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def app_esta_vivo() -> bool | None:
    """True/False/None. None = nao sei (e nao se age sobre 'nao sei')."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or "
          "Name='python.exe'\" | ForEach-Object { $_.CommandLine }")
    try:
        r = rodar(["powershell", "-NoProfile", "-NonInteractive",
                   "-Command", ps], timeout=25)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    alvo = str(RAIZ / "recursos" / "app.py").lower()
    return any(alvo in (l or "").lower() for l in (r.stdout or "").splitlines())


def main():
    est = ler_estado()
    agora = time.time()
    acoes = []

    # ── servidor de transcricao ───────────────────────────────────────
    if transcricao.servidor_vivo(timeout=4.0):
        est["strikes_server"] = 0
    else:
        est["strikes_server"] = int(est.get("strikes_server", 0)) + 1
        n = est["strikes_server"]
        espaco = BACKOFF[min(n - STRIKES_ANTES_DE_AGIR, len(BACKOFF) - 1)] \
            if n >= STRIKES_ANTES_DE_AGIR else None
        ultimo = float(est.get("ultima_acao_server", 0))
        if n >= STRIKES_ANTES_DE_AGIR and agora - ultimo >= (espaco or 0):
            est["ultima_acao_server"] = agora
            rodar(["schtasks", "/Run", "/TN", tarefas.nome("Servidor")], timeout=20)
            acoes.append(f"server religado (strike {n})")
        else:
            acoes.append(f"server fora (strike {n}, aguardando backoff)")

    # ── app do ditado: religado SO quando o processo esta confirmado MORTO ──
    # (app vivo nunca e' tocado: religar vivo derrubaria o gancho no meio do
    # ditado. Morto nao ha gancho a derrubar.) Silencioso: schtasks /Run.
    vivo = app_esta_vivo()
    if vivo is None:
        acoes.append("app: desconhecido (sonda falhou) — nenhuma acao")
    elif vivo:
        est["strikes_app"] = 0
        est["religacoes_app"] = 0
    elif saida_intencional():
        acoes.append("app fechado pelo usuário (Sair na bandeja) — nao religo")
    else:
        est["strikes_app"] = int(est.get("strikes_app", 0)) + 1
        n = int(est.get("religacoes_app", 0))
        espaco = BACKOFF[min(n, len(BACKOFF) - 1)]
        if agora - float(est.get("ultima_acao_app", 0)) >= espaco:
            est["ultima_acao_app"] = agora
            est["religacoes_app"] = n + 1
            rodar(["schtasks", "/Run", "/TN", tarefas.nome("App")], timeout=20)
            acoes.append(f"app caiu — religado (tentativa {n + 1})")
        else:
            acoes.append(f"app fora (strike {est['strikes_app']}, aguardando backoff)")

    gravar_estado(est)
    registrar("; ".join(acoes) if acoes else "tudo ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
