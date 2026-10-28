# -*- coding: utf-8 -*-
"""STATUS — o que esta de pe, em 15 linhas, sem jargao.

"nao sei" NAO e' "quebrado": sonda que falhou aparece como `? nao sei`, nunca
como erro. Foi exatamente assim que, no Mac, um comando que FALHAVA virou
"descarregado" e disparou religamento espurio em agente saudavel.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))

import config       # noqa: E402
import tarefas      # noqa: E402
import transcricao  # noqa: E402
from log import base_dados, caminho, dir_logs  # noqa: E402

MARCA = {"ok": "[ok]   ", "ruim": "[FORA] ", "nao-sei": "[?]    "}


def linha(rotulo, estado, detalhe=""):
    print(f"  {MARCA.get(estado, '[?]    ')}{rotulo:<40}{detalhe}")


def idade(p: Path):
    if not p.exists():
        return "nunca escrito"
    d = time.time() - p.stat().st_mtime
    if d < 90:
        return f"ha {int(d)} s"
    if d < 5400:
        return f"ha {int(d / 60)} min"
    return f"ha {int(d / 3600)} h"


def main():
    print()
    print("  MICROFONE (OFICIAL) — situacao agora")
    print("  " + "-" * 56)

    # motor
    t0 = time.monotonic()
    vivo = transcricao.servidor_vivo(timeout=4.0)
    linha("Motor de transcricao (7777)", "ok" if vivo else "ruim",
          f"{(time.monotonic() - t0) * 1000:.0f} ms" if vivo else
          "nao respondeu — rode o REINICIAR.bat")

    # tarefas
    for chave in ("App", "Servidor", "Guardiao", "KeepWarm"):
        e = tarefas.estado(chave)
        est = {"rodando": "ok", "pronta": "ok", "ausente": "ruim",
               "desabilitada": "ruim"}.get(e, "nao-sei")
        detalhe = e if e != "ausente" else "nao instalada — rode o INSTALAR.bat"
        linha(f"Tarefa {chave}", est, detalhe)

    # teclas
    from guardiao import app_esta_vivo
    app_vivo = app_esta_vivo()
    linha("App que recebe o Alt direito",
          "ok" if app_vivo is True else "ruim" if app_vivo is False else "nao-sei",
          "ativo" if app_vivo is True else "desligado" if app_vivo is False else "sonda falhou")
    nome = config.get("hotkey_ditar_nome", "")
    linha("Tecla de DITAR", "ok" if nome else "ruim",
          nome or "nenhuma — rode o CONFIGURAR-TECLA.bat")
    linha("Tecla de FORMATAR/CORRIGIR", "ok", "Ctrl direito")

    # microfone
    import captura
    try:
        import sounddevice as sd
        idx = captura.dispositivo_escolhido()
        mic = sd.query_devices(idx, "input")["name"]
        linha("Microfone reconhecido", "ok", mic)
    except captura.MicrofoneIndisponivel:
        mic = config.get("manual_mic_nome", "") or config.get("mic_preferido_nome", "")
        linha("Microfone reconhecido", "ruim", f"{mic} ausente — confira o receptor USB")
    except Exception as e:
        linha("Microfone reconhecido", "nao-sei", type(e).__name__)

    # dados
    banco = config.caminho_banco()
    if banco.exists():
        try:
            import historico
            n = len(historico.ultimos(100000))
        except Exception:
            n = "?"
        linha("Historico", "ok", f"{n} ditados · {banco.stat().st_size // 1024} KB")
    else:
        linha("Historico", "nao-sei", "ainda nao existe")

    # logs
    for rotulo, arquivo in (("Log do app", caminho()),
                            ("Log do keep-warm", dir_logs() / "keepwarm.log"),
                            ("Log do guardiao", dir_logs() / "guardiao.log")):
        linha(rotulo, "ok" if arquivo.exists() else "nao-sei", idade(arquivo))

    print("  " + "-" * 56)
    print(f"  Dados em: {base_dados()}")
    print(f"  Config:   {config.caminho_config()}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
