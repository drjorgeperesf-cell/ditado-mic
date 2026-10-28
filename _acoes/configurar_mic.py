# -*- coding: utf-8 -*-
"""CONFIGURAR-MIC — grava ~3 s de cada MICROFONE, mede o pico e da o veredito.

Espelha o `CONFIGURAR-MIC.command` do Mac:
  mudo = 0.0 exato · ruido de sala = 0,005 a 0,05 · fala = acima de 0,1
E faz backup datado do config antes de mudar qualquer coisa.

Diagnostico honesto (11/10/2026):
- UMA linha por APARELHO FISICO: o Windows lista o mesmo mic 3-4x (um por
  driver) e o MME corta o nome em 31 caracteres.
- Cada medicao diz o driver que DE FATO captou. O teste "processo novo abre
  WASAPI" de 11/10 foi falso positivo: a fonte caiu calada no DirectSound e a
  medicao parecia do WASAPI.
- Grava em `manual_mic_nome` a CHAVE normalizada do aparelho (ex.: 'fifine
  microphone'), que sobrevive ao '2- ' da reenumeracao USB e ao corte do MME.

EVITE MICROFONE BLUETOOTH: usar o fone como mic forca o perfil de chamada
(HFP/SCO), o audio fica ruim e o fone para de tocar som — mesma fisica do Bose
no Mac. Os BT aparecem marcados e SO sao medidos se ele digitar SIM.
"""
from __future__ import annotations

import array
import shutil
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))
sys.path.insert(0, str(RAIZ / "_acoes"))

import captura     # noqa: E402
import config      # noqa: E402
from log import log  # noqa: E402

SEGUNDOS = 3.0
API_CURTA = {"Windows WASAPI": "WASAPI", "MME": "MME",
             "Windows DirectSound": "DirectSound"}


def perguntar(texto):
    """input() em que stdin fechado (sem console) devolve None = CANCELAR.

    O perguntar do _acoes_util devolve "" sem console, e aqui "" e' ENTER
    (comecar a medir / voltar pro automatico): rodado sem ninguem na frente,
    o script abria todos os mics e regravava o config sozinho."""
    try:
        return input(texto)
    except (EOFError, KeyboardInterrupt):
        print("\n  (sem console interativo — tratando como CANCELAR)")
        return None


def medir(idx):
    """Grava SEGUNDOS do dispositivo `idx` e devolve (pico, indice que DE FATO
    captou). Pico -1 = nao abriu ou nao recebeu audio (indice None).

    A lista deste processo e' fresca (acabou de ser lida), entao a fonte pode
    cair no MME/DS do MESMO aparelho — por isso o driver que captou e'
    devolvido, nunca presumido."""
    fonte = captura.FonteMicrofone(idx, alternativas=True)
    pico, blocos, erros = [0.0], [0], []

    def ao_bloco(dados):
        a = array.array("h")
        a.frombytes(dados[:len(dados) - (len(dados) % 2)])
        if a:
            pico[0] = max(pico[0], max(abs(int(x)) for x in a) / 32768.0)
            blocos[0] += 1

    def ler():
        try:
            fonte.ler(ao_bloco)
        except Exception as e:
            erros.append(type(e).__name__)
    thread = threading.Thread(target=ler, name="captura", daemon=True)
    try:
        thread.start()
        time.sleep(SEGUNDOS)
    except Exception as e:
        print(f"      (nao consegui abrir: {type(e).__name__})")
        return -1.0, None
    finally:
        fonte.parar()
        if thread.ident:
            thread.join(timeout=2.0)
    if erros or not blocos[0]:
        print("      (nao consegui receber audio)")
        return -1.0, None
    return pico[0], fonte.device


def veredito(p) -> str:
    if p < 0:
        return "nao abriu"
    if p == 0.0:
        return "MUDO (pico 0.00000 exato)"
    if p < 0.005:
        return "quase mudo"
    if p < 0.05:
        return "ruido de sala (normal em silencio)"
    return "captando fala"


