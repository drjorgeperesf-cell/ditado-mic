# -*- coding: utf-8 -*-
"""REINICIAR — sobe de novo o que caiu.

O APP DO DITADO nunca se reinicia sozinho (derrubaria o gancho de teclado no
meio de um ditado). Pelo REINICIAR.bat e' o usuário quem manda, entao pode —
mesmo com o app travado gravando: SEM flag, nada aqui espera o app.

Quem reinicia SEM o usuário olhando (um agente, depois de atualizar o codigo)
usa `--so-se-ocioso`: espera o app ficar OCIOSO e so entao reinicia; se nao
ficar dentro de `--esperar-s` (padrao 15 min), NAO reinicia (sai 3). Em
11/10/2026 um reinicio caiu com uma captura aberta (sobrou o WAV orfao
`rec\\wispr-rec-19004-1.wav`, 31,8 s). `--checar` so diz o estado (sai 0 =
ocioso, 3 = ocupado ou "nao sei") e nao mexe em nada.

OCIOSO, lido de FORA do app (o estado dele so existe na memoria):
- nenhum `rec\\wispr-rec-<pid>-<n>.wav` de um processo VIVO do app mexido nos
  ultimos 15 min (o app apaga o WAV logo depois de transcrever; WAV de PID
  morto e' sobra de crash/reinicio e nao conta);
- nenhuma linha nova no log nos ultimos 10 s (colar e devolver o clipboard
  acontecem DEPOIS de o WAV sumir);
- nenhuma operacao em voo no log (formatado tardio, corretor, formatar
  selecao), cada uma com teto de tempo;
- sonda de processos que falhou = "nao sei" = NAO reinicia (licao 6.11).
Reinicia so depois de DUAS leituras ociosas seguidas. E' heuristica: entre a
ultima leitura e o /End ainda ha ~1 s em que um Alt direito novo seria cortado.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))

import tarefas      # noqa: E402
import transcricao  # noqa: E402
from proc import rodar  # noqa: E402

QUIETO_S = 10.0              # log parado ha pelo menos isto
WAV_RECENTE_S = 900.0        # WAV mais velho que isto e' sobra (falha total preservada)
ESPERA_PADRAO_S = 900.0      # --so-se-ocioso: desiste depois disto
INTERVALO_S = 5.0            # entre leituras
LEITURAS_OCIOSAS = 2         # seguidas, antes do /End (uma so pode ser o vao
                             # entre o press e o 1o bloco do WAV)
LINHAS_LOG = 400             # so a cauda do log

# Operacoes que o log ABRE e FECHA. So prefixos fixos de cada modulo: texto de
# ditado nunca vai pro log. Passado o teto, a operacao conta como encerrada.
EM_VOO = (
    ("formatado tardio",
     ("colar: texto colado",),
     ("formatado: pronto", "formatado: pedido falhou", "formatado: fila cheia",
      "selecao: pronto",             # a selecao cola o PROPRIO formatado
      "colar-ultimo:",               # Win+Alt+V cola sem formatado depois
      "comando: pronto"),            # a instrucao falada cola o resultado
     400.0),                         # formatado_timeout_s 180 x 2 tentativas
    ("instrucao falada",
     ("comando: pedindo",),
     ("comando: pronto", "comando: falhou"),
     150.0),                         # comando_voz_timeout_s 90 x 2 (desvio da API)
    ("correcao do Ctrl direito",
     ("corretor: revisando",),
     ("corretor: reescrito", "corretor: revisao recusada", "corretor: fluxo quebrou",
      "aviso:"),
     120.0),
    ("formatar selecao",
     ("corretor: aperto sem texto digitado",),
     ("selecao: pronto", "selecao: fluxo quebrou", "selecao: sem selecao",
      "selecao: pedido concorrente", "aviso:"),
     200.0),
)
_RE_WAV = re.compile(r"^wispr-rec-(\d+)-", re.IGNORECASE)


def _sch(*args):
    return rodar(["schtasks"] + list(args), timeout=25)


def esperar_app_encerrar(timeout=15.0):
    """/End retorna antes de o Python filho soltar o mutex da instancia."""
    from guardiao import app_esta_vivo
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        vivo = app_esta_vivo()
        if vivo is False:
            return True
        if vivo is None:
            return False
        time.sleep(0.3)
    return False


# ── o app esta ocioso? (so leitura: nada aqui mexe no app) ───────────────
def _sonda_confiavel(todos_os_pids) -> bool:
    """Quem roda isto e' um python.exe: se a lista nao tem NEM ESTE processo,
    a sonda veio vazia/quebrada — e "vazio" nao pode virar "app fechado"."""
    return os.getpid() in todos_os_pids


def pids_do_app():
    """PIDs vivos de `recursos\\app.py` (o lancador da venv e o interpretador,
    que e' quem da nome ao WAV). None = a sonda falhou ("nao sei")."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or "
          "Name='python.exe'\" | ForEach-Object { [string]$_.ProcessId + '|' + "
          "$_.CommandLine }")
    try:
        r = rodar(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                  timeout=25)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    alvo = str(RAIZ / "recursos" / "app.py").lower()
    todos, do_app = set(), set()
    for linha in (r.stdout or "").splitlines():
        pid, _, cmd = linha.partition("|")
        pid = pid.strip()
        if not pid.isdigit():
            continue
        todos.add(int(pid))
        if alvo in cmd.lower():
            do_app.add(int(pid))
    if not _sonda_confiavel(todos):
        return None
    return do_app


