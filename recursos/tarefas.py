# -*- coding: utf-8 -*-
"""Tarefas do Agendador — o `launchd` daqui.

Prefixo obrigatorio: `DitadoMic-`. Quatro tarefas:

  DitadoMic-App        app de bandeja, no logon (pythonw, sem janela)
  DitadoMic-Servidor   whisper-server, no logon
  DitadoMic-Guardiao   a cada 120 s: sobe quem caiu
  DitadoMic-KeepWarm   a cada 150 s: ping de silencio no servidor

`KeepAlive` do launchd vira o **guardiao**; o `StartInterval` do launchd era
VETADO la (podia pendurar para sempre se o disparo vencesse durante o sleep),
mas no Agendador do Windows o gatilho repetitivo e' o mecanismo normal e nao
tem esse modo de falha. Mesmo assim, cada tarefa periodica LOGA UMA LINHA POR
CICLO com o motivo — guarda de script nunca da `exit 0` silencioso.

Tudo **sem exigir administrador**: gatilho de logon do usuario, `/RL LIMITED`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

RAIZ = Path(__file__).resolve().parent.parent
PREFIXO = "DitadoMic-"

PYW = RAIZ / ".venv" / "Scripts" / "pythonw.exe"
PY = RAIZ / ".venv" / "Scripts" / "python.exe"

TAREFAS = {
    "App": {
        "cmd": (str(PYW), "", str(RAIZ / "recursos" / "app.py")),
        "gatilho": ["/SC", "ONLOGON"],
        "desc": "app de bandeja + pilula + gancho de teclado, no logon",
    },
    "Servidor": {
        "cmd": (str(PYW), "", str(RAIZ / "_acoes" / "subir_servidor.py")),
        "gatilho": ["/SC", "ONLOGON"],
        "desc": "whisper-server 127.0.0.1:7777, no logon",
    },
    "Guardiao": {
        "cmd": (str(PYW), "", str(RAIZ / "_acoes" / "guardiao.py")),
        "gatilho": ["/SC", "MINUTE", "/MO", "2"],     # 120 s
        "desc": "a cada 120 s: sobe o que caiu",
    },
    "KeepWarm": {
        "cmd": (str(PYW), "", str(RAIZ / "_acoes" / "keepwarm.py")),
        "gatilho": ["/SC", "MINUTE", "/MO", "3"],     # ~150 s (teto do schtasks)
        "desc": "ping de silencio no servidor (motor sempre quente)",
    },
}


def _rodar(args, timeout=30, com_erro=False):
    """`com_erro=True` junta o stderr ao stdout.

    Sem isso, a mensagem do `schtasks` ("O sistema nao pode encontrar o
    arquivo especificado") ia pro DEVNULL e toda tarefa ausente virava
    `desconhecido` — que e' exatamente o estado que NAO dispara acao.
    """
    from proc import rodar
    try:
        r = rodar(args, timeout=timeout, juntar_erro=com_erro)
        return r.returncode, (r.stdout or "").strip()
    except Exception as e:
        return -1, type(e).__name__


def nome(chave) -> str:
    return PREFIXO + chave


def usuario_atual() -> str:
    """SID do usuario que instala, sem depender de nome traduzido ou dominio."""
    import csv
    rc, out = _rodar(["whoami", "/USER", "/FO", "CSV", "/NH"], com_erro=True)
    if rc == 0:
        for linha in csv.reader(out.splitlines()):
            if len(linha) == 2 and linha[1].startswith("S-1-"):
                return linha[1]
    raise RuntimeError("Nao foi possivel identificar o usuario das tarefas")


def _xml(chave, usuario=None) -> str:
    """XML da tarefa. `InteractiveToken` + `LeastPrivilege`.

    Por XML e nao por `/TR`, por tres motivos que doem nesta pasta:
      - o caminho tem ESPACO e PARENTESES ("Ditado Mic"), que o
        `/TR` escapa mal;
      - `InteractiveToken` e' o que garante sessao de desktop: um app de
        interface NAO pode rodar como servico (nao teria pilula nem bandeja);
      - da para desligar `StopIfGoingOnBatteries`, `DisallowStartIfOnBatteries`
        e o `ExecutionTimeLimit`, que por padrao MATARIAM o app depois de 3
        dias ou quando ele desconectasse o carregador.
    """
    t = TAREFAS[chave]
    prog, _, args = t["cmd"]
    usuario = escape(usuario or usuario_atual())
    if t["gatilho"][1] == "ONLOGON":
        # Sem UserId, o gatilho vale para TODOS os usuarios e exige admin.
        gatilho = ("<LogonTrigger><Enabled>true</Enabled>"
                   f"<UserId>{usuario}</UserId></LogonTrigger>")
    else:
        minutos = t["gatilho"][3]
        gatilho = (
            "<TimeTrigger>"
            "<Repetition>"
            f"<Interval>PT{minutos}M</Interval>"
            "<StopAtDurationEnd>false</StopAtDurationEnd>"
            "</Repetition>"
            "<StartBoundary>2026-01-01T00:00:00</StartBoundary>"
            "<Enabled>true</Enabled>"
            "</TimeTrigger>")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        f'  <RegistrationInfo><Description>{t["desc"]}</Description>'
        '</RegistrationInfo>\n'
        f'  <Triggers>{gatilho}</Triggers>\n'
        '  <Principals><Principal id="Author">'
        f'<UserId>{usuario}</UserId>'
        '<LogonType>InteractiveToken</LogonType>'
        '<RunLevel>LeastPrivilege</RunLevel>'
        '</Principal></Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n'
        '    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd>'
        '<RestartOnIdle>false</RestartOnIdle></IdleSettings>\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\n'
        '    <Enabled>true</Enabled>\n'
        '    <Hidden>false</Hidden>\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n'
        '    <WakeToRun>false</WakeToRun>\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
        '    <Priority>7</Priority>\n'
        '  </Settings>\n'
        '  <Actions Context="Author"><Exec>\n'
        f'    <Command>{escape(prog)}</Command>\n'
        f'    <Arguments>"{escape(args)}"</Arguments>\n'
        f'    <WorkingDirectory>{escape(str(RAIZ))}</WorkingDirectory>\n'
        '  </Exec></Actions>\n'
        '</Task>\n')


def criar_todas():
    """Cria as 4 tarefas por XML. Devolve [(nome, ok, detalhe)]."""
    import tempfile
    saida = []
    usuario = usuario_atual()
    for chave, t in TAREFAS.items():
        n = nome(chave)
        # O schtasks exige o XML em UTF-16 (ele mesmo declara isso no header).
        f = Path(tempfile.gettempdir()) / f"{n}.xml"
        try:
            f.write_text(_xml(chave, usuario), encoding="utf-16")
            rc, out = _rodar(["schtasks", "/Create", "/TN", n, "/XML", str(f),
                              "/F"], com_erro=True)
            saida.append((n, rc == 0, t["desc"] if rc == 0 else out[:160]))
        finally:
            try:
                f.unlink()
            except OSError:
                pass
    return saida


def apagar_todas():
    saida = []
    for chave in TAREFAS:
        n = nome(chave)
        _rodar(["schtasks", "/End", "/TN", n])
        rc, out = _rodar(["schtasks", "/Delete", "/TN", n, "/F"])
        saida.append((n, rc == 0, out[:120]))
    return saida


def rodar_todas():
    for chave in ("Servidor", "App"):
        _rodar(["schtasks", "/Run", "/TN", nome(chave)])


def estado(chave) -> str:
    """"pronta" | "rodando" | "ausente" | "desconhecido".

    'desconhecido' e' um estado SEPARADO de proposito (licao 6.11): antes de
    dar ACAO a um estado, e' preciso saber como ele e' produzido em FALHA.
    `schtasks` com rc != 0 NAO significa "a tarefa nao existe".
    """
    rc, out = _rodar(["schtasks", "/Query", "/TN", nome(chave), "/FO", "LIST"],
                     timeout=15, com_erro=True)
    baixo = out.lower()
    if rc != 0:
        # O schtasks e' LOCALIZADO: em pt-BR a frase e' "O sistema nao pode
        # encontrar o arquivo especificado". So estas frases significam
        # "nao existe"; qualquer outra falha (timeout, acesso negado) e'
        # `desconhecido`, e desconhecido nao dispara acao nenhuma.
        for marca in ("cannot find the file", "does not exist",
                      "encontrar o arquivo", "não existe", "nao existe"):
            if marca in baixo:
                return "ausente"
        return "desconhecido"
    # Le so a linha do Status (a chave inteira): "Hora da proxima EXECUcao"
    # contem "execu" e viraria "rodando" por engano.
    for linha in out.splitlines():
        rotulo, _, valor = linha.partition(":")
        if rotulo.strip().lower() not in ("status", "estado"):
            continue
        v = valor.strip().lower()
        if v.startswith("running") or v.startswith("em execu"):
            return "rodando"
        if v.startswith("disabled") or v.startswith("desabilit"):
            return "desabilitada"
        if v.startswith("ready") or v.startswith("pronto"):
            return "pronta"
    return "pronta"


def main():
    for chave in TAREFAS:
        print(f"{nome(chave):40s} {estado(chave)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
