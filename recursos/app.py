# -*- coding: utf-8 -*-
"""Ditado Mic — Windows. Orquestrador.

Fluxo ponta a ponta (single-shot, o caminho de 99% do uso):
 1. **Tecla** — gancho WH_KEYBOARD_LL ve o press da tecla escolhida no M0 ->
    debounce de 0,35 s -> alterna gravacao.
 2. **Traz a pilula pra frente** ANTES de qualquer coisa.
 3. **Grava** — WAV 16 kHz mono s16 escrito INCREMENTALMENTE.
 4. **Para** — pilula entra em "transcrevendo".
 5. **Transcreve** — POST no whisper-server; fallback whisper-cli.
 6. **Pos-processa** — anti-alucinacao -> poda de credito -> vocab medico ->
    app-aware.
 7. **Cola** — clipboard + Ctrl+V, depois restaura o clipboard antigo.
 8. **Salva** no historico.
 9. **Formatado tardio** — enfileira pro CLI do Claude, que devolve uma versao
    estruturada 2-10 s depois; ela vai pro banco e pra janelinha ≡, **por
    fora**, sem tocar no que ja esta colado.

CORRIDAS (licao 6.1 e 6.4), todas cobertas aqui:
 - press vs release vem da MENSAGEM do gancho, nunca de mascara agregada;
 - stop durante o start e' AGENDADO (`_stop_pendente`), nao descartado;
 - o perfil so muda no press que INICIA;
 - **geracao em tudo**: thread zumbi pos-timeout nao renomeia/apaga o WAV da
   gravacao nova, nao adota a sessao nova, nem escreve no estado dela;
 - **dict por geracao**, nao slot unico de resultado.

Nada pode atrasar gravar/transcrever/colar: todo trabalho pesado e' thread
daemon.
"""
from __future__ import annotations

import os
import sys
import threading
import queue
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atalhos            # noqa: E402
import captura            # noqa: E402
import clipboard          # noqa: E402
import colar              # noqa: E402
import comando as mod_comando  # noqa: E402
import config             # noqa: E402
import formatado          # noqa: E402
import silencio           # noqa: E402
import historico          # noqa: E402
import seletor_mic        # noqa: E402
import texto as htexto    # noqa: E402
import transcricao        # noqa: E402
import winapi             # noqa: E402
from log import base_dados, log  # noqa: E402

__version__ = "1.0.0-win"

ESTADO_OCIOSO = "ocioso"
ESTADO_INICIANDO = "iniciando"
ESTADO_GRAVANDO = "gravando"
ESTADO_TRANSCREVENDO = "transcrevendo"
ESTADO_ERRO = "erro"


