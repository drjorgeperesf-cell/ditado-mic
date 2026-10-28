# -*- coding: utf-8 -*-
"""Configuracao do Microfone.

DOIS PRINCIPIOS QUE VIERAM DE INCIDENTES (inventario 6.9):

1. **Todo default vive AQUI, no codigo.** Se o "false" de um comportamento
   vetado morasse so no `config.json`, um DESINSTALAR+INSTALAR ressuscitaria o
   comportamento (classe do bug do `vad_enabled`, repetida 5x no Mac).
   `config.exemplo.json` repete os mesmos valores, mas e' comentario: a verdade
   e' `PADROES` abaixo. `testes/teste_defaults_vetados.py` carrega o app SEM
   config.json nenhum e afirma cada default vetado desligado.
2. **Chave PRESENTE com valor vazio != chave AUSENTE.** `get(k, fallback)` so
   cai no fallback quando a chave nao existe — foi isso que matou o dicionario
   medico por 3 semanas. Por isso `perfil_valor()` usa `in`, nao `get`.

E coercao de tipo e' obrigatoria: `"livre": {"initial_prompt": ["lista"]}`
derrubava a transcricao inteira (AttributeError no `.strip()` -> ditado
perdido); `float(True) == 1.0` passava no branch float.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from log import base_dados, log

# ════════════════════════════════════════════════════════════════════
# PADROES — a fonte da verdade. Nada aqui pode depender do config.json.
PADROES: dict = {
    # ── teclas ───────────────────────────────────────────────────────
    # DECISAO EXPLICITA DO USUÁRIO, 20/09/2026: "eu quero o alt direito pra
    # puxar o mic e o ctrl direito pra formatar". Isso SUBSTITUI a proibicao
    # do Alt direito que estava no inventario.
    # O default mora AQUI, no codigo (e no config.exemplo.json): se morasse so
    # no config.json, um DESINSTALAR+INSTALAR devolveria o app sem tecla.
    "hotkey_ditar_vk": 0xA5,            # VK_RMENU = Alt DIREITO (= AltGr)
    "hotkey_ditar_nome": "Alt direito",
    "hotkey_ditar_extended": True,
    "hotkey_ditar_suprimir": False,     # NUNCA engolir: AltGr+tecla tem que passar
    "hotkey_formatar_vk": 0xA3,         # VK_RCONTROL — Control DIREITO, igual ao Mac
    "hotkey_formatar_nome": "Ctrl direito",
    "hotkey_mode": "toggle",            # "toggle" | "hold" (push-to-talk)
    "hotkey_debounce_s": 0.35,          # RCMD_DEBOUNCE_S do Mac

    # O Alt direito E' o AltGr. Esta chave e' o "sim, eu sei" dele: com ela
    # ligada o app aceita a tecla sem reclamar no boot, e o seletor manual
    # (CONFIGURAR-TECLA.bat) continua RECUSANDO AltGr para quem nao sabe.
    "tecla_ditar_altgr_autorizado": True,
    # "Alt direito SOZINHO dita": outra tecla apertada dentro desta janela,
    # com o Alt segurado, e' AltGr+tecla -> cancela o ditado EM SILENCIO.
    "altgr_janela_combinacao_s": 0.35,
    # Injeta uma tecla neutra no keydown para o Alt nunca abrir a barra de
    # menus ao soltar ("Alt poison"). Se algum acento parar de funcionar,
    # este e' o primeiro knob a desligar.
    "altgr_quebrar_menu": True,

    # ── transcricao ──────────────────────────────────────────────────
    "server_host": "127.0.0.1",
    "server_port": 7777,
    "language": "pt",
    # Dica de vocabulario para o whisper (nunca troca palavras do texto).
    # Ponha aqui nomes e siglas que voce usa. O dicionario.txt faz o mesmo.
    "initial_prompt": "Transcrição em português do Brasil, com pontuação.",
    # O perfil "livre" NAO declara initial_prompt DE PROPOSITO (3o incidente da
    # mesma classe): com a chave presente e vazia, o top-level nunca chegava.
    "profiles": {
        "livre": {},
        "casual": {"initial_prompt": ""},
    },
    "whisper_max_context_prompt": 117,   # calibrado no Mac (teto exato)
    "whisper_max_context_prompt_hash":
        "cf992ca8e210ab8a2b285403590f851525075fa104a7e3cd4add6786a75270cc",
    "server_timeout_base_s": 25.0,
    "server_timeout_fator": 0.5,
    "server_request_timeout": 300,
    "whisper_cli_timeout_s": 600,
    "transcricao_lenta_aviso_s": 60.0,
    "transcricao_lenta_fator": 0.35,
    "no_vad_retry_min_peak": 0.08,

    # ── VETADO: --vad no servidor. NAO RELIGAR (veto 4). ──────────────
    "vad_enabled": False,

    # ── higiene do texto ─────────────────────────────────────────────
    "collapse_segments": True,
    "anti_loop_collapse": True,
    "credito_legenda_filtro": True,
    "credito_legenda_podar_cauda": True,
    "credito_legenda_min_palavras": 5,
    "credito_legenda_descarta_com_fala": True,
    "hallucination_peak_max": 0.12,
    "hallucination_blacklist": [],
    # Portao de energia ANTES do whisper (silencio.py). Silencio puro virou
    # "Legendas por <nome>" com no_speech_prob=4e-10: so energia pega isso.
    "silencio_gate_ativo": True,
    "silencio_pico_min": 0.008,        # pico abaixo disto = mic mudo, nem transcreve
    "silencio_fala_min_s": 0.04,       # menos de 40 ms com energia = sem fala (nem transcreve)
    "silencio_fala_marginal_s": 0.30,  # 40-300 ms: transcreve, mas texto passa pelo filtro RIGIDO
    "silencio_limiar_rms": 0.008,      # fala real: p50 ~0,026; ruido de sala <=0,0005
    "silencio_compactar": True,        # encurta silencio longo (onde o whisper inventa)
    "silencio_run_min_s": 3.0,         # so corta trecho sem fala maior que isto
    "silencio_margem_s": 0.5,          # deixa isto de cada lado do corte
    "eco_prompt_filtro": True,         # descarta "Nomes proprio." (eco do initial_prompt)
    "credito_curto_descarta_s": 4.0,   # "E ai Legenda por X" com < 4 s de fala = lixo
    "audio_curto_min_s": 0.35,
    "failed_wav_preserva_min_s": 120.0,

    # ── texto CRU e' o default (usuário desligou os dois em 10/07/2026) ─
    "medical_vocab_enabled": False,
    "medical_vocab_fuzzy_d": 1,
    "medical_vocab_min_len": 7,
    "app_aware_enabled": False,

    # ── colagem ──────────────────────────────────────────────────────
    "paste_restore_clipboard": True,
    "paste_restore_delay_s": 0.6,
    "paste_restore_delay_slow_s": 1.2,
    # Apps que processam o Ctrl+V DEPOIS do restore (Electron). No Mac:
    # com.anthropic.claude. Aqui, por nome de executavel, minusculo.
    "slow_paste_exes": ["claude.exe", "slack.exe", "discord.exe",
                        "code.exe", "cursor.exe", "obsidian.exe",
                        "whatsapp.exe", "teams.exe", "chatgpt.exe",
                        "chrome.exe", "msedge.exe", "firefox.exe",
                        "brave.exe", "opera.exe", "comet.exe"],
    "recheck_gen_before_paste": True,
    "warn_focus_changed": True,

    # ── formatado tardio ─────────────────────────────────────────────
    "formatado_tardio": True,
    "formatado_provedor": "claude",
    "formatado_modelo": "claude-haiku-5-5",
    "formatado_claude_esforco": "medium",
    "formatado_timeout_s": 180,
    "formatado_worker_ocioso_s": 3600,

    # ── formatar selecao ─────────────────────────────────────────────
    "formatar_selecao": True,
    "selecao_substituir": True,
    "selecao_min_chars": 20,
    "selecao_max_chars": 12000,
    "selecao_atalho_traz_painel": True,
    # ── VETADO (veto 1): botao flutuante ao selecionar texto. ─────────
    #    "seleciono e clico manual". NAO RELIGAR.
    "selecao_botao_flutuante": False,

    # ── corretor PT (mesmo processo aqui; no Mac era app irmao) ───────
    "corretor_enabled": True,
    "corretor_modelo": "sonnet",
    "corretor_timeout_s": 20,
    "corretor_max_chars": 800,
    "corretor_idle_reset_s": 300,

    # ── pilula / janela ──────────────────────────────────────────────
    "sempre_na_frente": True,
    "visibilidade_intervalo_s": 3.0,
    "pular_pra_tela_do_cursor": True,
    "mic_mudo_alerta_s": 4.0,
    "mic_mudo_peak_min": 0.02,

    # ── historico ────────────────────────────────────────────────────
    "history_save_audio": False,
    "history_keep_days": 30,       # 0 = NUNCA apagar (cuidado com `int(x) or 30`)
    "textos_limite": 200,

    # ── microfone ────────────────────────────────────────────────────
    "manual_mic_nome": "",         # escolha manual = CHAVE do aparelho (captura.chave_mic);
                                   # vazio = automatico (mic_preferido_nome ou o padrao do Windows)
    "mic_preferido_nome": "",      # preferencia do PC; nao grava indices USB
    "mic_usb_generico_permitido": False,
    "mic_abertura_timeout_s": 4.0,
    "evitar_mic_bluetooth": True,  # BT forca HFP e destroi o audio

    # ── painel web ───────────────────────────────────────────────────
    "painel_porta": 7910,
    "painel_ocioso_s": 1200,

    # ── ciclo de vida ────────────────────────────────────────────────
    "keepwarm_intervalo_s": 150,
    "guardiao_intervalo_s": 120,

    # ── recursos copiados do Wispr Flow (27/10/2026) ─────────────────
    # Atalho global "colar o ultimo ditado de novo" (paste_last_text do
    # Wispr). RegisterHotKey, sem gancho novo e sem engolir tecla. Formato
    # "win+alt+v": exige Win ou Ctrl; Ctrl+Alt sem Win e' RECUSADO (Ctrl+Alt
    # = AltGr: roubaria os simbolos do ABNT2). Cola so depois que ele SOLTA
    # os modificadores (Win solto pela metade abre o menu Iniciar).
    "colar_ultimo_ativo": True,
    "colar_ultimo_atalho": "win+alt+v",
    "atalho_espera_soltar_s": 3.0,
    # Dicionario pessoal: VIES do whisper (initial_prompt), nunca troca o
    # texto. Arquivo: <dir_config>/dicionario.txt, uma palavra por linha.
    # Teto em caracteres (o whisper.cpp aceita ~223 tokens de prompt e o
    # prompt base ja usa ~116). Estimativa de tokens PESSIMISTA de proposito
    # (4 caracteres/token): subestimar so corta os 1os termos do dicionario;
    # superestimar ligaria o contexto rolante (risco de loop).
    "dicionario_ativo": True,
    "dicionario_max_chars": 300,
    "dicionario_chars_por_token": 4.0,
    # Ganho digital para fala baixa / sussurro: so DEPOIS do portao de
    # silencio (que continua medindo o audio ORIGINAL) e so na copia enviada
    # ao whisper. Pico abaixo de `ganho_pico_abaixo` sobe ate `ganho_pico_alvo`
    # (sem clipar), no maximo `ganho_max` vezes.
    "ganho_fala_baixa_ativo": True,
    "ganho_pico_abaixo": 0.25,
    "ganho_pico_alvo": 0.5,
    "ganho_max": 6.0,
    # Pos-processamento deterministico SO do texto formatado (≡). O colado
    # continua CRU (veto 14). Backtrack so de NUMERO ("5, quer dizer, 10 mg").
    "formatado_backtrack": True,
    "formatado_tirar_muletas": True,
    # Instrucao falada sobre a selecao (Command Mode do Wispr): seleciona,
    # aperta o atalho, fala a instrucao, aperta de novo (ou Alt direito).
    # So CLI `claude` (veto 10); sucesso MUDO (veto 2); sem botao (veto 1).
    "comando_voz_ativo": True,
    "comando_voz_atalho": "win+alt+c",
    "comando_voz_modelo": "claude-haiku-5-5",
    "comando_voz_timeout_s": 90,

    # ── FORA DA V1 (no Mac ja estavam desligados; nao implementados) ──
    "streaming_enabled": False,      # streaming ao vivo — nao implementado
    "matar_drive_ao_gravar": False,  # "matar Drive" — nao implementado aqui
    "polish_enabled": False,         # polish sincrono legado
    "snippets_enabled": False,       # snippets — nunca foram usados de verdade
}

# Chaves cujo `False`/valor e' VETO do dono. O teste de defaults vetados
# le ESTA lista — mexer aqui e' mexer num veto.
VETADOS_DESLIGADOS = {
    "vad_enabled": False,             # veto 4  — --vad banido
    "selecao_botao_flutuante": False, # veto 1  — botao flutuante ao selecionar
    "streaming_enabled": False,       # fora da v1
    "medical_vocab_enabled": False,   # texto CRU e' o default
    "app_aware_enabled": False,       # texto CRU e' o default
    "polish_enabled": False,          # legado
    "snippets_enabled": False,        # nunca usado
    "matar_drive_ao_gravar": False,   # fora da v1
    "hotkey_formatar_vk": 0xA3,       # Control DIREITO
}

# Teclas PROIBIDAS no SELETOR MANUAL (CONFIGURAR-TECLA.bat).
# O Alt direito continua aqui de propósito: ele é o AltGr, e quem abre o
# seletor sem saber disso precisa ser avisado. O caminho AUTORIZADO é outro —
# `tecla_ditar_altgr_autorizado` — e é decisão explícita do usuário.
VK_PROIBIDOS = {
    0xA5: ("Alt direito e' o AltGr. Dá pra usar mesmo assim (é o que o usuário "
           "escolheu), mas só pelo caminho autorizado: "
           "CONFIGURAR-TECLA.bat --alt-direito"),
    0xA4: "Alt esquerdo e' modificador de menu de todo app",
    0x10: "Shift", 0xA0: "Shift esquerdo", 0xA1: "Shift direito",
    0x11: "Control", 0xA2: "Control esquerdo",
    0xA3: "Control direito ja' e' o botao de FORMATAR",
    0x12: "Alt", 0x1B: "Esc", 0x08: "Backspace", 0x0D: "Enter",
    0x09: "Tab", 0x20: "Espaco", 0x14: "Caps Lock",
}

# Sugestoes do seletor, em ordem
VK_SUGERIDOS = [
    (0x5D, "Tecla Menu (aplicacao)"),
    (0x5C, "Windows direito"),
    (0x7A, "F11"), (0x7B, "F12"),
    (0x76, "F7"), (0x77, "F8"),
    (0x91, "Scroll Lock"), (0x13, "Pause"),
]

_LOCK = threading.Lock()
_CACHE: dict | None = None


def dir_config() -> Path:
    r = os.environ.get("MICROFONE_CONFIG_DIR")
    if r:
        return Path(r)
    return Path.home() / ".config" / "ditado-mic"


def caminho_config() -> Path:
    return dir_config() / "config.json"


def _merge_profundo(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_profundo(out[k], v)
        else:
            out[k] = v
    return out


def _coagir(cfg: dict) -> dict:
    """Coercao de tipo obrigatoria (licao 6.9).

    Valor do tipo errado no config NAO pode derrubar transcricao: cai pro
    default e loga. `float(True) == 1.0` nao e' um float valido aqui.
    """
    out = dict(cfg)
    for k, padrao in PADROES.items():
        if k not in out:
            continue
        v = out[k]
        if isinstance(padrao, bool):
            if not isinstance(v, bool):
                log(f"config: '{k}' nao e' booleano — usando default")
                out[k] = padrao
        elif isinstance(padrao, int):
            if isinstance(v, bool):
                log(f"config: '{k}' veio booleano onde se espera inteiro "
                    f"— usando default")
                out[k] = padrao
            elif not isinstance(v, int):
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    log(f"config: '{k}' nao e' inteiro — usando default")
                    out[k] = padrao
        elif isinstance(padrao, float):
            # `float(True) == 1.0` passava limpo no branch float e virava um
            # valor de configuracao que ninguem escreveu. Booleano NUNCA e'
            # numero valido aqui: cai no default.
            if isinstance(v, bool):
                log(f"config: '{k}' veio booleano onde se espera numero "
                    f"— usando default")
                out[k] = padrao
            elif isinstance(v, (int, float)):
                out[k] = float(v)
            else:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    log(f"config: '{k}' nao e' numero — usando default")
                    out[k] = padrao
        elif isinstance(padrao, str):
            if not isinstance(v, str):
                log(f"config: '{k}' nao e' texto — usando default")
                out[k] = padrao
        elif isinstance(padrao, list):
            if not isinstance(v, list):
                log(f"config: '{k}' nao e' lista — usando default")
                out[k] = padrao
        elif isinstance(padrao, dict):
            if not isinstance(v, dict):
                log(f"config: '{k}' nao e' objeto — usando default")
                out[k] = padrao
    # profiles: cada perfil tem que ser dict; initial_prompt tem que ser str
    perfis = out.get("profiles")
    if not isinstance(perfis, dict):
        out["profiles"] = dict(PADROES["profiles"])
    else:
        limpos = {}
        for nome, p in perfis.items():
            if not isinstance(p, dict):
                log(f"config: perfil '{nome}' invalido — ignorado")
                continue
            q = dict(p)
            if "initial_prompt" in q and not isinstance(q["initial_prompt"], str):
                log(f"config: profiles.{nome}.initial_prompt nao e' texto — removido")
                q.pop("initial_prompt")
            limpos[nome] = q
        out["profiles"] = limpos
    return out


def carregar(forcar=False) -> dict:
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not forcar:
            return _CACHE
        bruto = {}
        p = caminho_config()
        try:
            if p.exists():
                bruto = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(bruto, dict):
                    log("config: raiz nao e' objeto — ignorado por inteiro")
                    bruto = {}
        except (OSError, ValueError) as e:
            log(f"config: ilegivel ({type(e).__name__}) — usando so os defaults do codigo")
            bruto = {}
        # chaves `_*_doc` sao comentarios
        bruto = {k: v for k, v in bruto.items() if not k.startswith("_")}
        _CACHE = _coagir(_merge_profundo(PADROES, bruto))
        return _CACHE


def get(chave, padrao=None):
    cfg = carregar()
    if chave in cfg:
        return cfg[chave]
    return PADROES.get(chave, padrao)


def gravar(mudancas: dict):
    """Grava overrides no config.json (merge raso na raiz), atomicamente."""
    global _CACHE
    with _LOCK:
        p = caminho_config()
        p.parent.mkdir(parents=True, exist_ok=True)
        atual = {}
        try:
            if p.exists():
                atual = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(atual, dict):
                    atual = {}
        except (OSError, ValueError):
            atual = {}
        atual.update(mudancas)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(atual, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
        _CACHE = None
    carregar(forcar=True)


def perfil_valor(perfil: str, chave: str, fallback):
    """Valor de uma chave DENTRO do perfil, caindo pro top-level SO quando a
    chave esta AUSENTE (nao quando esta presente e vazia).

    Este `in` e' o conserto do incidente que matou o dicionario medico por 3
    semanas: `p.get(chave, fallback)` com `""` presente devolvia `""`.
    """
    cfg = carregar()
    p = (cfg.get("profiles") or {}).get(perfil)
    if isinstance(p, dict) and chave in p:
        return p[chave]
    if chave in cfg:
        return cfg[chave]
    return fallback


def initial_prompt(perfil: str = "livre") -> str:
    v = perfil_valor(perfil, "initial_prompt", "")
    return v if isinstance(v, str) else ""


# ════════════════════════════════════════════════════════════════════
# Dicionario pessoal (vies do whisper). Editavel pelo usuário.
DICIONARIO_PADRAO = """\
# Dicionario pessoal do Ditado Mic.
# Uma palavra ou termo por linha. Linha com # e' comentario.
# Serve so de DICA para o whisper acertar a grafia: nunca troca o texto.
# So cabem ~300 letras no total: ponha no TOPO o que ele mais erra.
# Exemplo: um nome proprio, uma sigla da sua area, uma marca.
# Vale no proximo ditado, sem reiniciar.
"""

_DIC_CACHE: dict = {"chave": None, "termos": []}


def caminho_dicionario() -> Path:
    return dir_config() / "dicionario.txt"


def garantir_dicionario() -> bool:
    """Cria o dicionario com a lista inicial SO se ele nao existir (nunca
    sobrescreve o que o usuário editou). Devolve True se criou."""
    p = caminho_dicionario()
    try:
        if p.exists():
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DICIONARIO_PADRAO, encoding="utf-8")
        return True
    except OSError:
        return False


def termos_dicionario() -> list[str]:
    """Termos do arquivo, na ordem, sem comentario/duplicata. Cache por
    mtime+tamanho: editar o arquivo vale no proximo ditado."""
    p = caminho_dicionario()
    try:
        st = p.stat()
        chave = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    if _DIC_CACHE["chave"] == chave:
        return list(_DIC_CACHE["termos"])
    termos, vistos = [], set()
    try:
        linhas = p.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for linha in linhas:
        t = " ".join(linha.split("#", 1)[0].split()).strip(" ,;.")
        if not t or len(t) > 60:
            continue
        k = t.lower()
        if k in vistos:
            continue
        vistos.add(k)
        termos.append(t)
    _DIC_CACHE["chave"], _DIC_CACHE["termos"] = chave, termos
    return list(termos)


def parte_dicionario(prompt_base: str) -> str:
    """Trecho do dicionario que entra ANTES do prompt base ("Termos: a, b.
    "). Vazio quando: desligado, perfil sem prompt (casual = anti-loop puro)
    ou arquivo vazio. Termos que o prompt base ja tem ficam de fora.

    ORDEM: o whisper.cpp corta o prompt PELO COMECO quando passa do
    `max_context`. Por isso o dicionario vem antes do prompt base (o base,
    calibrado, nunca e' cortado) e em ORDEM INVERSA (as 1as linhas do
    arquivo ficam coladas no base: sao as ultimas a cair)."""
    if not prompt_base or not get("dicionario_ativo", True):
        return ""
    try:
        teto = int(get("dicionario_max_chars", 300))
    except (TypeError, ValueError):
        teto = 300
    if teto <= 0:
        return ""
    base_min = prompt_base.lower()
    escolhidos, total = [], len("Termos: . ")
    for t in termos_dicionario():
        if re.search(r"(?<!\w)" + re.escape(t.lower()) + r"(?!\w)", base_min):
            continue
        custo = len(t) + 2
        if total + custo > teto:
            break
        escolhidos.append(t)
        total += custo
    if not escolhidos:
        return ""
    return "Termos: " + ", ".join(reversed(escolhidos)) + ". "


def tokens_estimados_dicionario(parte: str) -> int:
    """Estimativa PESSIMISTA (para baixo) de tokens do trecho do dicionario."""
    if not parte:
        return 0
    try:
        cpt = float(get("dicionario_chars_por_token", 4.0))
    except (TypeError, ValueError):
        cpt = 4.0
    cpt = max(2.0, cpt)
    return int(len(parte) / cpt)


def initial_prompt_efetivo(perfil: str = "livre") -> tuple[str, str]:
    """(prompt_enviado, prompt_base). O enviado = dicionario + base."""
    base = initial_prompt(perfil)
    return parte_dicionario(base) + base, base


def dir_historico() -> Path:
    d = base_dados() / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def caminho_banco() -> Path:
    base_dados().mkdir(parents=True, exist_ok=True)
    return base_dados() / "history.db"
