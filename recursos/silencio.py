# -*- coding: utf-8 -*-
"""Portao de energia ANTES do whisper (anti-alucinacao na origem).

Medido nesta maquina (09/10/2026): 5 s de silencio digital voltaram do
whisper-server como "Transcricao e Legendas por Quintena Coelho" com
`no_speech_prob = 4e-10` — o sinal interno do modelo NAO pega isso. Entao o
filtro de texto (texto.py) so limpa o que ele conhece; a defesa de verdade e'
nao entregar ao modelo audio sem fala.

Duas coisas, ambas stdlib (wave + array), sem tocar no servidor (`--vad` segue
BANIDO — veto 4; isto aqui e' energia pura, sem estado entre requests):

 1. `sem_fala(analise)`: pico minusculo OU menos de `silencio_fala_min_s` de
    quadros com energia -> nao transcreve.
 2. `compactar(...)`: silencio LONGO (pausa no meio, rabo da gravacao depois do
    "fim" da fala) e' onde o whisper inventa texto. Encurta cada trecho sem
    fala maior que `silencio_run_min_s` para ~2 x `silencio_margem_s`. A fala
    nunca e' tocada (ha uma folga de 0,3 s em volta de todo quadro com energia).

Calibracao (fala real dele: RMS p50 ~0,026, p90 ~0,09-0,17; silencio e ruido de
sala <= 0,0005): limiar 0,008 fica ~16x acima do ruido e ~3x abaixo da fala
media. "Nao sei" != "quebrado": qualquer duvida (formato estranho, erro de
leitura) devolve None e o audio segue INTEIRO para o whisper.
"""
from __future__ import annotations

import array
import math
import operator
import wave
from pathlib import Path

import config
from log import log

QUADRO_MS = 20
_FOLGA_QUADROS = 15            # 0,3 s de folga em volta da fala


def _cfg_float(chave, padrao):
    try:
        return float(config.get(chave, padrao))
    except (TypeError, ValueError):
        return float(padrao)


