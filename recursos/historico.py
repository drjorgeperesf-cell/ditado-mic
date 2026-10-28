# -*- coding: utf-8 -*-
"""Historico: SQLite FTS5 (busca ignorando acento) + arquivos por dia.

O INSERT NAO PODE TRAVAR O DITADO: `timeout=0` + `busy_timeout=0` e retry em
background com 5 tentativas em (0.05, 0.15, 0.5, 1.5, 3.0) s. No Mac, com o
timeout padrao de 5 s do SQLite, um banco travado congelava a interface.
Os retries conferem geracao e texto original alem do rowid — impede associar a
um rowid reciclado depois de uma limpeza.

Retencao: `history_keep_days`. **`0` significa "nunca apagar"** — `int(x) or 30`
viraria 30 e APAGARIA.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
from log import log

RETRY_ESPERAS = (0.05, 0.15, 0.5, 1.5, 3.0)

# ESQUEMA IDENTICO AO DO MAC (app.py:1807-1833). `transcripts` E' a propria
# tabela FTS5, com as colunas de metadado marcadas UNINDEXED — nao e' uma
# tabela comum com um indice FTS ao lado.
# Isto NAO e' detalhe de gosto: o painel web (porte direto do `painel.py`) le
# `transcripts` com `MATCH` e pelos nomes `app_bundle` e `polish`. Com um
# esquema diferente, o painel degrada para "0 ditados" em silencio — o
# `except` dele engole o erro e nunca da 500. Alem disso, o `history.db` do
# Mac abre aqui como esta.
_ESQUEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts USING fts5(
    timestamp     UNINDEXED,
    text,
    app_bundle    UNINDEXED,
    profile       UNINDEXED,
    duration_s    UNINDEXED,
    transcribe_s  UNINDEXED,
    polish        UNINDEXED,
    wav_path      UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS textos_formatados (
    rowid_transcript INTEGER PRIMARY KEY,
    ts               REAL,
    texto            TEXT
);
"""


def _conectar(somente_leitura=False, timeout=0.0):
    p = config.caminho_banco()
    if somente_leitura:
        uri = f"file:{p.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=timeout)
        con.execute("PRAGMA query_only=ON")
        return con
    con = sqlite3.connect(str(p), timeout=timeout)
    con.execute("PRAGMA busy_timeout=0")
    con.execute("PRAGMA journal_mode=DELETE")
    return con


def criar():
    con = _conectar(timeout=5.0)
    try:
        con.executescript(_ESQUEMA)
        con.commit()
    finally:
        con.close()
    _restringir_acl(config.caminho_banco())


def _restringir_acl(p: Path):
    """NTFS nao tem umask: ACL explicita, so o usuario atual.
    (Analogo do chmod 0600 do Mac; o texto de ditado e' dado de paciente.)"""
    try:
        from proc import rodar
        usuario = os.environ.get("USERNAME") or ""
        if not usuario or not p.exists():
            return
        rodar(["icacls", str(p), "/inheritance:r",
               "/grant:r", f"{usuario}:(F)"], timeout=10)
    except Exception:
        pass


def inserir(texto, duracao_s=0.0, transcricao_s=0.0, perfil="livre", app="",
            geracao=None, ao_terminar=None):
    """Insere SEM bloquear o ditado. Devolve imediatamente; o rowid chega pelo
    callback (ou None se as 5 tentativas falharem)."""
    def _tentar():
        for i, espera in enumerate((0.0,) + RETRY_ESPERAS):
            if espera:
                time.sleep(espera)
            try:
                con = _conectar(timeout=0.0)
                try:
                    cur = con.execute(
                        "INSERT INTO transcripts(timestamp, text, app_bundle, "
                        "profile, duration_s, transcribe_s, polish, wav_path) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (time.time(), texto, app, perfil, float(duracao_s),
                         float(transcricao_s), "", ""))
                    con.commit()
                    rid = cur.lastrowid
                finally:
                    con.close()
                if ao_terminar:
                    ao_terminar(rid)
                return
            except sqlite3.Error:
                continue
        log(f"historico: insert falhou em {len(RETRY_ESPERAS) + 1} tentativas "
            f"({len(texto)}c)")
        if ao_terminar:
            ao_terminar(None)

    threading.Thread(target=_tentar, name="hist-insert", daemon=True).start()
    _arquivo_do_dia(texto, duracao_s, transcricao_s, perfil, app)