class Microfone:
    """O app. Testavel sem interface: `pilula=None`, `avisar` injetavel, e a
    captura vem de `fabrica_gravador` (em teste, um WAV de arquivo)."""

    def __init__(self, pilula=None, avisar=None, fabrica_gravador=None,
                 colar_fn=None):
        self.pilula = pilula
        self._avisar = avisar or self._avisar_padrao
        self.fabrica_gravador = fabrica_gravador or self._gravador_real
        self.colar_fn = colar_fn or colar.colar_texto

        self.estado = ESTADO_OCIOSO
        self.geracao = 0
        self._starting = False
        self._stop_pendente = False
        self._lock = threading.Lock()
        self._ultimo_toggle = 0.0
        self._gravador = None
        self._perfil = "livre"           # congelado no press que INICIA
        self._exe_inicial = ""
        self.ultimo_texto = ""
        self.resultados: dict = {}       # dict POR GERACAO, nunca slot unico

        self.fila = formatado.Fila()
        self.corretor = None
        self.selecao = None
        self.gancho = None
        self._parar = threading.Event()
        self._acoes_tecla = queue.Queue()
        self._thread_acoes = None
        # recursos do Wispr Flow (27/10/2026)
        self.atalhos = None
        self._colar_ultimo_lock = threading.Lock()
        self._comando_pendente = None    # selecao lida, esperando o start
        self._comandos: dict = {}        # geracao -> selecao (instrucao falada)

    # ── avisos (silencio total: nada de som, nada de janela pulando) ──
    def _avisar_padrao(self, msg):
        if not msg:
            return
        log(f"aviso: {msg[:60]}")
        if self.pilula is not None:
            try:
                self.pilula.definir_estado(self.pilula.estado)
            except Exception:
                pass

    def avisar(self, msg):
        try:
            self._avisar(msg)
        except Exception:
            pass

    def _estado(self, novo, **kw):
        self.estado = novo
        if self.pilula is not None:
            try:
                self.pilula.definir_estado(novo, **kw)
            except Exception:
                pass

    # ── captura ───────────────────────────────────────────────────────
    def _gravador_real(self, destino, geracao):
        config.carregar(forcar=True)
        return captura.Gravador(destino, geracao=geracao)

    def _wav_da_geracao(self, g):
        d = base_dados() / "rec"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"wispr-rec-{os.getpid()}-{g}.wav"

    # ── hotkey ────────────────────────────────────────────────────────
    def acionar_gravacao(self):
        """Entrada dos botoes de interface: mesma fila do Alt direito."""
        import tecla_ditar as td
        self._acoes_tecla.put(td.INICIAR_OU_PARAR)

    def toggle(self):
        """Todo press passa por aqui. Debounce de 0,35 s."""
        # 0) o seletor de microfone (se aberto) fecha em TODO press, ate' no
        #    que cai no debounce. Nao espera o Tk.
        try:
            seletor_mic.fechar_se_aberto("alt direito")
        except Exception:
            pass
        agora = time.monotonic()
        deb = float(config.get("hotkey_debounce_s", 0.35))
        if agora - self._ultimo_toggle < deb:
            return "debounce"
        self._ultimo_toggle = agora

        # 1) a pilula volta a vista em TODO press
        if self.pilula is not None:
            self.pilula.trazer_pra_frente()

        with self._lock:
            # 2) transcrevendo e nao gravando: press IGNORADO (pra ele nao
            #    falar no vazio). Som de "ocupado": o usuário tem 4 cues curtos
            #    no Mac; aqui o retorno e' a COR da pilula (silencio total).
            if self.estado == ESTADO_TRANSCREVENDO:
                log("hotkey: ocupado (transcrevendo) — press ignorado")
                return "ocupado"
            # 3) start ainda subindo: AGENDA o stop, nao perde o comando
            if self._starting:
                self._stop_pendente = True
                log("hotkey: stop agendado (start em curso)")
                return "stop-agendado"
            if self.estado == ESTADO_GRAVANDO:
                parar = True
            else:
                parar = False
                self._starting = True
        if parar:
            self.parar_gravacao()
            return "parado"
        self.iniciar_gravacao()
        return "iniciado"

    # ── gravacao ──────────────────────────────────────────────────────
    def iniciar_gravacao(self):
        self.geracao += 1
        g = self.geracao
        # Instrucao falada: a selecao lida no atalho vale SO para esta geracao
        # (nunca vaza para o proximo Alt direito).
        cmd, self._comando_pendente = self._comando_pendente, None
        self._comandos = {g: cmd} if cmd else {}
        # O PERFIL SO MUDA NO PRESS QUE INICIA (ler o modificador no stop
        # convertia "prontuario" em "livre" em silencio).
        self._perfil = self._perfil_dos_modificadores()
        self._exe_inicial = winapi.app_em_foco()
        self._estado(ESTADO_INICIANDO, segundos=0)
        grav = None
        try:
            grav = self.fabrica_gravador(self._wav_da_geracao(g), g)
            with self._lock:
                if self.geracao != g:
                    return False
                self._gravador = grav
            grav.iniciar()
            if self.geracao != g or self._gravador is not grav:
                grav.parar()
                self._limpar_wav(grav)
                return False
        except Exception as e:
            log(f"gravacao: nao iniciou ({type(e).__name__})")
            if grav:
                self._limpar_wav(grav)
            if self.geracao != g:
                return False
            self._gravador = None
            self._starting = False
            self._stop_pendente = False
            self._estado(ESTADO_ERRO)
            self.avisar("A gravacao nao iniciou — confira o microfone.")
            return False
        self._estado(ESTADO_GRAVANDO, nivel=0.0, segundos=0)
        self._starting = False
        threading.Thread(target=self._tick_gravacao, args=(g,),
                         name="tick-gravacao", daemon=True).start()
        if self._stop_pendente:
            self._stop_pendente = False
            log("hotkey: executando o stop agendado")
            threading.Thread(target=self.parar_gravacao, args=(g,), daemon=True).start()
        return True

    def _tick_gravacao(self, g):
        """Atualiza timer/VU e o alerta PASSIVO de mic mudo."""
        while self.geracao == g and self.estado == ESTADO_GRAVANDO:
            grav = self._gravador
            if grav is None:
                break
            if getattr(grav, "erro", None):
                log("gravacao: captura interrompida — preservando o trecho captado")
                self.avisar("O microfone desconectou; confira o trecho captado.")
                self._acoes_tecla.put("parar")
                break
            seg = int(grav.elapsed)
            mudo = self._mic_mudo(seg, grav.pico)
            nivel = getattr(grav, "nivel", grav.pico)
            self._estado(ESTADO_GRAVANDO, nivel=min(1.0, nivel * 3),
                         segundos=seg, mic_mudo=mudo)
            time.sleep(0.15)

    @staticmethod
    def _mic_mudo(gravando_ha_s, pico) -> bool:
        """Anel AMBAR. PASSIVO: sem som, sem notificacao, sem texto.
        E NUNCA por pausa no MEIO — so quando NUNCA houve fala desde o inicio
        (o usuário pausa pra ler questoes)."""
        try:
            limite_s = float(config.get("mic_mudo_alerta_s", 4.0))
            pico_min = float(config.get("mic_mudo_peak_min", 0.02))
        except (TypeError, ValueError):
            limite_s, pico_min = 4.0, 0.02
        return bool(gravando_ha_s >= limite_s and float(pico or 0) < pico_min)

    def parar_gravacao(self, geracao=None):
        g = self.geracao if geracao is None else geracao
        if self.geracao != g:
            return None
        grav = self._gravador
        if grav is None:
            self._estado(ESTADO_OCIOSO)
            return None
        self._estado(ESTADO_TRANSCREVENDO)
        duracao = grav.parar()
        if self.geracao != g:
            self._limpar_wav(grav)
            return None
        self._gravador = None
        d = grav.drift()
        if d:
            log(d)
        threading.Thread(target=self._processar, args=(g, grav, duracao),
                         name="processar", daemon=True).start()
        return g

    # ── pipeline pos-gravacao ─────────────────────────────────────────
    def _processar(self, g, grav, duracao):
        try:
            self._processar_interno(g, grav, duracao)
        except Exception as e:
            log(f"pipeline: quebrou ({type(e).__name__})")
            self.avisar("Nao deu pra processar o ditado.")
        finally:
            self._comandos.pop(g, None)
            if self.geracao == g:
                self._estado(ESTADO_OCIOSO)

    def _processar_interno(self, g, grav, duracao):
        # GERACAO na ENTRADA: se esta sessao ja foi superada, nem gasta o motor
        # — e, principalmente, nao toca no WAV da gravacao NOVA.
        if self.geracao != g:
            log(f"pipeline: geracao {g} obsoleta na entrada — nada a fazer")
            return
        if not Path(grav.destino).exists():
            log("pipeline: o WAV da gravacao sumiu — nada a transcrever")
            return
        minimo = float(config.get("audio_curto_min_s", 0.35))
        if duracao < minimo:
            log(f"pipeline: audio curto demais ({duracao:.2f}s) — descartado")
            self._limpar_wav(grav)
            return
        # PORTAO DE ENERGIA: sem fala nao chega ao whisper (silencio puro vira
        # "Legendas por <nome>"; so energia pega isso). Qualquer duvida -> segue.
        an = silencio.analisar(grav.destino)
        fala_s = an["fala_s"] if an else None
        if silencio.sem_fala(an):
            log(f"pipeline: sem fala (pico={an['pico']:.3f}, fala={an['fala_s']:.2f}s, "
                f"audio {duracao:.1f}s) — nao transcreve")
            self._limpar_wav(grav)
            self.avisar("Nada transcrito (sem fala).")
            return
        wav_envio, cortado = silencio.compactar(grav.destino, an)
        if wav_envio:
            log(f"silencio: {cortado:.1f}s de pausa longa compactados "
                f"({duracao:.1f}s -> {duracao - cortado:.1f}s)")
        # GANHO para fala baixa: so na copia enviada ao whisper, DEPOIS do
        # portao (que julgou o audio original).
        wav_ganho, ganho = silencio.amplificar(wav_envio or grav.destino, an)
        if wav_ganho:
            log(f"silencio: ganho digital {ganho:.1f}x (pico {an['pico']:.3f})")
        try:
            r = transcricao.transcrever(wav_ganho or wav_envio or grav.destino,
                                        duracao - cortado, self._perfil)
        finally:
            for tmp in (wav_ganho, wav_envio):
                if tmp:
                    try:
                        Path(tmp).unlink(missing_ok=True)
                    except OSError:
                        pass
        # GERACAO: thread zumbi nao adota a sessao nova.
        if self.geracao != g:
            log(f"pipeline: geracao {g} obsoleta — resultado descartado")
            self._limpar_wav(grav)
            return
        bruto = r.get("texto")
        if bruto is None:
            # WAV preservado SO em falha total de audio LONGO.
            preserva = float(config.get("failed_wav_preserva_min_s", 120.0))
            if duracao >= preserva:
                log(f"pipeline: falha total — WAV preservado ({duracao:.0f}s)")
                self.resultados[g] = {"wav": str(grav.destino)}
            else:
                self._limpar_wav(grav)
            self.avisar("Nada transcrito — o motor nao respondeu.")
            return
        limpo, descarte, aviso = htexto.higienizar(bruto, grav.pico, fala_s)
        self._limpar_wav(grav)
        cmd = self._comandos.pop(g, None)
        if cmd is not None:
            # INSTRUCAO FALADA: nada e' colado como ditado, nada vai pro ≡.
            self._executar_comando(cmd, "" if descarte else (limpo or ""))
            return
        if descarte:
            log(f"pipeline: descartado ({descarte}, pico={grav.pico:.3f})")
            if aviso:
                self.avisar(aviso)
            return
        if not limpo:
            return
        limpo = htexto.aplicar_vocab_medico(limpo, self._vocab())
        limpo = htexto.aplicar_app_aware(limpo, winapi.app_em_foco())

        # RE-CHECAGEM DE GERACAO ANTES DE COLAR: nao cola texto velho no app
        # errado depois de um polish demorado.
        if config.get("recheck_gen_before_paste", True) and self.geracao != g:
            log("pipeline: geracao mudou antes do paste — vai so pro historico")
            self._salvar(limpo, duracao, r.get("segundos", 0.0))
            return
        exe_agora = winapi.app_em_foco()
        colou, msg = self.colar_fn(limpo, exe_alvo=exe_agora)
        if msg:
            self.avisar(msg)
        if (colou and config.get("warn_focus_changed", True)
                and exe_agora and self._exe_inicial
                and exe_agora != self._exe_inicial):
            self.avisar("O foco mudou desde o inicio do ditado — confira onde colou")
        self.ultimo_texto = limpo
        self.resultados[g] = {"texto": limpo, "colou": colou}
        rowid_box = {}
        self._salvar(limpo, duracao, r.get("segundos", 0.0), rowid_box)
        # FORMATADO TARDIO: so depois de colar de verdade.
        if colou and config.get("formatado_tardio", True):
            self._enfileirar_formatado(limpo, rowid_box)

    # ── recursos do Wispr Flow (27/10/2026) ───────────────────────────
    def colar_ultimo(self, esperar=None):
        """Atalho global: cola de novo o ULTIMO ditado (o texto cru, o mesmo
        que foi colado). Espera ele soltar as teclas; sem janela, sem som."""
        if not self._colar_ultimo_lock.acquire(blocking=False):
            log("colar-ultimo: pedido concorrente ignorado")
            return "ocupado"
        try:
            texto = self.ultimo_texto or historico.ultimo_ditado()
            if not texto:
                log("colar-ultimo: nenhum ditado ainda")
                self.avisar("Ainda nao ha ditado para colar.")
                return "vazio"
            esperar = esperar or atalhos.esperar_soltar_modificadores
            if not esperar(float(config.get("atalho_espera_soltar_s", 3.0))):
                log("colar-ultimo: teclas seguradas demais — nao colado")
                return "preso"
            exe = winapi.app_em_foco()
            colou, msg = self.colar_fn(texto, exe_alvo=exe)
            if msg:
                self.avisar(msg)
            log(f"colar-ultimo: {'colado' if colou else 'nao colado'} "
                f"({len(texto)}c)")
            return "colado" if colou else "falhou"
        finally:
            self._colar_ultimo_lock.release()

    def comando_voz_acionar(self):
        """Atalho global da instrucao falada: so ENFILEIRA (mesma fila do Alt
        direito, entao nunca cruza um start/stop)."""
        if self._fila_viva():
            self._acoes_tecla.put(("comando",))
        else:
            log("comando: fila de acoes parada — ignorado")

    def _comando_toggle(self, ler_selecao=None, esperar=None):
        """1o aperto: le a selecao e comeca a gravar a instrucao.
        2o aperto (ou Alt direito): para; o resto e' `_executar_comando`."""
        import selecao as mod_selecao
        if self.estado == ESTADO_GRAVANDO:
            if self._comandos.get(self.geracao) is not None:
                return self.toggle()
            log("comando: ignorado (ditado normal em curso)")
            return "ignorado"
        if self.estado == ESTADO_TRANSCREVENDO or self._starting:
            log("comando: ocupado — ignorado")
            return "ocupado"
        esperar = esperar or atalhos.esperar_soltar_modificadores
        if not esperar(float(config.get("atalho_espera_soltar_s", 3.0))):
            log("comando: teclas seguradas demais — ignorado")
            return "preso"
        exe = winapi.app_em_foco()
        ler = ler_selecao or mod_selecao.ler_selecao
        texto, via = ler("atalho", exe)
        if not texto or not texto.strip():
            log(f"comando: sem selecao (via={via})")
            self.avisar(mod_comando.mensagem_de_falha("fail:sem-selecao"))
            return "sem-selecao"
        maximo = int(config.get("selecao_max_chars", 12000))
        if len(texto) > maximo:
            self.avisar(mod_comando.mensagem_de_falha("fail:grande"))
            return "grande"
        self._comando_pendente = {"texto": texto, "exe": exe}
        r = self.toggle()
        if r != "iniciado":
            self._comando_pendente = None
        else:
            log(f"comando: gravando instrucao ({len(texto)}c selecionados, via={via})")
        return r

    def _executar_comando(self, cmd, instrucao, executar=None):
        """Aplica a instrucao a selecao. SUCESSO E' MUDO (veto 2)."""
        import selecao as mod_selecao
        texto, exe = cmd.get("texto") or "", cmd.get("exe") or ""
        if not instrucao.strip():
            log("comando: falhou (sem instrucao)")
            self.avisar(mod_comando.mensagem_de_falha("fail:sem-instrucao"))
            return "sem-instrucao"
        atividade0 = mod_selecao._ATIVIDADE["n"]
        # O ORIGINAL vai pro historico ANTES — e' o "desfazer" do ≡.
        caixa = {}
        historico.inserir(texto, duracao_s=0, transcricao_s=0, perfil="comando",
                          app=exe, ao_terminar=lambda rid: caixa.update(rowid=rid))
        log(f"comando: pedindo ({len(texto)}c, instrucao {len(instrucao)}c)")
        saida, motivo = (executar or mod_comando.executar)(texto, instrucao)
        if not saida:
            log(f"comando: falhou ({motivo})")
            self.avisar(mod_comando.mensagem_de_falha(motivo))
            return motivo
        entrega = mod_selecao.decidir_entrega(exe, winapi.app_em_foco(), atividade0)
        if entrega == "colar":
            mod_selecao._ATIVIDADE["ignorar_ate"] = time.monotonic() + 0.4
            colou, aviso = self.colar_fn(saida, exe_alvo=exe)
            if not colou:
                entrega = "clipboard"
                if aviso:
                    self.avisar(aviso)
        if entrega == "clipboard":
            if clipboard.escrever_texto(saida):
                self.avisar("Resultado copiado — Ctrl+V cola onde quiser.")
            else:
                log("comando: falhou (clipboard recusou)")
                self.avisar(mod_comando.mensagem_de_falha(""))
                return "fail:clipboard"

        # o resultado vira o "formatado" da linha do original (aparece no ≡)
        def _gravar():
            for _ in range(40):
                if "rowid" in caixa:
                    break
                time.sleep(0.05)
            historico.gravar_formatado(caixa.get("rowid"), saida, cru_esperado=texto)
        threading.Thread(target=_gravar, name="comando-hist", daemon=True).start()
        # SUCESSO E' MUDO: so log, sem aviso.
        log(f"comando: pronto ({len(texto)}c -> {len(saida)}c, entrega={entrega}, "
            f"app={exe})")
        return entrega

    def instalar_atalhos(self):
        """Win+Alt+V (colar o ultimo) e Win+Alt+C (instrucao falada)."""
        a = atalhos.AtalhosGlobais()
        if config.get("colar_ultimo_ativo", True):
            a.adicionar(str(config.get("colar_ultimo_atalho", "win+alt+v")),
                        self.colar_ultimo, "colar-ultimo")
        if config.get("comando_voz_ativo", True):
            a.adicionar(str(config.get("comando_voz_atalho", "win+alt+c")),
                        self.comando_voz_acionar, "comando-voz")
        a.iniciar()
        self.atalhos = a
        return a

    def _limpar_wav(self, grav):
        try:
            if not config.get("history_save_audio", False):
                Path(grav.destino).unlink(missing_ok=True)
        except OSError:
            pass

    def _vocab(self):
        if not config.get("medical_vocab_enabled", False):
            return None
        try:
            import json
            p = config.dir_config() / "medical_vocab.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            return None

    def _salvar(self, txt, duracao, transcricao_s, caixa=None):
        def pronto(rowid):
            if caixa is not None:
                caixa["rowid"] = rowid
        historico.inserir(txt, duracao_s=duracao, transcricao_s=transcricao_s,
                          perfil=self._perfil, app=self._exe_inicial,
                          ao_terminar=pronto)

    def _enfileirar_formatado(self, cru, rowid_box):
        def terminou(saida, motivo):
            if not saida:
                return
            # Backtrack de numero e muletas: SO no formatado (≡), nunca no
            # colado (veto 14). Deterministico; o prompt tem sha256.
            try:
                saida = htexto.limpar_formatado(saida) or saida
            except Exception as e:
                log(f"formatado: limpeza falhou ({type(e).__name__}) — segue sem")
            rowid = rowid_box.get("rowid")
            gravou = historico.gravar_formatado(rowid, saida, cru_esperado=cru)
            # via clipboard: so troca se o clipboard AINDA for exatamente o cru
            trocou = False
            if clipboard.tem_exatamente(cru):
                trocou = clipboard.escrever_texto(saida)
                if not trocou:
                    clipboard.escrever_texto(cru)   # nunca deixar vazio
            # MENSAGEM HONESTA: "Ctrl+V cola" so quando trocou o clipboard;
            # "no botao ≡" so quando o insert CONFIRMOU; silencio quando
            # nenhuma via entregou.
            if trocou:
                self.avisar("Revisao pronta — Ctrl+V cola.")
            elif gravou:
                log("formatado: entregue no botao ≡")
        self.fila.enfileirar(cru, ref=None, ao_terminar=terminou)

    # ── perfil ────────────────────────────────────────────────────────
    @staticmethod
    def _perfil_dos_modificadores() -> str:
        """No Mac: Shift = prontuario, Option = casual. O historico e' 100%
        `livre` (ele nunca segura Shift) — prontuario/casual existem, mas sao
        letra morta. Aqui Shift mantem o prontuario; casual fica fora da v1."""
        if winapi.user32.GetAsyncKeyState(winapi.VK_SHIFT) & 0x8000:
            return "prontuario"
        return "livre"

    # ── cancelamento silencioso (AltGr+tecla) ─────────────────────────
    def cancelar_gravacao_silenciosa(self, motivo="altgr"):
        """Cancela a gravacao SEM colar, SEM avisar e descartando o WAV.

        E' o que acontece quando o Alt direito era, na verdade, um AltGr+tecla:
        os acentos dele tem que continuar funcionando, e ele nem pode perceber
        que o microfone chegou a abrir.
        """
        with self._lock:
            if self.estado not in (ESTADO_GRAVANDO, ESTADO_INICIANDO):
                return False
            self.geracao += 1          # invalida quem estiver em voo
            grav, self._gravador = self._gravador, None
            self._stop_pendente = False
            self._starting = False
        self._estado(ESTADO_OCIOSO)
        if grav is None:
            return True
        try:
            grav.parar()
        except Exception:
            pass
        try:
            Path(grav.destino).unlink(missing_ok=True)
        except OSError:
            pass
        log(f"hotkey: ditado cancelado em silencio ({motivo}) — "
            f"{grav.duracao_audio:.2f}s de audio descartados")
        return True

    # ── gancho de teclado ─────────────────────────────────────────────
    def _executar_acoes_tecla(self):
        """Audio e stop podem esperar pelo driver; o gancho nunca espera."""
        while not self._parar.is_set():
            acao = self._acoes_tecla.get()
            if acao is None:
                return
            try:
                self._executar_uma_acao(acao)
            except Exception as e:
                log(f"hotkey: acao falhou ({type(e).__name__})")
            finally:
                maquina = getattr(self, "maquina", None)
                if maquina is not None and self._acoes_tecla.empty():
                    maquina.ditando = self.estado in (
                        ESTADO_INICIANDO, ESTADO_GRAVANDO)

    def _executar_uma_acao(self, acao):
        """Uma acao da fila. Tudo que mexe no audio passa por esta UNICA thread
        (acoes-teclado): uma recarga da lista nunca cruza um start."""
        import tecla_ditar as td
        if isinstance(acao, tuple) and acao and acao[0] == "recarregar_mics":
            self._recarregar_mics(acao[1] if len(acao) > 1 else None)
        elif isinstance(acao, tuple) and acao and acao[0] == "comando":
            self._comando_toggle()
        elif acao == "limpar_erro":
            if self.estado == ESTADO_ERRO:
                self._estado(ESTADO_OCIOSO)
        elif acao == td.INICIAR_OU_PARAR:
            self.toggle()
        elif acao == td.CANCELAR_SILENCIOSO:
            self.cancelar_gravacao_silenciosa("altgr")
            if self.estado == ESTADO_ERRO:
                self._estado(ESTADO_OCIOSO)
        elif acao == "parar" and self.estado == ESTADO_GRAVANDO:
            self.parar_gravacao()

    # ── microfone escolhido na pilula / bandeja ───────────────────────
    def _fila_viva(self) -> bool:
        t = self._thread_acoes
        return t is not None and t.is_alive()

    def limpar_erro(self):
        """Escolha nova de microfone: se estava em ERRO ('Sem microfone'),
        volta a ocioso — pela fila, na ordem dos presses."""
        if self._fila_viva():
            self._acoes_tecla.put("limpar_erro")
        else:
            self._executar_uma_acao("limpar_erro")

    def pedir_recarga_mics(self, ao_terminar=None) -> bool:
        """'Atualizar lista' (seletor da pilula e bandeja): so ENFILEIRA. Quem
        recarrega e' a fila acoes-teclado, e so com o app ocioso ou em erro.
        `ao_terminar(ok)` e' SEMPRE chamado (na thread da fila)."""
        if not self._fila_viva():
            log("mic: lista nao atualizada (fila de acoes parada)")
            _chamar_sem_erro(ao_terminar, False)
            return False
        self._acoes_tecla.put(("recarregar_mics", ao_terminar))
        return True

    def _pode_recarregar_mics(self) -> bool:
        """Pa_Terminate/Pa_Initialize NUNCA com captura ativa ou subindo."""
        with self._lock:
            return (self.estado in (ESTADO_OCIOSO, ESTADO_ERRO)
                    and not self._starting and self._gravador is None)

    def _recarregar_mics(self, ao_terminar=None) -> bool:
        ok = False
        if not self._pode_recarregar_mics():
            log(f"mic: lista nao atualizada (app {self.estado})")
        else:
            antes = captura._RECARGAS
            try:
                mics = captura.listar_microfones(atualizar=True)
                ok = captura._RECARGAS > antes
                log(f"mic: lista atualizada a pedido ({len(mics)} entradas)" if ok
                    else "mic: lista nao atualizada (captura ativa)")
            except Exception as e:
                log(f"mic: lista nao atualizada ({type(e).__name__})")
        _chamar_sem_erro(ao_terminar, ok)
        return ok

    def instalar_gancho(self):
        import tecla_ditar as td

        vk_ditar = int(config.get("hotkey_ditar_vk", 0) or 0)
        vk_fmt = int(config.get("hotkey_formatar_vk", 0xA3) or 0xA3)
        if not vk_ditar:
            log("hotkey: nenhuma tecla de ditar escolhida — rode o "
                "CONFIGURAR-TECLA.bat. O app segue vivo sem ditado por tecla.")
        # O Alt direito NUNCA pode ser engolido: AltGr+tecla tem que passar.
        suprimir = set()
        if (vk_ditar and vk_ditar != td.VK_RMENU
                and config.get("hotkey_ditar_suprimir", False)):
            suprimir.add(vk_ditar)

        e_altgr = (vk_ditar == td.VK_RMENU)
        if e_altgr and not config.get("tecla_ditar_altgr_autorizado", False):
            log("hotkey: Alt direito pedido sem `tecla_ditar_altgr_autorizado` "
                "— ligue a chave no config ou escolha outra tecla")
        self.maquina = td.MaquinaAltDireito(
            janela_combinacao_s=float(config.get("altgr_janela_combinacao_s", 0.35)),
            debounce_s=float(config.get("hotkey_debounce_s", 0.35)),
            quebrar_menu=bool(config.get("altgr_quebrar_menu", True)))
        self._thread_acoes = threading.Thread(target=self._executar_acoes_tecla,
                                              name="acoes-teclado", daemon=True)
        self._thread_acoes.start()

        def ao_evento(vk, pressionada, extended, scancode=0, quando_ms=None):
            try:
                if e_altgr:
                    for acao in self.maquina.evento(vk, pressionada, extended,
                                                    scancode, quando_ms):
                        if acao == td.QUEBRAR_MENU:
                            winapi.quebrar_alt_sozinho()
                        elif acao == td.INICIAR_OU_PARAR:
                            self._acoes_tecla.put(acao)
                        elif acao == td.CANCELAR_SILENCIOSO:
                            self._acoes_tecla.put(acao)
                        elif acao == td.FORMATAR and self.corretor:
                            self.corretor.acionar()
                else:
                    if pressionada and vk_ditar and vk == vk_ditar:
                        self._acoes_tecla.put(td.INICIAR_OU_PARAR)
                        return
                    if pressionada and vk == vk_fmt and extended:
                        if self.corretor:
                            self.corretor.acionar()
                        return
                if pressionada and self.corretor:
                    if not (e_altgr and self.maquina.alt_segurado):
                        self.corretor.tecla(vk, _char_da_tecla(vk))
                if pressionada and self.selecao:
                    from selecao import registrar_atividade
                    registrar_atividade()
            except Exception:
                pass

        self.gancho = winapi.GanchoTeclado(ao_evento, suprimir_vks=suprimir)
        ok = self.gancho.iniciar()
        nome = config.get("hotkey_ditar_nome", "") or f"0x{vk_ditar:02X}"
        log(f"hotkey: gancho {'instalado' if ok else 'FALHOU'} "
            f"(ditar={nome} 0x{vk_ditar:02X}"
            f"{' [AltGr autorizado]' if e_altgr else ''}, "
            f"formatar=Ctrl direito 0x{vk_fmt:02X})")
        threading.Thread(target=self._watchdog_gancho, name="watchdog-gancho",
                         daemon=True).start()
        return ok

    def _watchdog_gancho(self):
        """A cada 5 s confere se o gancho esta vivo; o proprio GanchoTeclado
        remonta sozinho, aqui so o registro do incidente (uma vez por queda)."""
        ultimo = 0
        while not self._parar.wait(5.0):
            g = self.gancho
            if g is None:
                continue
            if g.remontagens != ultimo:
                ultimo = g.remontagens
                log(f"hotkey: gancho remontado ({ultimo}x)")

    # ── ciclo ─────────────────────────────────────────────────────────
    def encerrar(self):
        self._parar.set()
        self._acoes_tecla.put(None)
        if self._gravador:
            self._gravador.parar()
            self._limpar_wav(self._gravador)
            self._gravador = None
        try:
            self.fila.cancelar_tudo("fail:cancelado")
        except Exception:
            pass
        if self.gancho:
            self.gancho.parar()
        if self.atalhos:
            try:
                self.atalhos.parar()
            except Exception:
                pass