def _ler(caminho):
    """(taxa, amostras) ou None se nao for WAV PCM16 mono."""
    try:
        with wave.open(str(caminho), "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return None
            taxa = w.getframerate()
            bruto = w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    a = array.array("h")
    a.frombytes(bruto[: len(bruto) - (len(bruto) % 2)])
    return taxa, a


def _rms_por_quadro(a, f):
    mul = operator.mul
    out = []
    for i in range(0, len(a) - f + 1, f):
        bloco = a[i:i + f]
        out.append(math.sqrt(sum(map(mul, bloco, bloco)) / f) / 32768.0)
    return out


def analisar(caminho):
    """Devolve dict(duracao_s, pico, fala_s, limiar, ativo, taxa) ou None."""
    lido = _ler(caminho)
    if not lido:
        return None
    taxa, a = lido
    if taxa <= 0 or not len(a):
        return None
    f = int(taxa * QUADRO_MS / 1000)
    rms = _rms_por_quadro(a, f)
    if not rms:
        return None
    pico = max(max(a), -min(a)) / 32768.0
    ordenado = sorted(rms)
    piso = ordenado[int(len(ordenado) * 0.10)]
    limiar_abs = _cfg_float("silencio_limiar_rms", 0.008)
    # relativo ao ruido de sala, mas nunca acima de 0,02 (fala baixa tem que passar)
    limiar = min(0.02, max(limiar_abs, 4.0 * piso))
    ativo = [r > limiar for r in rms]
    return {
        "duracao_s": len(a) / taxa,
        "pico": pico,
        "fala_s": sum(ativo) * QUADRO_MS / 1000.0,
        "limiar": limiar,
        "ativo": ativo,
        "taxa": taxa,
        "amostras_por_quadro": f,
    }


def sem_fala(an) -> bool:
    """True se NAO ha fala suficiente para valer uma transcricao.

    Zona cinza (40-300 ms de energia, ex.: um "sim" curtinho ou palavra cortada)
    NAO barra: transcreve e o texto sofre o filtro rigido (texto.higienizar).
    """
    if not an or not config.get("silencio_gate_ativo", True):
        return False
    if an["pico"] < _cfg_float("silencio_pico_min", 0.008):
        return True
    return an["fala_s"] < _cfg_float("silencio_fala_min_s", 0.04)


def _trechos_mantidos(an):
    """Lista de (ini, fim) em QUADROS que ficam. None se nada a cortar."""
    ativo = an["ativo"]
    n = len(ativo)
    # dilata a fala em +-_FOLGA_QUADROS
    perto = [False] * n
    ultimo = -10 ** 9
    for i in range(n):
        if ativo[i]:
            ultimo = i
        if i - ultimo <= _FOLGA_QUADROS:
            perto[i] = True
    ultimo = 10 ** 9
    for i in range(n - 1, -1, -1):
        if ativo[i]:
            ultimo = i
        if ultimo - i <= _FOLGA_QUADROS:
            perto[i] = True
    run_min = int(_cfg_float("silencio_run_min_s", 3.0) * 1000 / QUADRO_MS)
    margem = int(_cfg_float("silencio_margem_s", 0.5) * 1000 / QUADRO_MS)
    mantidos, cortou = [], False
    i = 0
    while i < n:
        j = i
        while j < n and perto[j] == perto[i]:
            j += 1
        if perto[i] or (j - i) <= run_min:
            mantidos.append((i, j))
        else:
            cortou = True
            nasce_do_inicio = (i == 0)
            termina_no_fim = (j == n)
            if nasce_do_inicio:
                mantidos.append((max(i, j - margem), j))
            elif termina_no_fim:
                mantidos.append((i, min(j, i + margem)))
            else:
                mantidos.append((i, min(j, i + margem)))
                mantidos.append((max(i, j - margem), j))
        i = j
    return mantidos if cortou else None


def ganho_para(pico) -> float:
    """Ganho a aplicar (1.0 = nenhum). So para fala BAIXA (sussurro, mic
    longe): pico abaixo de `ganho_pico_abaixo`. Leva o pico ate
    `ganho_pico_alvo` (< 1: nunca clipa), no maximo `ganho_max` vezes."""
    if not config.get("ganho_fala_baixa_ativo", True):
        return 1.0
    try:
        pico = float(pico)
    except (TypeError, ValueError):
        return 1.0
    abaixo = _cfg_float("ganho_pico_abaixo", 0.25)
    alvo = min(0.95, _cfg_float("ganho_pico_alvo", 0.5))
    teto = _cfg_float("ganho_max", 6.0)
    if pico <= 0 or pico >= abaixo or alvo <= pico:
        return 1.0
    g = min(teto, alvo / pico)
    return g if g >= 1.2 else 1.0


def amplificar(caminho, an):
    """Grava `<wav>.ganho.wav` com ganho digital para fala baixa. Devolve
    (Path, ganho) ou (None, 1.0) quando nao precisa / qualquer duvida.

    Roda DEPOIS de `sem_fala()` (o portao continua julgando o audio
    ORIGINAL: ganho nunca transforma silencio em "fala"). `--vad` segue
    banido; isto e' so multiplicar amostras, sem estado entre requests."""
    if not an or sem_fala(an):
        return None, 1.0
    g = ganho_para(an.get("pico"))
    if g <= 1.0:
        return None, 1.0
    try:
        lido = _ler(caminho)
        if not lido:
            return None, 1.0
        taxa, a = lido
        # o pico real deste arquivo (pode ser o compactado) limita o ganho
        pico_aqui = max(max(a), -min(a)) / 32768.0 if len(a) else 0.0
        if pico_aqui > 0:
            g = min(g, 0.95 / pico_aqui)
        if g <= 1.0:
            return None, 1.0
        saida = array.array("h", [int(s * g) for s in a])
        destino = Path(str(caminho) + ".ganho.wav")
        with wave.open(str(destino), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(taxa)
            w.writeframes(saida.tobytes())
        return destino, g
    except Exception as e:                              # nunca derruba o ditado
        log(f"silencio: ganho falhou ({type(e).__name__}) — segue sem ganho")
        return None, 1.0


def compactar(caminho, an):
    """Grava `<wav>.cmp.wav` sem os silencios longos. Devolve (Path, segundos
    cortados) ou (None, 0.0) quando nao ha o que cortar / qualquer duvida."""
    if (not an or not config.get("silencio_compactar", True)
            or not config.get("silencio_gate_ativo", True)):
        return None, 0.0
    try:
        mantidos = _trechos_mantidos(an)
        if not mantidos:
            return None, 0.0
        lido = _ler(caminho)
        if not lido:
            return None, 0.0
        taxa, a = lido
        f = an["amostras_por_quadro"]
        saida = array.array("h")
        for ini, fim in mantidos:
            saida.extend(a[ini * f:fim * f])
        saida.extend(a[len(an["ativo"]) * f:])          # resto parcial do ultimo quadro
        destino = Path(str(caminho) + ".cmp.wav")
        with wave.open(str(destino), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(taxa)
            w.writeframes(saida.tobytes())
        cortado = (len(a) - len(saida)) / taxa
        return destino, cortado
    except Exception as e:                              # nunca derruba o ditado
        log(f"silencio: compactacao falhou ({type(e).__name__}) — segue inteiro")
        return None, 0.0
