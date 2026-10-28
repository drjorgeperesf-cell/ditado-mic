# -*- coding: utf-8 -*-
"""Higiene do texto transcrito (M2). Logica portada 1:1 de app.py:520-935.

Tres camadas de anti-alucinacao:
 1. blacklist literal (so casa o texto INTEIRO, nunca corta trecho do meio);
 2. credito de legendagem por padrao ESTRUTURAL ancorado no FIM (o NOME do
    legendador muda a cada alucinacao — lista literal nao resolve);
 3. gate de pico de audio: "Obrigado." com fala real pode ser ditado de
    verdade; credito de legendagem nunca e'.

A ANCORA DE FIM e' a trava principal contra falso positivo: existe um ditado
real em que o usuário FALA do bug ("...ele gera uma coisa que chama Legenda por
Sonia Huberty...") e ele precisa passar INTACTO.

Mais: anti-loop de repeticao (3+ copias consecutivas de frase de 4-16 palavras).

As constantes e a blacklist vem de `prompts.py`, extraidas por AST do app.py do
Mac — nunca redigitadas.
"""
from __future__ import annotations

import re
import unicodedata

import config
from log import log
from prompts import (CREDITO_LEGENDA_MAX_CORTE, WHISPER_HALLUCINATIONS)

# ── Credito de legendagem: regex VERBATIM do app.py:575-638 ──────────
_CRED_NOME_TOKEN = "[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’-]+"
_CRED_PARTICULA = "(?:d[aeou]s?|e|von|van|del|la|di)"
_CRED_NOME = (_CRED_NOME_TOKEN
              + "(?:\\s+(?:" + _CRED_PARTICULA + "\\s+)?" + _CRED_NOME_TOKEN + "){1,3}")

_CRED_CHAVE_LIVRE = "legendas?|legendado|legendada|legendados|legendagem"

_CRED_CHAVE_CONECTOR = (
    "revis(?:a|ã)o|revisado|revisada"
    "|transcri(?:c|ç)(?:a|ã)o|transcri(?:c|ç)(?:o|õ)es"
    "|tradu(?:c|ç)(?:a|ã)o|traduzido|traduzida|tradutor|tradutora"
    "|sincroniza(?:c|ç)(?:a|ã)o|sincronizado"
)

_CRED_PREFIXO = ("(?:(?:" + _CRED_CHAVE_LIVRE + "|" + _CRED_CHAVE_CONECTOR + ")"
                 "\\s*(?:,|/|&|\\s+e)\\s+)?")

_CRED_SITE = "[A-ZÀ-ÖØ-Þ][A-Za-z0-9À-ÖØ-öø-ÿ'’-]*(?:\\.[A-Za-z]{2,6})+"

RE_CREDITO_LEGENDA = re.compile(
    "(?:^|(?<=[\\s\"'(\\[—–-]))"
    "(?i:" + _CRED_PREFIXO +
    "(?:" + _CRED_CHAVE_LIVRE + ")"
    "\\s*(?:[:–—-]\\s*|\\s+(?:por|pela|pelo)\\s+|\\s+)"
    "(?:a\\s+comunidade\\s+|o\\s+canal\\s+|comunidade\\s+|canal\\s+)?"
    ")"
    "(?P<nome>" + _CRED_NOME + "|" + _CRED_SITE + ")"
    "[\\s.,;:!?…\"'\\)\\]—–-]*$",
    re.UNICODE,
)


def e_credito_de_legenda(text: str) -> bool:
    """True se o texto INTEIRO for um credito de legendagem alucinado."""
    if not text or not config.get("credito_legenda_filtro", True):
        return False
    m = RE_CREDITO_LEGENDA.search(text.strip(" .,!?;:-—…\"'"))
    return bool(m) and m.start() == 0


