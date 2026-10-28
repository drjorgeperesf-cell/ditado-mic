# -*- coding: utf-8 -*-
"""Sobe o painel web em 127.0.0.1:7910 e abre a janela.

Passo a passo (o mesmo desenho do Mac, adaptado):
 1. le/cria o `.mestre` (`--garantir-mestre`);
 2. sobe `painel/painel.py` com o segredo em **variavel de ambiente**
    `PAINEL_MESTRE` — NUNCA por argv (argv e' legivel por qualquer processo);
 3. espera a porta responder de verdade (ate 30 s);
 4. pede o ticket one-shot (vale 90 s) mandando `X-Painel-Mestre`;
 5. abre `http://127.0.0.1:7910/?t=<ticket>` no Chrome em modo app.

SEM COOKIE, DE PROPOSITO: cookie nao e' escopado por PORTA (RFC 6265) — um
cookie de `127.0.0.1:7910` iria para QUALQUER outro servidor local e vazaria
o acesso aos ditados. O ticket vai na URL (uso unico, 90 s) e o token de
sessao vive em `sessionStorage`, que E' escopado por esquema+host+porta.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))

import config          # noqa: E402
from log import log    # noqa: E402
from proc import CREATE_NO_WINDOW, rodar  # noqa: E402

PY = RAIZ / ".venv" / "Scripts" / "python.exe"
PAINEL = RAIZ / "painel" / "painel.py"
CHROMES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


def porta() -> int:
    return int(config.get("painel_porta", 7910))


def mestre() -> str:
    r = rodar([str(PY), str(PAINEL), "--garantir-mestre"], timeout=30)
    return (r.stdout or "").strip()


def responde(p, timeout=1.0) -> bool:
    try:
        c = http.client.HTTPConnection("127.0.0.1", p, timeout=timeout)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{p}"})
        c.getresponse().read(1)
        c.close()
        return True
    except Exception:
        return False


def pedir_ticket(p, segredo):
    c = http.client.HTTPConnection("127.0.0.1", p, timeout=8)
    c.request("POST", "/api/sessao/ticket", body="{}",
              headers={"Host": f"127.0.0.1:{p}",
                       "X-Painel-Mestre": segredo,
                       "Content-Type": "application/json"})
    r = c.getresponse()
    dados = r.read()
    c.close()
    if r.status != 200:
        return None
    try:
        return json.loads(dados).get("t")
    except ValueError:
        return None


def abrir(url):
    for ch in CHROMES:
        if ch.exists():
            subprocess.Popen([str(ch), f"--app={url}"],
                             creationflags=CREATE_NO_WINDOW)
            return True
    os.startfile(url)          # nosec — acao PEDIDA pelo usuário, nao automatica
    return True


def main():
    p = porta()
    segredo = mestre()
    if not segredo:
        print("  ERRO: nao consegui ler/criar o segredo do painel.")
        return 2
    if not responde(p):
        print(f"  subindo o painel em 127.0.0.1:{p}...")
        env = dict(os.environ)
        env["PAINEL_MESTRE"] = segredo       # NUNCA por argv
        env["PYTHONUTF8"] = "1"
        subprocess.Popen([str(PY), str(PAINEL), "--porta", str(p),
                          "--sem-navegador"], env=env,
                         creationflags=CREATE_NO_WINDOW, cwd=str(RAIZ / "painel"))
        limite = time.time() + 30
        while time.time() < limite and not responde(p):
            time.sleep(0.3)
        if not responde(p):
            print(f"  ERRO: o painel nao respondeu em 30 s na porta {p}.")
            return 3
    t = pedir_ticket(p, segredo)
    if not t:
        print("  ERRO: o painel recusou o segredo (403).")
        print(f"  Apague {RAIZ / 'painel' / '.mestre'} e tente de novo.")
        return 4
    url = f"http://127.0.0.1:{p}/?t={t}"
    abrir(url)
    log(f"painel: aberto em 127.0.0.1:{p}")
    print(f"  painel aberto: http://127.0.0.1:{p}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