def _arquivo_do_dia(texto, duracao_s, transcricao_s, perfil, app):
    def _escrever():
        try:
            agora = datetime.now()
            d = config.dir_historico() / agora.strftime("%Y-%m-%d")
            d.mkdir(parents=True, exist_ok=True)
            base = d / agora.strftime("%H%M%S")
            base.with_suffix(".txt").write_text(texto, encoding="utf-8")
            base.with_suffix(".json").write_text(json.dumps({
                "ts": time.time(), "duration_s": duracao_s,
                "transcribe_s": transcricao_s, "profile": perfil, "app": app,
                "chars": len(texto)}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    threading.Thread(target=_escrever, name="hist-arquivo", daemon=True).start()


def gravar_formatado(rowid, texto, cru_esperado=None) -> bool:
    """Grava a versao formatada. Confere o texto original antes: impede
    associar a um rowid reciclado apos uma limpeza."""
    if not rowid or not texto:
        return False
    try:
        con = _conectar(timeout=2.0)
        try:
            if cru_esperado is not None:
                r = con.execute("SELECT text FROM transcripts WHERE rowid=?",
                                (rowid,)).fetchone()
                if not r or r[0] != cru_esperado:
                    log("formatado: rowid nao bate com o texto original — nao gravado")
                    return False
            con.execute("INSERT OR REPLACE INTO textos_formatados"
                        "(rowid_transcript, ts, texto) VALUES (?,?,?)",
                        (rowid, time.time(), texto))
            con.commit()
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


def ultimos(n=None):
    n = int(n or config.get("textos_limite", 200))
    try:
        con = _conectar(somente_leitura=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        linhas = con.execute(
            "SELECT t.rowid, t.timestamp, t.text, t.duration_s, t.app_bundle, "
            "f.texto FROM transcripts t LEFT JOIN textos_formatados f "
            "ON f.rowid_transcript = t.rowid ORDER BY t.rowid DESC LIMIT ?",
            (n,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"id": a, "ts": b, "texto": c, "duracao": d, "app": e,
             "formatado": f} for a, b, c, d, e, f in linhas]


def ultimo_ditado() -> str:
    """Texto do ultimo DITADO (perfis de ditado; nunca o original de uma
    selecao/instrucao). Usado pelo "colar o ultimo" depois de reiniciar."""
    try:
        con = _conectar(somente_leitura=True, timeout=2.0)
    except sqlite3.Error:
        return ""
    try:
        r = con.execute(
            "SELECT text FROM transcripts WHERE profile IN "
            "('livre','prontuario','casual') ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        con.close()
    return (r[0] or "") if r else ""


def sanitizar_busca(q: str, max_termos=32) -> str:
    """Converte texto humano em FTS5 SEMPRE valido.

    Cada palavra vira frase entre aspas, pontuacao interna vira separador
    ("pos-operatorio" -> "pos operatorio"), max 32 termos, `*` de prefixo so no
    ultimo termo e so com >=2 chars. Busca sem nada pesquisavel devolve ZERO
    resultados, nunca a lista inteira.
    """
    import re
    bruto = (q or "").strip()
    if not bruto:
        return ""
    termos = []
    for pedaco in re.split(r"\s+", bruto):
        palavras = [p for p in re.split(r"[^\wÀ-ÿ]+", pedaco) if p]
        if palavras:
            termos.append('"' + " ".join(palavras) + '"')
        if len(termos) >= max_termos:
            break
    if not termos:
        return ""
    ultimo = termos[-1]
    if len(ultimo.strip('"').replace(" ", "")) >= 2 and not bruto.endswith(" "):
        termos[-1] = ultimo + "*"
    return " ".join(termos)


def buscar(q, limite=30):
    fts = sanitizar_busca(q)
    if not fts:
        return []
    try:
        con = _conectar(somente_leitura=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        linhas = con.execute(
            "SELECT rowid, timestamp, text, app_bundle FROM transcripts "
            "WHERE transcripts MATCH ? ORDER BY rowid DESC LIMIT ?",
            (fts, int(limite))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"id": a, "ts": b, "texto": c, "app": d} for a, b, c, d in linhas]


# ════════════════════════════════════════════════════════════════════
# Limpar TUDO (botao Limpar)
def escopo_seguro(caminho: Path) -> bool:
    """TRAVA DE ESCOPO antes de qualquer remocao recursiva.

    Caminho REAL resolvido, DENTRO da pasta de dados do app, nunca a propria
    base, so FILHO DIRETO, nome no formato YYYY-MM-DD, symlink nunca seguido.
    Falhou = aborta, zero apagamento.
    """
    from log import base_dados
    try:
        base = (base_dados() / "history").resolve(strict=False)
        alvo = Path(caminho)
        if alvo.is_symlink():
            return False
        alvo = alvo.resolve(strict=False)
        if alvo == base:
            return False
        if alvo.parent != base:
            return False
        import re
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", alvo.name):
            return False
        return True
    except Exception:
        return False


def limpar_tudo(confirmado: bool):
    """Apaga banco e arquivos. SEM `confirmado=True` nada acontece.

    Falha do banco NAO cancela os arquivos (os audios sao o grosso dos MB) e a
    mensagem diz as duas coisas.
    """
    if confirmado is not True:
        return {"ok": False, "erro": "nao-confirmado", "banco": False,
                "arquivos": False}
    import shutil
    from log import base_dados
    banco = arquivos = False
    try:
        con = _conectar(timeout=5.0)
        try:
            con.execute("DELETE FROM transcripts")
            con.execute("DELETE FROM textos_formatados")
            con.commit()
            banco = True
        finally:
            con.close()
        try:
            con = _conectar(timeout=3.0)
            con.execute("VACUUM")
            con.close()
        except sqlite3.Error:
            pass                       # ocupado = pula em silencio
    except sqlite3.Error as e:
        log(f"limpar: banco falhou ({type(e).__name__})")
    d = base_dados() / "history"
    if d.exists():
        removidos = 0
        try:
            for filho in d.iterdir():        # iterdir, NUNCA rglob
                if not escopo_seguro(filho):
                    log(f"limpar: recusado por escopo ({filho.name})")
                    continue
                shutil.rmtree(filho, ignore_errors=True)
                removidos += 1
            arquivos = True
        except OSError as e:
            log(f"limpar: arquivos falharam ({type(e).__name__})")
        log(f"limpar: {removidos} dias removidos")
    else:
        arquivos = True
    return {"ok": banco and arquivos, "banco": banco, "arquivos": arquivos}


def aplicar_retencao():
    """`0` = NUNCA apagar. A varredura de orfaos roda SEMPRE, nao so quando
    houve delecao (senao nunca roda justo quando orfao e' mais provavel)."""
    try:
        dias = int(config.get("history_keep_days", 30))
    except (TypeError, ValueError):
        dias = 30
    if dias <= 0:
        _varrer_orfaos()
        return 0
    corte = time.time() - dias * 86400
    n = 0
    try:
        con = _conectar(timeout=3.0)
        try:
            cur = con.execute("DELETE FROM transcripts WHERE timestamp < ?", (corte,))
            n = cur.rowcount or 0
            con.execute("DELETE FROM textos_formatados WHERE ts < ?", (corte,))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass
    limite_dia = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    import shutil
    from log import base_dados
    d = base_dados() / "history"
    if d.exists():
        for filho in d.iterdir():
            if filho.name < limite_dia and escopo_seguro(filho):
                shutil.rmtree(filho, ignore_errors=True)
    _varrer_orfaos()
    return n


def _varrer_orfaos():
    try:
        con = _conectar(timeout=2.0)
        try:
            con.execute("DELETE FROM textos_formatados WHERE rowid_transcript "
                        "NOT IN (SELECT rowid FROM transcripts)")
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass
