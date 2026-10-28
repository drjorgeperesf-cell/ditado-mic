# -*- coding: utf-8 -*-
"""Transcricao: POST no whisper-server local, fallback no whisper-cli.

O protocolo HTTP do whisper.cpp e' o mesmo no Windows, entao a logica de
requisicao do app.py vale como esta:
  POST multipart /inference  com file, language, response_format=text,
  prompt (se o perfil tiver), carry_initial_prompt, max_context.

TIMEOUT ADAPTATIVO: min(300, 25 + 0,5 x segundos_de_audio). Audio de 10 s falha
em ~30 s, nao em 3 min.

REGRA DO max_context (lida no fonte do whisper.cpp v1.9.1):
  n_take0 = ultimos min(tokens_do_prompt, max_context-1) do prompt
  n_take1 = max_context - n_take0 - 1   (texto herdado da janela anterior)
Ou seja: enquanto (max_context - 1) <= tokens_do_prompt, o contexto rolante e'
ZERO e o anti-loop continua de pe mesmo com prompt.
`max_context == 1` e' PROIBIDO (coagido pra 0): da n_take1 = -1 e o whisper.cpp
le fora do vetor.

`--vad` NAO e' usado NUNCA (veto 4): vazava estado entre requests
(req01=0,4 s -> req20=7,5 s) e ficava surdo.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import config
from log import log
from proc import rodar

SERVER_REQUEST_TIMEOUT = 300
SERVER_TIMEOUT_BASE_S = 25.0
SERVER_TIMEOUT_FATOR = 0.5

_HASH_AVISADO = False


def _tokens_aproximados(prompt: str) -> int:
    """Estimativa conservadora de tokens do prompt (so pra guarda do knob)."""
    return max(1, len(prompt) // 3)


def max_context(initial_prompt: str = "") -> int:
    """Contexto seguro desta transcricao.

    Sem prompt: 0 (anti-loop puro — janela nova nao herda texto da anterior).
    Com prompt: o valor CALIBRADO do config, e so se o sha256 do prompt bater
    com o calibrado. Prompt diferente clampa pra 64, com aviso UNICO.
    """
    global _HASH_AVISADO
    if not initial_prompt:
        return 0
    try:
        valor = int(config.get("whisper_max_context_prompt", 117))
    except (TypeError, ValueError):
        valor = 64
    if valor <= 0:
        return 0
    if valor == 1:                      # PROIBIDO: n_take1 = -1 no whisper.cpp
        return 0
    if valor > 64:
        esperado = str(config.get("whisper_max_context_prompt_hash", "") or "")
        atual = hashlib.sha256(initial_prompt.encode("utf-8")).hexdigest()
        if esperado and atual != esperado:
            if not _HASH_AVISADO:
                _HASH_AVISADO = True
                log("max_context: prompt mudou sem recalibrar — clampado em 64")
            return 64
    return min(224, valor)              # teto fisico do whisper.cpp


def max_context_efetivo(prompt_base: str, parte_dicionario: str = "") -> int:
    """max_context com o dicionario pessoal na FRENTE do prompt base.

    O base (calibrado, sha256) segue com o valor de `max_context(base)`; o
    dicionario soma uma estimativa PESSIMISTA (para baixo) de tokens, com teto
    fisico de 224. Subestimar so faz o whisper.cpp cortar os primeiros termos
    do dicionario (ele corta pelo comeco); o base nunca e' cortado e o
    contexto rolante continua ZERO (anti-loop de pe).
    """
    mc = max_context(prompt_base)
    if mc <= 0 or not parte_dicionario:
        return mc
    return min(224, mc + config.tokens_estimados_dicionario(parte_dicionario))


def timeout_adaptativo(segundos_audio: float) -> float:
    try:
        base = float(config.get("server_timeout_base_s", SERVER_TIMEOUT_BASE_S))
        fator = float(config.get("server_timeout_fator", SERVER_TIMEOUT_FATOR))
        teto = float(config.get("server_request_timeout", SERVER_REQUEST_TIMEOUT))
    except (TypeError, ValueError):
        base, fator, teto = SERVER_TIMEOUT_BASE_S, SERVER_TIMEOUT_FATOR, 300.0
    return min(teto, base + fator * max(0.0, float(segundos_audio or 0)))


def _multipart(campos: dict, wav: Path) -> tuple[bytes, str]:
    b = "----MicrofoneBoundary" + uuid.uuid4().hex
    partes = []
    for k, v in campos.items():
        partes.append(
            f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
            .encode("utf-8"))
    partes.append(
        f'--{b}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{wav.name}"\r\nContent-Type: audio/wav\r\n\r\n'.encode("utf-8"))
    partes.append(wav.read_bytes())
    partes.append(f"\r\n--{b}--\r\n".encode("utf-8"))
    return b"".join(partes), f"multipart/form-data; boundary={b}"


def servidor_vivo(timeout=2.0) -> bool:
    """GET de liveness — roda ANTES de qualquer check de arquivo."""
    try:
        c = http.client.HTTPConnection(config.get("server_host", "127.0.0.1"),
                                       int(config.get("server_port", 7777)),
                                       timeout=timeout)
        c.request("GET", "/")
        r = c.getresponse()
        r.read(1)
        c.close()
        return True
    except Exception:
        return False


def via_servidor(wav: Path, segundos_audio: float, initial_prompt: str = "",
                 mc=None):
    """POST no whisper-server. Devolve (texto|None, erro|None, segundos).
    `mc` (opcional) = max_context ja calculado (prompt com dicionario)."""
    t0 = time.monotonic()
    campos = {
        "language": str(config.get("language", "pt")),
        "response_format": "text",
    }
    if mc is None:
        mc = max_context(initial_prompt)
    if initial_prompt:
        campos["prompt"] = initial_prompt
        campos["carry_initial_prompt"] = "true"
    campos["max_context"] = str(mc)
    corpo, ctype = _multipart(campos, wav)
    tmo = timeout_adaptativo(segundos_audio)
    try:
        c = http.client.HTTPConnection(config.get("server_host", "127.0.0.1"),
                                       int(config.get("server_port", 7777)),
                                       timeout=tmo)
        c.request("POST", "/inference", body=corpo,
                  headers={"Content-Type": ctype, "Content-Length": str(len(corpo))})
        r = c.getresponse()
        dados = r.read()
        c.close()
        if r.status != 200:
            return None, f"http-{r.status}", time.monotonic() - t0
        return dados.decode("utf-8", "replace"), None, time.monotonic() - t0
    except Exception as e:
        return None, type(e).__name__, time.monotonic() - t0


_CLI_CARRY = None


def _cli_bin() -> str | None:
    p = os.environ.get("WHISPER_CLI")
    if p and Path(p).exists():
        return p
    padrao = Path(__file__).resolve().parent.parent / "whisper" / "Release" / "whisper-cli.exe"
    if padrao.exists():
        return str(padrao)
    return shutil.which("whisper-cli") or shutil.which("whisper-cli.exe")


def _cli_suporta_carry(binario: str) -> bool:
    """SONDA 1x/processo via --help. A licao de 21/07 ('flag pode nao existir')
    virou sonda, nao veto: sem a flag, derruba so o prompt (-mc 0) e preserva o
    anti-loop."""
    global _CLI_CARRY
    if _CLI_CARRY is not None:
        return _CLI_CARRY
    try:
        # o whisper-cli imprime o --help no stderr: `juntar_erro` traz ele.
        r = rodar([binario, "--help"], timeout=15, juntar_erro=True)
        _CLI_CARRY = "--carry-initial-prompt" in (r.stdout or "")
    except Exception:
        _CLI_CARRY = False
    if not _CLI_CARRY:
        log("cli sem --carry-initial-prompt — fallback CLI vai de -mc 0 "
            "com prompt ignorado (anti-loop preservado)")
    return _CLI_CARRY


def modelo() -> str:
    p = os.environ.get("WHISPER_MODELO")
    if p:
        return p
    return str(Path(__file__).resolve().parent.parent / "modelos" / "ggml-small-q5_1.bin")


def via_cli(wav: Path, initial_prompt: str = "", mc_prompt=None):
    """Fallback CLI. Devolve (texto|None, erro|None, segundos)."""
    t0 = time.monotonic()
    b = _cli_bin()
    if not b:
        return None, "sem-cli", 0.0
    # TXT UNICO por chamada: dois CLIs concorrentes nao cruzam texto.
    base = Path(tempfile.gettempdir()) / f"wispr-rec-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    cmd = [b, "-m", modelo(), "-f", str(wav), "-l", str(config.get("language", "pt")),
           "-nt", "-otxt", "-of", str(base)]
    mc = 0
    if initial_prompt and _cli_suporta_carry(b):
        cmd += ["--prompt", initial_prompt, "--carry-initial-prompt"]
        mc = max_context(initial_prompt) if mc_prompt is None else mc_prompt
    cmd += ["-mc", str(mc), "-bo", "5", "-bs", "5", "-sns"]
    try:
        tmo = float(config.get("whisper_cli_timeout_s", 600))
        r = rodar(cmd, timeout=tmo)
        txt = base.with_suffix(".txt")
        if r.returncode == 0 and txt.exists():
            s = txt.read_text(encoding="utf-8", errors="replace")
            try:
                txt.unlink()
            except OSError:
                pass
            return s, None, time.monotonic() - t0
        return None, f"rc-{r.returncode}", time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return None, "timeout", time.monotonic() - t0
    except Exception as e:
        return None, type(e).__name__, time.monotonic() - t0


def transcrever(wav: Path, segundos_audio: float, perfil: str = "livre"):
    """Caminho completo: servidor -> (se falhar) CLI.

    Devolve dict com texto, via, segundos e erro.
    """
    prompt, base = config.initial_prompt_efetivo(perfil)
    mc = max_context_efetivo(base, prompt[:len(prompt) - len(base)])
    texto, erro, dt = via_servidor(wav, segundos_audio, prompt, mc=mc)
    if texto is not None:
        log(f"transcricao: server ok ({dt:.2f}s, audio {segundos_audio:.1f}s, "
            f"{len(texto)}c)")
        return {"texto": texto, "via": "server", "segundos": dt, "erro": None}
    log(f"transcricao: server falhou ({erro}, {dt:.2f}s) — caindo pro CLI")
    texto, erro2, dt2 = via_cli(wav, prompt, mc_prompt=mc)
    if texto is not None:
        log(f"transcricao: cli ok ({dt2:.2f}s, {len(texto)}c)")
        return {"texto": texto, "via": "cli", "segundos": dt + dt2, "erro": None}
    log(f"transcricao: cli falhou ({erro2})")
    return {"texto": None, "via": "nenhuma", "segundos": dt + dt2,
            "erro": erro2 or erro}