def _instante(linha):
    """'2026-10-11 17:45:32 ...' -> epoch (hora local). None se ilegivel."""
    try:
        return time.mktime(time.strptime(linha[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def _cauda_do_log(caminho, n=LINHAS_LOG):
    try:
        texto = Path(caminho).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return texto.splitlines()[-n:]


def wav_em_uso(pids, dir_rec, agora):
    """Nome do WAV que um processo VIVO do app esta gravando/transcrevendo."""
    try:
        arquivos = list(Path(dir_rec).iterdir())
    except OSError:
        return None
    for p in sorted(arquivos):
        m = _RE_WAV.match(p.name)
        if not m or int(m.group(1)) not in pids:
            continue
        try:
            if agora - p.stat().st_mtime < WAV_RECENTE_S:
                return p.name
        except OSError:
            continue
    return None


def em_voo(linhas, agora):
    """Operacao aberta no log e ainda dentro do teto: o nome dela, ou None.
    'app: subindo' zera tudo (o processo velho morreu com o que tinha)."""
    abertas = {nome: [0, None] for nome, _a, _f, _t in EM_VOO}
    for linha in linhas:
        ts = _instante(linha)
        msg = linha[20:] if ts is not None else linha
        if msg.startswith("app: subindo"):
            for v in abertas.values():
                v[0], v[1] = 0, None
            continue
        for nome, abre, fecha, _teto in EM_VOO:
            v = abertas[nome]
            if msg.startswith(abre):
                v[0], v[1] = v[0] + 1, ts
            elif msg.startswith(fecha):
                v[0] = max(0, v[0] - 1)
    for nome, _abre, _fecha, teto in EM_VOO:
        n, ts = abertas[nome]
        if n > 0 and ts is not None and agora - ts < teto:
            return nome
    return None


def estado_do_app(sondar_pids=None, dir_rec=None, caminho_log=None, agora=None):
    """(ocioso, motivo). True = pode reiniciar; False = ocupado; None = nao sei."""
    from log import base_dados, caminho as caminho_do_log
    pids = (sondar_pids or pids_do_app)()
    if pids is None:
        return None, "sonda dos processos falhou (nao sei)"
    if not pids:
        return True, "app fechado"
    agora = time.time() if agora is None else agora
    wav = wav_em_uso(pids, base_dados() / "rec" if dir_rec is None else dir_rec, agora)
    if wav:
        return False, f"gravando ou transcrevendo ({wav})"
    linhas = _cauda_do_log(caminho_do_log() if caminho_log is None else caminho_log)
    ultimo = next((t for t in map(_instante, reversed(linhas)) if t is not None), None)
    # relogio andou para tras mais de 1 min: a linha "do futuro" nao conta
    if ultimo is not None and -60.0 <= agora - ultimo < QUIETO_S:
        return False, f"log ativo ha {max(0.0, agora - ultimo):.0f} s"
    op = em_voo(linhas, agora)
    if op:
        return False, f"{op} em andamento"
    return True, "ocioso"


def _dormir(segundos):
    time.sleep(segundos)


def esperar_ocioso(teto_s=ESPERA_PADRAO_S, intervalo_s=INTERVALO_S, sondar=None,
                   dormir=None, relogio=None):
    """Espera LEITURAS_OCIOSAS leituras ociosas seguidas. (True, motivo) |
    (False, motivo). "Nao sei" (None) nunca conta como ocioso."""
    sondar = sondar or estado_do_app
    dormir = dormir or _dormir
    relogio = relogio or time.monotonic
    limite = relogio() + max(0.0, float(teto_s))
    seguidas, ultimo = 0, None
    while True:
        ocioso, motivo = sondar()
        if ocioso is True:
            seguidas += 1
            if seguidas >= LEITURAS_OCIOSAS:
                return True, motivo
        else:
            seguidas = 0
            if motivo != ultimo:
                print(f"  [..]   esperando: {motivo}")
                ultimo = motivo
            if relogio() >= limite:
                return False, motivo
        dormir(intervalo_s)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reinicia o Microfone (app + motor).")
    ap.add_argument("--so-se-ocioso", action="store_true",
                    help="espera o app ficar ocioso; se nao ficar, NAO reinicia (sai 3)")
    ap.add_argument("--esperar-s", type=float, default=ESPERA_PADRAO_S,
                    help="teto da espera do --so-se-ocioso, em segundos")
    ap.add_argument("--checar", action="store_true",
                    help="so diz se o app esta ocioso (sai 0) ou nao (sai 3)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.checar:
        ocioso, motivo = estado_do_app()
        rotulo = {True: "OCIOSO", False: "OCUPADO"}.get(ocioso, "NAO SEI")
        print(f"  {rotulo}: {motivo}")
        return 0 if ocioso is True else 3

    print()
    if args.so_se_ocioso:
        print("  Esperando o Microfone ficar ocioso (nenhum ditado e' cortado)...")
        ok, motivo = esperar_ocioso(args.esperar_s)
        if not ok:
            print(f"  [--]   NAO reiniciado: {motivo}")
            return 3
        print(f"  [ok]   {motivo}")
    print("  Reiniciando o Microfone...")
    tudo_ok = True
    for chave in ("App", "Servidor"):
        n = tarefas.nome(chave)
        _sch("/End", "/TN", n)
        if chave == "App" and not esperar_app_encerrar():
            print("  [FALHOU] o app ainda esta fechando ou a sonda falhou; tente novamente.")
            tudo_ok = False
            continue
        r = _sch("/Run", "/TN", n)
        print(f"  {'[ok]  ' if r.returncode == 0 else '[FALHOU]'} {n}")
        tudo_ok = tudo_ok and r.returncode == 0
    print("  esperando o motor responder (ate 40 s)...")
    limite = time.time() + 40
    while time.time() < limite:
        if transcricao.servidor_vivo(timeout=2.0):
            print("  [ok]   motor de transcricao no ar")
            return 0 if tudo_ok else 1
        time.sleep(1.0)
    print("  [?]    o motor ainda nao respondeu.")
    print("  Na PRIMEIRA vez depois de trocar o driver da placa de video,")
    print("  ele leva ~25 s a mais para compilar. Espere e rode o STATUS.bat.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