def via(pedido, captou) -> str:
    """De que driver veio o audio. Diferente do pedido = o pedido FALHOU."""
    if not captou:
        return ""
    curta = API_CURTA.get(captou, captou)
    if captou == pedido:
        return f"  via {curta}"
    return f"  via {curta} ({API_CURTA.get(pedido, pedido)} falhou)"


def main():
    mics = captura.listar_microfones()
    aparelhos = captura.agrupar_aparelhos(mics)
    api_de = {d["idx"]: d["api"] for d in mics}
    print()
    if not aparelhos:
        print("  Nenhum microfone encontrado.")
        return 1
    print("  MICROFONES DESTE PC (um por aparelho)")
    print("  " + "-" * 56)
    for i, a in enumerate(aparelhos, 1):
        marca = "  << padrao" if a["padrao"] else ""
        bt = "  [BLUETOOTH — evite]" if a["bluetooth"] else ""
        print(f"   {i:2d}. {a['nome']}{bt}{marca}")
    print()
    print(f"  Vou gravar {SEGUNDOS:.0f} s de cada um. FALE ALGUMA COISA agora,")
    print("  em voz normal, ate o teste terminar.")
    resp = perguntar("  ENTER para comecar (qualquer outra coisa cancela): ")
    if resp is None or resp.strip():
        print("  Cancelado.")
        return 1
    medir_bt = False
    if any(a["bluetooth"] for a in aparelhos):
        print()
        print("  Medir o Bluetooth forca o fone para o perfil de chamada por alguns")
        print("  segundos (o som do fone para enquanto isso).")
        medir_bt = perguntar("  Digite SIM para medir o Bluetooth tambem "
                             "(ENTER pula): ") == "SIM"
    print()
    for i, a in enumerate(aparelhos, 1):
        rotulo = f"   {i:2d}. {a['nome'][:44]:<44}"
        if a["bluetooth"] and not medir_bt:
            print(f"{rotulo} (Bluetooth — nao medido)")
            continue
        pedido = a["entradas"][0]            # o melhor driver: WASAPI -> MME -> DS
        print(f"{rotulo} gravando...", end="", flush=True)
        p, dev = medir(pedido["idx"])
        captou = api_de.get(dev)
        print(f"\r{rotulo} pico {max(p, 0):.5f}  {veredito(p)}"
              f"{via(pedido['api'], captou)}")
        log(f"configurar-mic: {a['chave']} pico={max(p, 0):.5f} "
            f"pedido={pedido['api']} captou={captou or 'nada'}")
    print()
    r = perguntar("  Numero do microfone que voce quer usar "
                  "(ENTER = automatico, q cancela): ")
    if r is None or r.strip().lower() == "q":
        print("  Cancelado.")
        return 1
    r = r.strip()
    escolhido = None
    if r:
        try:
            n = int(r)
            if n < 1:
                raise IndexError
            escolhido = aparelhos[n - 1]
        except (ValueError, IndexError):
            print("  Numero invalido. Nada mudou.")
            return 1
        if escolhido["bluetooth"]:
            print()
            print("  ATENCAO: esse e um microfone Bluetooth. Usar o fone como mic")
            print("  forca o perfil de chamada: o audio fica ruim e o fone para de")
            print("  tocar som enquanto voce dita.")
            if perguntar("  Digite SIM para usar mesmo assim: ") != "SIM":
                print("  Nada mudou.")
                return 1
    # backup datado do config ANTES de mudar
    cfg = config.caminho_config()
    if cfg.exists():
        bkp = cfg.with_name(f"config.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(cfg, bkp)
        print(f"  backup do config: {bkp.name}")
    if escolhido is None:
        config.gravar({"manual_mic_nome": ""})
        preferido = config.get("mic_preferido_nome", "")
        print(f"  gravado: automatico ({preferido})." if preferido
              else "  gravado: padrao do sistema.")
        log("mic: escolhido no CONFIGURAR-MIC (automatico)")
        return 0
    # a CHAVE, nao o nome cru: vale com ou sem o '2- ' e no nome cortado do MME
    config.gravar({"manual_mic_nome": escolhido["chave"]})
    print(f"  gravado: {escolhido['nome']}  (chave '{escolhido['chave']}')")
    log(f"mic: escolhido no CONFIGURAR-MIC ({escolhido['chave']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