def podar_cauda_credito(text: str):
    """Remove credito GRUDADO NO FIM de um ditado legitimo.

    Conservadora de proposito (podar texto legitimo do usuário e' MUITO pior que
    deixar um credito passar): so sufixo, UMA passada, NUNCA esvazia, piso de
    `credito_legenda_min_palavras` restantes, corte maior que
    CREDITO_LEGENDA_MAX_CORTE nao corta.
    """
    if (not text
            or not config.get("credito_legenda_filtro", True)
            or not config.get("credito_legenda_podar_cauda", True)):
        return text, None
    m = RE_CREDITO_LEGENDA.search(text)
    if not m:
        return text, None
    cabeca = text[:m.start()].rstrip()
    if not cabeca.strip():
        return text, None
    try:
        min_pal = int(config.get("credito_legenda_min_palavras", 5))
    except (TypeError, ValueError):
        min_pal = 5
    if len(cabeca.split()) < min_pal:
        return text, None
    if len(text) - m.start() > CREDITO_LEGENDA_MAX_CORTE:
        return text, None
    return cabeca, m.group(0).strip()


# Alucinacoes tipicas do whisper em PT-BR que NAO estao no conjunto verbatim do
# Mac (prompts.py nao se edita — ha sha256). Comparadas SEM acento, texto INTEIRO,
# e so descartam com pico baixo (mesmo gate da blacklist original).
ALUCINACOES_EXTRA = frozenset([
    "inscreva-se no canal", "se inscreva no canal", "inscreva-se",
    "nao esqueca de se inscrever", "deixe seu like", "deixe o seu like",
    "obrigado por assistir", "obrigada por assistir", "obrigado por assistirem",
    "ate a proxima", "ate o proximo video", "ate o proximo", "ate mais",
    "tchau tchau", "legendas em portugues", "legenda em portugues",
    "aplausos", "risos", "silencio", "som de fundo", "musica de fundo",
    "musica tocando", "vozes ao fundo", "voz ao fundo",
    "subtitles by the amara.org community", "sous-titres",
    "thank you for watching", "thanks for watching", "you", "bye",
    "obrigado a todos", "obrigada a todos", "e isso ai",
])

# Anotacao de som inteira, tipo "[Musica]" / "(aplausos)" / "[ risos ]".
_RE_ANOTACAO_SOM = re.compile(r"^[\[\(\*♪\s][^\]\)]{0,40}[\]\)\*♪\s]*$")

# Palavras-rotulo do initial_prompt: so elas "assinam" um eco do prompt.
_ROTULOS_PROMPT = frozenset([
    "transcricao", "portugues", "brasileiro", "siglas", "nomes", "proprios",
    "termos", "prontuario"])


def _norm_palavras(s: str) -> list[str]:
    s = _sem_acento(s or "").lower()
    return re.findall(r"[a-z0-9]+", s)


def _prompts_conhecidos() -> list[str]:
    """Todos os initial_prompt possiveis (topo + perfis), cada um tambem na
    versao ENVIADA (com o dicionario pessoal na frente): o eco do trecho do
    dicionario ("Termos: enoxaparina, ...") tem que cair igual."""
    out = []
    topo = config.get("initial_prompt", "")
    if isinstance(topo, str) and topo:
        out.append(topo)
    perfis = config.get("profiles") or {}
    if isinstance(perfis, dict):
        for p in perfis.values():
            v = p.get("initial_prompt") if isinstance(p, dict) else None
            if isinstance(v, str) and v:
                out.append(v)
    for base in list(out):
        try:
            extra = config.parte_dicionario(base)
        except Exception:
            extra = ""
        if extra:
            out.append(extra + base)
    return out


def e_eco_de_termos(text: str) -> bool:
    """True se o texto e' SO termos do dicionario pessoal ("Enoxaparina.
    Ceftriaxona."), sem nenhuma outra palavra. Com o dicionario no prompt, e'
    o eco tipico de audio quase sem fala. Quem chama so usa na ZONA CINZA
    (pouquissima fala medida): ditar "enoxaparina" de verdade passa."""
    if not text or not config.get("eco_prompt_filtro", True):
        return False
    pal = _norm_palavras(text)
    if not pal:
        return False
    try:
        termos = config.termos_dicionario() if config.get(
            "dicionario_ativo", True) else []
    except Exception:
        termos = []
    vocab = set()
    for t in termos:
        vocab.update(_norm_palavras(t))
    return bool(vocab) and all(p in vocab for p in pal)


