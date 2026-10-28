# -*- coding: utf-8 -*-
"""INSTALAR — baixa o motor de voz, o modelo, cria as tarefas e sobe tudo.

Roda DENTRO da .venv (o INSTALAR.bat cria a venv e instala os pacotes antes).

Passos:
  1. baixa o whisper.cpp (versao fixa, so CPU) para `whisper\\`;
  2. baixa o modelo `ggml-small-q5_1` (190 MB) para `modelos\\`;
  3. grava a tecla de ditar (Alt direito) e a de formatar (Ctrl direito);
  4. opcional: cria as tarefas do Agendador (iniciar com o Windows);
  5. sobe o servidor e o app e confere.

Nada e' apagado. Rodar de novo e' seguro: o que ja foi baixado e conferido
nao e' baixado outra vez.

Opcoes:  --sim   responde SIM a pergunta de iniciar com o Windows
         --nao   responde NAO (so baixa e deixa pronto)
"""
from __future__ import annotations

import hashlib
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))
sys.path.insert(0, str(RAIZ / "_acoes"))

import config      # noqa: E402
import tarefas     # noqa: E402
from _acoes_util import perguntar  # noqa: E402

WHISPER_URL = ("https://github.com/ggml-org/whisper.cpp/releases/download/"
               "b5454/whisper-bin-x64.zip")
WHISPER_SHA256 = "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb"
MODELO_URL = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
              "ggml-small-q5_1.bin")
MODELO_TAMANHO = 190085487
MODELO_SHA256 = "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb"

PASTA_WHISPER = RAIZ / "whisper"
PASTA_MODELOS = RAIZ / "modelos"
SERVIDOR = PASTA_WHISPER / "Release" / "whisper-server.exe"
MODELO = PASTA_MODELOS / "ggml-small-q5_1.bin"


def sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def baixar(url: str, destino: Path, esperado: str, rotulo: str) -> bool:
    """Baixa para `.part`, confere o SHA-256 e so entao troca o nome."""
    if destino.exists() and sha256(destino) == esperado:
        print(f"  [ok]   {rotulo}: ja estava baixado e conferido")
        return True
    destino.parent.mkdir(parents=True, exist_ok=True)
    parte = destino.with_suffix(destino.suffix + ".part")
    print(f"  baixando {rotulo} ...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ditado-mic"})
        with urllib.request.urlopen(req, timeout=60) as r, open(parte, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            feito = 0
            ultimo = 0.0
            while True:
                bloco = r.read(1 << 20)
                if not bloco:
                    break
                f.write(bloco)
                feito += len(bloco)
                if total and time.time() - ultimo > 2:
                    ultimo = time.time()
                    print(f"         {feito * 100 // total}% "
                          f"({feito // (1 << 20)} de {total // (1 << 20)} MB)")
    except Exception as e:
        print(f"  [FALHOU] {rotulo}: {type(e).__name__}: {e}")
        return False
    if sha256(parte) != esperado:
        print(f"  [FALHOU] {rotulo}: o arquivo baixado nao confere (SHA-256).")
        print(f"           Apague {parte} e rode o INSTALAR de novo.")
        return False
    parte.replace(destino)
    print(f"  [ok]   {rotulo}")
    return True


def passo_whisper() -> bool:
    if SERVIDOR.exists():
        print("  [ok]   whisper.cpp: ja instalado")
        return True
    zip_ = RAIZ / "whisper-bin-x64.zip"
    if not baixar(WHISPER_URL, zip_, WHISPER_SHA256, "whisper.cpp (9 MB)"):
        return False
    PASTA_WHISPER.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_) as z:
        for nome in z.namelist():
            base = nome.rsplit("/", 1)[-1]
            # so o que o ditado usa: servidor, cli e as bibliotecas
            if base in ("whisper-server.exe", "whisper-cli.exe") or base.endswith(".dll"):
                destino = PASTA_WHISPER / nome
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(z.read(nome))
    zip_.unlink()
    return SERVIDOR.exists()


def passo_modelo() -> bool:
    return baixar(MODELO_URL, MODELO, MODELO_SHA256, "modelo de voz (190 MB)")


def passo_teclas():
    import seletor_tecla as st
    st.gravar(st.escolha_alt_direito())
    print(f"  [ok]   tecla de ditar: {config.get('hotkey_ditar_nome')}; "
          f"formatar: {config.get('hotkey_formatar_nome')}")


def main() -> int:
    print()
    print("  DITADO MIC — instalacao")
    print("  " + "=" * 56)
    if not passo_whisper():
        return 2
    if not passo_modelo():
        return 3
    passo_teclas()

    if "--sim" in sys.argv:
        resp = "S"
    elif "--nao" in sys.argv:
        resp = "N"
    else:
        print()
        resp = perguntar("  Iniciar o Ditado Mic junto com o Windows? (S/N): ").strip().upper()
    import transcricao
    if resp.startswith("S"):
        print("  tarefas do Agendador:")
        for n, ok, detalhe in tarefas.criar_todas():
            print(f"        {'[ok]    ' if ok else '[FALHOU]'} {n}" +
                  ("" if ok else f" — {detalhe}"))
        tarefas.rodar_todas()
    else:
        import subprocess
        from proc import CREATE_NO_WINDOW
        pyw = RAIZ / ".venv" / "Scripts" / "pythonw.exe"
        subprocess.Popen([str(pyw), str(RAIZ / "_acoes" / "abrir_microfone.py")],
                         creationflags=CREATE_NO_WINDOW, cwd=str(RAIZ))

    print("  subindo o motor de voz (pode levar uns 20 segundos) ...")
    limite = time.time() + 90
    no_ar = False
    while time.time() < limite:
        if transcricao.servidor_vivo(timeout=2.0):
            no_ar = True
            break
        time.sleep(1.5)
    print(f"  motor de voz: {'no ar' if no_ar else 'NAO respondeu em 90 s (veja logs)'}")
    print()
    print("  COMO USAR")
    print("    1. Clique onde quer escrever (qualquer programa).")
    print("    2. Aperte ALT DIREITO, fale, aperte ALT DIREITO de novo.")
    print("    3. O texto aparece no cursor.")
    print("  Outra tecla: CONFIGURAR-TECLA.bat. Outro microfone: CONFIGURAR-MIC.bat.")
    return 0 if no_ar else 4


if __name__ == "__main__":
    sys.exit(main())