def _chamar_sem_erro(fn, *a):
    """Callback de quem pediu (seletor/bandeja): erro dele nao derruba a fila."""
    if fn is None:
        return
    try:
        fn(*a)
    except Exception as e:
        log(f"mic: retorno do pedido falhou ({type(e).__name__})")


def _char_da_tecla(vk):
    """Caractere que a tecla produz no layout ativo (pro buffer do corretor)."""
    import ctypes
    from ctypes import wintypes
    u = winapi.user32
    estado = (ctypes.c_ubyte * 256)()
    u.GetKeyboardState(estado)
    buf = ctypes.create_unicode_buffer(8)
    sc = u.MapVirtualKeyW(vk, 0)
    n = u.ToUnicodeEx(wintypes.UINT(vk), wintypes.UINT(sc), estado, buf, 8,
                      wintypes.UINT(0), u.GetKeyboardLayout(0))
    if n > 0 and buf.value and buf.value.isprintable():
        return buf.value[0]
    return None


# ════════════════════════════════════════════════════════════════════
def main():
    from proc import InstanciaUnica
    with InstanciaUnica("Local\\DitadoMic-App") as unica:
        if not unica:
            log("app: ja esta aberto — segundo gancho evitado")
            return 0
        return _executar_app()


def _executar_app():
    import bandeja
    import corretor as mod_corretor
    import pilula as mod_pilula
    import selecao as mod_selecao

    log(f"app: subindo v{__version__} (pid {os.getpid()})")
    from proc import limpar_saida_intencional
    limpar_saida_intencional()
    historico.criar()
    try:
        if config.garantir_dicionario():
            log("dicionario: criado com a lista inicial")
    except Exception:
        pass
    try:
        historico.aplicar_retencao()
    except Exception:
        pass

    app = Microfone()
    p = mod_pilula.Pilula(ao_clique=lambda b: _clique_da_pilula(app, b))
    if not p.iniciar():
        log("pilula: nao subiu — seguindo sem ela")
        p = None
    app.pilula = p

    app.selecao = mod_selecao.Selecao(
        app.fila, avisar=app.avisar, pilula=p,
        historico_inserir=historico.inserir)
    app.corretor = mod_corretor.Corretor(
        ao_pedir_selecao=lambda: app.selecao.acionar("atalho"),
        avisar=app.avisar)
    app.instalar_gancho()
    try:
        app.instalar_atalhos()
    except Exception as e:
        log(f"atalho: nao instalados ({type(e).__name__}) — o ditado segue normal")

    icone = bandeja.criar(app)
    try:
        icone.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.encerrar()
        if p:
            p.encerrar()
    return 0