def e_eco_do_prompt(text: str) -> bool:
    """True se o texto e' o whisper DEVOLVENDO pedaco do initial_prompt.

    Caso real (08/10/2026, tres vezes): "Nomes proprio." colado no lugar do
    ditado — eco do trecho "Nomes proprios: Whipple ...". Regra estreita pra
    nunca comer ditado legitimo ("clinica medica" e' do prompt mas NAO tem
    palavra-rotulo): exige >= 2 palavras, sequencia CONTIGUA do prompt (com
    tolerancia de sufixo de 1 letra) e pelo menos uma palavra-rotulo.
    """
    if not text or not config.get("eco_prompt_filtro", True):
        return False
    pal = _norm_palavras(text)
    if len(pal) < 2 or len(pal) > 14:
        return False
    if not any(p in _ROTULOS_PROMPT or p + "s" in _ROTULOS_PROMPT for p in pal):
        return False

    def casa(a, b):
        return a == b or (len(a) >= 4 and (a.startswith(b) or b.startswith(a))
                          and abs(len(a) - len(b)) <= 1)

    for prompt in _prompts_conhecidos():
        base = _norm_palavras(prompt)
        for i in range(len(base) - len(pal) + 1):
            if all(casa(pal[k], base[i + k]) for k in range(len(pal))):
                return True
    return False


def e_alucinacao(text: str) -> bool:
    """True se o texto INTEIRO (normalizado) for alucinacao tipica do whisper."""
    if not text:
        return False
    norm = text.lower().strip(" .,!?;:-—…\"'")
    if not norm:
        return True
    if norm in WHISPER_HALLUCINATIONS:
        return True
    sem = _sem_acento(norm)
    if sem in ALUCINACOES_EXTRA:
        return True
    if len(norm.split()) <= 5 and _RE_ANOTACAO_SOM.match(text.strip()) \
            and text.strip()[0] in "[(*♪":
        return True
    for extra in (config.get("hallucination_blacklist") or []):
        if norm == str(extra).lower().strip(" .,!?;:-—…\"'"):
            return True
    return e_credito_de_legenda(text)


def colapsar_repeticoes(text: str) -> str:
    """Colapsa 3+ repeticoes CONSECUTIVAS da mesma frase (4-16 palavras) numa so.

    Incidente 23/07/2026: ditado de 237 s voltou com a mesma frase 24x e as
    questoes 16-22 engolidas. Enfase legitima ("muito, muito") e repeticao 2x
    ficam intactas.
    """
    if not text or not config.get("anti_loop_collapse", True):
        return text
    palavras = text.split(" ")
    n_tot = len(palavras)
    if n_tot < 12:
        return text
    chave = [p.lower().strip(",.;:!?…\"'—–-") for p in palavras]
    saida = []
    copias_cortadas = 0
    i = 0
    while i < n_tot:
        rep_n = rep_k = 0
        for n in range(min(16, (n_tot - i) // 3), 3, -1):
            if chave[i:i + n] != chave[i + n:i + 2 * n]:
                continue
            k = 2
            while chave[i + k * n:i + (k + 1) * n] == chave[i:i + n]:
                k += 1
            if k >= 3:
                rep_n, rep_k = n, k
                break
        if rep_n:
            saida.extend(palavras[i:i + rep_n])
            i += rep_n * rep_k
            copias_cortadas += rep_k - 1
        else:
            saida.append(palavras[i])
            i += 1
    if copias_cortadas:
        log(f"anti-loop: frase repetida colapsada ({copias_cortadas} copias "
            f"removidas) — decoder loopou")
        return " ".join(saida)
    return text


def normalizar(text: str) -> str:
    """Junta os segmentos do whisper-server num paragrafo unico.

    "\\n" seguido de NAO-espaco  -> corte mid-word -> junta SEM espaco
    "\\n" seguido de espaco/borda -> fronteira normal -> vira 1 espaco
    """
    if not text:
        return text
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    if not config.get("collapse_segments", True):
        return colapsar_repeticoes(t.strip())
    t = re.sub(r"\n(?=\S)", "", t)
    t = re.sub(r"[ \t]*\n[ \t]*", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" +([,.;:!?])", r"\1", t)
    return colapsar_repeticoes(t.strip())


# ════════════════════════════════════════════════════════════════════
def higienizar(bruto: str, pico: float, fala_s=None):
    """Pipeline completo do pos-processamento (app.py:8236-8344).

    Devolve (texto, motivo_do_descarte|None, aviso|None).
    `pico` e' o pico de amplitude (0..1) da gravacao — o GATE que separa
    alucinacao de ditado intencional. `fala_s` (opcional) e' quanto da
    gravacao tinha energia de fala (silencio.py); None = nao medido.
    """
    t = normalizar(bruto or "")
    if not t:
        return "", "vazio", None
    limiar = float(config.get("hallucination_peak_max", 0.12))
    try:
        pico = float(pico)
    except (TypeError, ValueError):
        pico = 0.0

    # Zona cinza de energia (silencio.py): quase sem fala = trata como pico
    # baixo, entao toda alucinacao conhecida cai mesmo que um estalo tenha dado
    # pico alto.
    try:
        marginal = float(config.get("silencio_fala_marginal_s", 0.30))
    except (TypeError, ValueError):
        marginal = 0.30
    if fala_s is not None and fala_s < marginal:
        pico = min(pico, limiar / 2)

    # Eco do initial_prompt ("Nomes proprio."): so com pouca fala/pico, pra
    # nunca comer ditado de verdade que por acaso repita uma sigla do prompt.
    if e_eco_do_prompt(t) and (pico < 0.35 or (fala_s is not None and fala_s < 1.0)):
        return "", "eco-prompt", None
    # Eco do DICIONARIO pessoal (so termos dele): so na zona cinza de energia.
    if fala_s is not None and fala_s < marginal and e_eco_de_termos(t):
        return "", "eco-dicionario", None

    if e_alucinacao(t):
        credito = e_credito_de_legenda(t)
        # EXCECAO: credito de legendagem com fala real e' descartado mesmo
        # assim (colar nao recupera nada).
        if credito and config.get("credito_legenda_descarta_com_fala", True):
            return "", "credito-legenda", "Nada transcrito (credito de legenda)."
        if pico < limiar:
            return "", "alucinacao", None
        # Com fala real, "Obrigado." pode ser ditado intencional: passa.

    # "E ai Legenda por Sonia Ruberti" (08/10/2026): cabeca curta demais pra
    # poda (< min palavras), mas com pouquissima fala o resto e' lixo. So age
    # quando a energia de fala FOI medida e e' curta.
    if fala_s is not None and config.get("credito_legenda_filtro", True):
        m = RE_CREDITO_LEGENDA.search(t)
        try:
            lim_s = float(config.get("credito_curto_descarta_s", 4.0))
        except (TypeError, ValueError):
            lim_s = 4.0
        if m and fala_s < lim_s and len(t[:m.start()].split()) <= 3:
            return "", "credito-legenda", "Nada transcrito (credito de legenda)."

    podado, cauda = podar_cauda_credito(t)
    if cauda:
        log(f"credito-legenda: cauda podada ({len(t)}c -> {len(podado)}c)")
        t = podado
    return t, None, None


# ════════════════════════════════════════════════════════════════════
# Vocabulario medico (HOJE DESLIGADO — texto CRU e' o default)
def aplicar_vocab_medico(texto: str, vocab: dict | None) -> str:
    """37 correcoes regex + termos PT-BR com fuzzy Levenshtein.

    So roda com `medical_vocab_enabled` LIGADO — que e' False no codigo, por
    decisao do usuário em 10/07/2026 ("texto CRU, sem filtro").
    Plural correto nao e' singularizado; distancia 2 so em palavras >=9 letras.
    """
    if not texto or not config.get("medical_vocab_enabled", False) or not vocab:
        return texto
    for regra in (vocab.get("regex") or []):
        try:
            texto = re.sub(regra["de"], regra["para"], texto, flags=re.IGNORECASE)
        except (KeyError, re.error):
            continue
    termos = vocab.get("termos") or []
    if not termos:
        return texto
    try:
        dmax = int(config.get("medical_vocab_fuzzy_d", 1))
        lmin = int(config.get("medical_vocab_min_len", 7))
    except (TypeError, ValueError):
        dmax, lmin = 1, 7
    canon = {_sem_acento(t).lower(): t for t in termos}

    def troca(m):
        p = m.group(0)
        if len(p) < lmin:
            return p
        k = _sem_acento(p).lower()
        if k in canon:
            return p if p == canon[k] else canon[k]
        if k.endswith("s") and k[:-1] in canon:
            return p          # plural correto NAO e' singularizado
        d_ok = 2 if len(p) >= 9 else min(dmax, 1)
        melhor, melhor_d = None, d_ok + 1
        for ck, cv in canon.items():
            if abs(len(ck) - len(k)) > d_ok:
                continue
            d = _levenshtein(k, ck, d_ok)
            if d < melhor_d:
                melhor, melhor_d = cv, d
        return melhor if melhor and melhor_d <= d_ok else p

    return re.sub(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", troca, texto)


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


def _levenshtein(a: str, b: str, teto: int) -> int:
    if abs(len(a) - len(b)) > teto:
        return teto + 1
    ant = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(ant[j] + 1, cur[j - 1] + 1, ant[j - 1] + (ca != cb)))
        if min(cur) > teto:
            return teto + 1
        ant = cur
    return ant[-1]


# ════════════════════════════════════════════════════════════════════
# App-aware (HOJE DESLIGADO — texto CRU e' o default)
REGRAS_POR_APP = {
    # terminais e editores: nunca ponto final (quebra comando)
    "windowsterminal.exe": ["no_period"], "cmd.exe": ["no_period"],
    "powershell.exe": ["no_period"], "pwsh.exe": ["no_period"],
    "wt.exe": ["no_period"], "code.exe": ["no_period"],
    "cursor.exe": ["no_period"], "sublime_text.exe": ["no_period"],
    # e-mail e notas: frase com maiuscula e ponto
    "outlook.exe": ["capitalize_sentences", "add_period"],
    "olk.exe": ["capitalize_sentences", "add_period"],
    "notepad.exe": ["capitalize_sentences", "add_period"],
    # mensageiros e estudo: so maiuscula
    "obsidian.exe": ["capitalize_sentences"], "anki.exe": ["capitalize_sentences"],
    "whatsapp.exe": ["capitalize_sentences"], "telegram.exe": ["capitalize_sentences"],
}
REGRA_PADRAO_APP = ["capitalize_sentences"]

# Abreviacoes de via: "tomar 10mg v.o. de manha" NAO vira "...v.o. De manha".
_VIAS = ("v.o", "e.v", "i.m", "s.c", "s.l", "v.r")
_RE_VIAS = re.compile(r"\b(?:" + "|".join(v.replace(".", r"\.") for v in _VIAS)
                      + r")\.?", re.IGNORECASE)


def aplicar_app_aware(texto: str, exe: str) -> str:
    if not texto or not config.get("app_aware_enabled", False):
        return texto
    regras = (config.get("app_rules") or REGRAS_POR_APP).get(
        (exe or "").lower(), REGRA_PADRAO_APP)
    t = texto
    if "capitalize_sentences" in regras:
        t = _capitalizar_frases(t)
    if "add_period" in regras and t and t[-1] not in ".!?:;":
        t += "."
    if "no_period" in regras:
        t = t.rstrip(".")
    return t


def _capitalizar_frases(t: str) -> str:
    """Capitaliza inicio de frase, ignorando os pontos DE DENTRO de uma
    abreviacao de via ("v.o.", "e.v.") — senao "tomar 10mg v.o. de manha"
    virava "...v.O. De manha"."""
    pontos_da_via = set()
    for m in _RE_VIAS.finditer(t):
        for j in range(m.start(), m.end()):
            if t[j] == ".":
                pontos_da_via.add(j)
    saida, capitalizar = [], True
    for i, c in enumerate(t):
        if capitalizar and c.isalpha():
            saida.append(c.upper())
            capitalizar = False
        else:
            saida.append(c)
        if c in ".!?" and i not in pontos_da_via:
            capitalizar = True
    return "".join(saida)


# ════════════════════════════════════════════════════════════════════
# Pos-processamento do texto FORMATADO (≡) — recursos do Wispr Flow.
# NUNCA roda no texto colado (veto 14: o colado e' CRU). Deterministico, sem
# IA: o prompt do formatado (prompts.py) tem sha256 e nao se edita.

# Muletas SEM sentido em portugues. "é" sozinho NAO entra (e' verbo: "o
# diagnostico é... pneumonia"); so "éé", "hum", "hmm", "ahn", "hã".
_MULETA = r"(?:h+u+m+|h+m+|a+h+n+|h+ã+|ã+h+|éé+)"
_LETRA = r"[^\W_]"
_RE_MULETA = re.compile(
    r"(?<!" + _LETRA + r")(" + _MULETA + r")(?!" + _LETRA + r")"
    r"(?:[ \t]*(?:\.\.\.|…|,|\.))?[ \t]*", re.IGNORECASE)
_MARCA_MAIUSC = "\u0001"

# Backtrack SO de numero: "5, quer dizer, 10 mg" -> "10 mg". Palavra solta
# ("segunda, quer dizer, terca") fica: o alcance da correcao e' ambiguo.
_NUM = r"\d+(?:[.,]\d+)?"
_UNID = (r"(?:[ \t]*(?:mg|mcg|µg|g|kg|ml|l|ui|u|%|mmhg|bpm|cm|mm|meq|"
         r"mmol|h|horas?|dias?|anos?|meses|m[êe]s|semanas?|vezes|gotas))?")
_MARCADOR = (r"[ \t]*,?[ \t]*(?:n[ãa]o,?[ \t]+)?(?:quer dizer|ou melhor|digo|"
             r"ali[áa]s|corrigindo)[ \t]*[,:]?[ \t]*")
_RE_BACKTRACK_NUM = re.compile(
    r"(?<![\w.,])(" + _NUM + r")(" + _UNID + r")(?!" + _LETRA + r")" + _MARCADOR
    + r"(" + _NUM + r")(" + _UNID + r")(?!" + _LETRA + r")", re.IGNORECASE)


def tirar_muletas(t: str) -> tuple[str, int]:
    """Tira "hum", "ahn", "éé"... e conserta maiuscula/espaco. (texto, n)."""
    n = 0

    def troca(m):
        nonlocal n
        n += 1
        return _MARCA_MAIUSC if m.group(1)[:1].isupper() else ""

    linhas = []
    for linha in t.split("\n"):
        nova = _RE_MULETA.sub(troca, linha)
        if nova == linha:
            linhas.append(linha)
            continue
        # muleta que abria frase: a proxima letra herda a maiuscula
        nova = re.sub(_MARCA_MAIUSC + r"\s*(\S)", lambda m: m.group(1).upper(), nova)
        nova = nova.replace(_MARCA_MAIUSC, "")
        nova = re.sub(r"(?<=\S)[ \t]{2,}", " ", nova)
        nova = re.sub(r"[ \t]+([,.;:!?])", r"\1", nova)
        nova = re.sub(r"([,;])[ \t]*[,;]", r"\1", nova)
        nova = re.sub(r"^([ \t]*(?:-|\d+\.)[ \t]+)[,.][ \t]*", r"\1", nova)
        nova = re.sub(r"^([ \t]*(?:-|\d+\.)[ \t]+)(\S)",
                      lambda m: m.group(1) + m.group(2).upper(), nova) \
            if re.match(r"^[ \t]*(?:-|\d+\.)[ \t]+[a-zà-ÿ]", linha) is None else nova
        linhas.append(nova.rstrip())
    return "\n".join(linhas), n


def aplicar_backtrack(t: str) -> tuple[str, int]:
    """"dose de 5 mg, quer dizer, 10 mg" -> "dose de 10 mg". (texto, n)."""
    n = 0

    def troca(m):
        nonlocal n
        n += 1
        unid = m.group(4) if m.group(4).strip() else m.group(2)
        return m.group(3) + unid

    anterior = None
    while anterior != t:                      # "5, digo 6, digo 7" -> "7"
        anterior = t
        t = _RE_BACKTRACK_NUM.sub(troca, t)
    return t, n


def limpar_formatado(t: str) -> str:
    """Aplicado SO ao texto formatado (≡). Log so com contagens."""
    if not t:
        return t
    n_b = n_m = 0
    if config.get("formatado_backtrack", True):
        t, n_b = aplicar_backtrack(t)
    if config.get("formatado_tirar_muletas", True):
        t, n_m = tirar_muletas(t)
    if n_b or n_m:
        log(f"formatado: limpeza deterministica (backtrack={n_b}, muletas={n_m})")
    return t