def _clique_da_pilula(app, botao):
    if botao == "seletor":
        # O UNICO caminho que abre o seletor de microfone: clique DELE na
        # pilula (direito em qualquer ponto, ou esquerdo no nome). Nunca sozinho.
        seletor_mic.alternar(app)
        return
    # qualquer outro clique na pilula fecha o seletor (se aberto) e segue
    seletor_mic.fechar_se_aberto("clique na pilula")
    if botao == "mic":
        app.acionar_gravacao()
    elif botao == "formatar" and app.selecao:
        app.selecao.acionar("pilula")
    elif botao == "min" and app.pilula:
        if app.estado == ESTADO_GRAVANDO:
            app.avisar("Continua gravando com o painel escondido — "
                       "aperte a tecla de ditar para parar.")
        app.pilula.esconder(por_usuario=True)
    elif botao == "close" and app.pilula:
        # DORMIR: esconde, mas o gancho continua vivo. O × NAO encerra o app.
        if app.estado == ESTADO_TRANSCREVENDO:
            app.avisar("Tem transcricao em voo — ela nao foi cancelada.")
        app.pilula.esconder(por_usuario=True)
    elif botao == "textos":
        import janelinha
        janelinha.alternar(app)
    elif botao == "stream":
        # Streaming ao vivo esta FORA DA V1 (no Mac ficou desligado e o
        # historico e' 100% modo livre). Botao presente e inerte, de proposito.
        log("pilula: botao stream — streaming ao vivo esta fora da v1")
    elif botao == "restart":
        log("pilula: restart pedido")
        app.avisar("Use o REINICIAR.bat — o painel nunca se reinicia sozinho.")


if __name__ == "__main__":
    sys.exit(main())
