# -*- coding: utf-8 -*-
"""A pilula: barra escura sempre-no-topo que NUNCA rouba o foco (M5).

tkinter + estilos de janela via ctypes. O ponto critico e' `WS_EX_NOACTIVATE`:
sem ele, clicar na pilula rouba o foco do editor e o Ctrl+V cola no lugar
errado. Junto: `WS_EX_TOPMOST` e `WS_EX_TOOLWINDOW` (fora do Alt-Tab e da barra
de tarefas). `testes/teste_pilula_foco.py` prova isso com uma janela de teste
propria: foco nela -> mostra a pilula -> `GetForegroundWindow` NAO mudou.

FLAG DE INTENCAO (`_escondido_por_usuario`): `IsWindowVisible` nao distingue
"o usuário escondeu" de "sumiu por bug". Sem esse flag, ou o heartbeat desfaz o
"minimizar" dele em 2 s (ele clicou 8x e desistiu), ou um sumico por bug deixa
a janela invisivel pra sempre.

RESTAURACAO FORTE: REPOSICIONA antes de mostrar — janela parada em coordenada
de um monitor que nao existe mais nao volta so com "mostrar".

BACKOFF: apos 3 tentativas sem efeito, as checagens espacam 15 s -> 60 s ->
5 min e o app loga UMA vez um retrato completo da janela (detector que acusa e
nao conserta vira tempestade).

SEGUE A TELA DO CURSOR, NAO O CURSOR: cursor na mesma tela = o painel NAO se
mexe (seguir o mouse distrai). Decide pelo CENTRO do painel. O pulo NUNCA grava
no arquivo de posicao.

Estados e cores — a cor do circulo do mic e' o feedback principal:
  ocioso cinza · iniciando AMBAR · gravando VERMELHO · transcrevendo AZUL ·
  polindo (IA) ROXO. Alerta "mic mudo" = anel AMBAR, PASSIVO: sem som, sem
  notificacao, sem texto. E NUNCA alerta por pausa no MEIO (ele pausa pra ler
  questoes) — so quando NUNCA houve fala desde o inicio.

CLIQUES (11/10/2026, seletor de microfone — `seletor_mic.py`):
  esquerdo no icone do mic = grava/para (como sempre); esquerdo nos botoes =
  o botao; esquerdo SEM arrasto no NOME = abre/fecha o seletor; DIREITO em
  qualquer ponto = abre/fecha o seletor; arrasto (> 5 px) so move, nunca e'
  clique. O roteamento e' PURO (`alvo_do_clique`), testado sem janela.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import config
import seletor_mic
import winapi
import visual
from log import base_dados, log

L_EXPANDIDA, L_COMPACTA, ALTURA = visual.LARGURA, visual.LARGURA, visual.ALTURA
LIMIAR_ARRASTO_PX = 5        # ate' 5 px de tremida o clique continua clique

CORES = {estado: {"mic": cor, "fundo": visual.FUNDO}
         for estado, cor in visual.CORES.items()}
ESTADOS = tuple(CORES)

BTN_ORDER = visual.BOTOES
BTN_SIMBOLO = {"textos": "≡", "formatar": "✦", "stream": "≈",
               "restart": "↻", "min": "−", "close": "×"}
BTN_W, BTN_H, BTN_GAP = visual.BTN_W, visual.BTN_H, visual.BTN_GAP
BTN_RESPIRO_ANTES_DO_X = visual.RESPIRO_X     # anti-clique-acidental


def arquivo_posicao() -> Path:
    return base_dados() / "pos.txt"


def ler_posicao():
    try:
        x, y = arquivo_posicao().read_text(encoding="utf-8").strip().split(",")
        return int(x), int(y)
    except Exception:
        return None


def gravar_posicao(x, y):
    """SO no mouseUp do arraste manual. O pulo de tela NUNCA grava."""
    try:
        arquivo_posicao().parent.mkdir(parents=True, exist_ok=True)
        arquivo_posicao().write_text(f"{int(x)},{int(y)}", encoding="utf-8")
    except OSError:
        pass


def posicao_visivel(x, y, w=L_EXPANDIDA, h=ALTURA):
    """A posicao gravada cai dentro de alguma tela? (a pilula ja sumiu
    por ter `panel_x` apontando pra um monitor antigo — mesmo modo de falha)."""
    l, t, r, b = winapi.area_de_trabalho()
    lc, tc, rc, bc = winapi.retangulo_do_monitor_do_cursor()
    for L, T, R, B in ((l, t, r, b), (lc, tc, rc, bc)):
        if L - w + 40 <= x <= R - 40 and T - 10 <= y <= B - 20:
            return True
    return False


def posicao_padrao(w=L_EXPANDIDA, h=ALTURA):
    l, t, r, b = winapi.area_de_trabalho()
    return r - w - 24, b - h - 24


def limitar_a_tela(x, y, w=L_EXPANDIDA, h=ALTURA):
    """O tamanho novo nao deixa os controles fora da borda da tela antiga."""
    for l, t, r, b in (winapi.retangulo_do_monitor_do_cursor(), winapi.area_de_trabalho()):
        if l <= x + w/2 <= r and t <= y + h/2 <= b:
            return max(l, min(x, r-w)), max(t, min(y, b-h))
    return posicao_padrao(w, h)


def arrastou(dx, dy) -> bool:
    """Mexeu mais que o limiar = arrasto (move a pilula; nao e' clique)."""
    return abs(dx) > LIMIAR_ARRASTO_PX or abs(dy) > LIMIAR_ARRASTO_PX


def alvo_do_clique(botao_mouse, x, y, arrastado=False):
    """Roteamento PURO do clique (x, y em pixels da pilula).

    - arrasto nunca e' clique (None);
    - DIREITO (3) em qualquer ponto da pilula: 'seletor';
    - esquerdo (1): um botao, 'mic' (o icone), 'seletor' (zona do nome) ou
      'fundo' (o resto da capsula: so fecha o seletor, se aberto)."""
    if arrastado:
        return None
    dentro = 0 <= x <= L_EXPANDIDA and 0 <= y <= ALTURA
    if botao_mouse == 3:
        return "seletor" if dentro else None
    alvo = visual.alvo_em(x, y)
    if alvo == "nome":
        return "seletor"
    return alvo or ("fundo" if dentro else None)


class Pilula:
    """Janela da pilula. Roda na thread do tkinter; a API publica e' chamada de
    qualquer thread via `_agendar` (o tkinter so aceita chamada da dona)."""

    def __init__(self, ao_clique=None):
        self.ao_clique = ao_clique or (lambda _: None)
        self.estado = "ocioso"
        self.nivel = 0.0
        self.mic_mudo = False
        self.segundos = 0
        self._escondido_por_usuario = False   # FLAG DE INTENCAO
        self._tentativas_sem_efeito = 0
        self._proxima_checagem = 0.0
        self._retrato_logado = False
        self.hwnd = None
        self.raiz = None
        self.canvas = None
        self._pronta = threading.Event()
        self._arrastando = None
        self._viva = True
        self._hover = None
        self._ultima_imagem = None
        self._alpha_real = True
        self._imagem_tk = None
        self._proximo_heartbeat = 0.0

    # -- ciclo de vida -------------------------------------------------
    def iniciar(self):
        """A janela nasce na thread UNICA do tkinter (ver `tkloop.py`)."""
        import tkloop
        if not tkloop.iniciar():
            return False
        try:
            tkloop.executar_e_esperar(self._montar, timeout=10.0)
        except Exception as e:
            log(f"pilula: nao subiu ({type(e).__name__})")
            return False
        self._pronta.set()
        log(f"pilula: visual atualizado (capsula, alpha={'nativo' if self._alpha_real else 'fallback'})")
        return True

    def _montar(self):
        import tkinter as tk
        import tkloop
        self.raiz = tk.Toplevel(tkloop.raiz())
        self.raiz.withdraw()
        self.raiz.overrideredirect(True)
        self.raiz.attributes("-topmost", True)
        pos = ler_posicao()
        if not pos or not posicao_visivel(*pos):
            if pos:
                log("pilula: posicao gravada esta fora das telas — recentrando")
            pos = posicao_padrao()
        pos = limitar_a_tela(*pos)
        self.raiz.geometry(f"{L_EXPANDIDA}x{ALTURA}+{pos[0]}+{pos[1]}")
        self.canvas = tk.Canvas(self.raiz, width=L_EXPANDIDA, height=ALTURA,
                                highlightthickness=0, bd=0,
                                bg=CORES["ocioso"]["fundo"])
        self.canvas.pack(fill="both", expand=True)
        self.raiz.update_idletasks()
        self.hwnd = int(self.raiz.winfo_id())
        # O HWND do tk widget pode ser filho; pega a janela de topo.
        topo = winapi.user32.GetAncestor(self.hwnd, 2)  # GA_ROOT
        if topo:
            self.hwnd = topo
        winapi.nao_ativavel(self.hwnd, topo=True)
        self.canvas.bind("<Button-1>", self._mouse_down)
        self.canvas.bind("<B1-Motion>", self._mouse_move)
        self.canvas.bind("<ButtonRelease-1>", self._mouse_up)
        self.canvas.bind("<ButtonRelease-3>", self._mouse_up_direito)
        self.canvas.bind("<Motion>", self._mouse_hover)
        self.canvas.bind("<Leave>", self._mouse_leave)
        self._desenhar()
        self.raiz.deiconify()
        winapi.nao_ativavel(self.hwnd, topo=True)
        self.raiz.after(50, self._tick)

    def _agendar(self, fn, *a):
        import tkloop
        return tkloop.executar(fn, *a)

    # -- heartbeat -----------------------------------------------------
    def _tick(self):
        if not self._viva or self.raiz is None:
            return
        try:
            agora = time.monotonic()
            if agora >= self._proximo_heartbeat:
                self._manter_visivel()
                self._proximo_heartbeat = agora + max(.6, float(
                    config.get("visibilidade_intervalo_s", 3.0) or 3.0))
        except Exception:
            pass
        try:
            if not self._escondido_por_usuario:
                self._desenhar()
        except Exception:
            pass
        try:
            ativo = self.estado in ("gravando", "iniciando", "transcrevendo", "polindo")
            self.raiz.after(50 if ativo else 200, self._tick)
        except Exception:
            pass

    def _manter_visivel(self):
        """Nao desfaz a INTENCAO do usuario. Reposiciona ANTES de mostrar."""
        if self._escondido_por_usuario:
            return
        if not config.get("sempre_na_frente", True):
            return
        if time.monotonic() < self._proxima_checagem:
            return
        visivel = bool(winapi.user32.IsWindowVisible(self.hwnd))
        encoberta = winapi.esta_encoberta(self.hwnd)
        if visivel and not encoberta:
            self._tentativas_sem_efeito = 0
            self._retrato_logado = False
            winapi.reassertar_topo(self.hwnd)
            return
        self._forcar_frente()
        self._tentativas_sem_efeito += 1
        if self._tentativas_sem_efeito >= 3:
            # BACKOFF 15 s -> 60 s -> 5 min, e UM retrato no log
            espacos = (15.0, 60.0, 300.0)
            i = min(self._tentativas_sem_efeito - 3, len(espacos) - 1)
            self._proxima_checagem = time.monotonic() + espacos[i]
            if not self._retrato_logado:
                self._retrato_logado = True
                log(f"pilula: nao volta a vista — retrato hwnd={self.hwnd} "
                    f"visivel={visivel} encoberta={encoberta} "
                    f"geom={self.raiz.winfo_geometry()} "
                    f"tentativas={self._tentativas_sem_efeito}")

    def _forcar_frente(self):
        """REPOSICIONA antes de mostrar: janela parada em coordenada de monitor
        que nao existe mais nao volta so com 'mostrar'."""
        try:
            x, y = self.raiz.winfo_x(), self.raiz.winfo_y()
            if not posicao_visivel(x, y):
                x, y = posicao_padrao()
            x, y = limitar_a_tela(x, y)
            self.raiz.geometry(f"{L_EXPANDIDA}x{ALTURA}+{x}+{y}")
            self.raiz.deiconify()
            winapi.nao_ativavel(self.hwnd, topo=True)
        except Exception:
            pass

    # -- API publica (thread-safe) --------------------------------------
    def trazer_pra_frente(self):
        """Chamado por TODO press do hotkey. Limpa a intencao de esconder."""
        self._escondido_por_usuario = False
        self._tentativas_sem_efeito = 0
        self._proxima_checagem = 0.0
        self._agendar(self._forcar_frente)

    def esconder(self, por_usuario=True):
        self._escondido_por_usuario = bool(por_usuario)
        seletor_mic.fechar_se_aberto("pilula escondida")
        self._agendar(lambda: self.raiz.withdraw())

    def definir_estado(self, estado, nivel=None, mic_mudo=None, segundos=None):
        if estado in CORES:
            self.estado = estado
        if nivel is not None:
            self.nivel = max(0.0, min(1.0, float(nivel)))
        if mic_mudo is not None:
            self.mic_mudo = bool(mic_mudo)
        if segundos is not None:
            self.segundos = int(segundos)
        self._agendar(self._desenhar)

    def encerrar(self):
        """Destroi a janela DENTRO da thread dona do tkinter.

        (Destruir de outra thread e' o que dava
        "Tcl_AsyncDelete: async handler deleted by the wrong thread".)
        """
        self._viva = False
        seletor_mic.fechar_se_aberto("app saindo")
        pronto = threading.Event()

        def _fim():
            try:
                if self.raiz is not None:
                    self.raiz.destroy()
            except Exception:
                pass
            # Inclusive ImageTk: o ultimo ref do Tcl deve sair na thread dona.
            self._imagem_tk = None
            self.canvas = None
            self.raiz = None
            pronto.set()
        if self._agendar(_fim):
            pronto.wait(timeout=3.0)

    # -- desenho --------------------------------------------------------
    def _botoes_x(self):
        """Posicao x de cada botao, com o respiro extra antes do ×."""
        return visual.botoes_x()

    def _desenhar(self):
        if not self.canvas:
            return
        fase = time.monotonic()
        animado = self.estado in ("iniciando", "transcrevendo", "polindo")
        tecla = config.get("hotkey_ditar_nome", "") or "Alt direito"
        # O mic que vale no proximo ditado (so config: nunca o PortAudio aqui).
        try:
            mic = seletor_mic.rotulo_mic_ativo()
        except Exception:
            mic = ""
        detalhe = f"{tecla} · {mic}" if mic else tecla
        chave = (self.estado, round(self.nivel, 2), self.segundos, self.mic_mudo,
                 bool(config.get("manual_mic_nome", "")), detalhe, self._hover,
                 int(fase*12) if animado else 0)
        if chave == self._ultima_imagem:
            return
        img = visual.desenhar_pilula(self.estado, self.nivel, self.segundos,
            self.mic_mudo, chave[4], detalhe, self._hover, fase)
        if self._alpha_real:
            try:
                visual.apresentar(self.hwnd, img)
            except Exception as e:
                self._alpha_real = False
                log(f"pilula: compositor alpha indisponivel ({type(e).__name__})")
                # Fallback simples: mantem a barra utilizavel sem alpha nativo.
                winapi._SETLONG(self.hwnd, winapi.GWL_EXSTYLE,
                    winapi._GETLONG(self.hwnd, winapi.GWL_EXSTYLE) & ~winapi.WS_EX_LAYERED)
        if not self._alpha_real:
            from PIL import ImageTk
            self._imagem_tk = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self._imagem_tk)
        self._ultima_imagem = chave

    # -- mouse -----------------------------------------------------------
    def _botao_em(self, x, y):
        return visual.botao_em(x, y)

    def _mouse_hover(self, ev):
        hover = visual.alvo_em(ev.x, ev.y)        # botao | 'mic' | 'nome' | None
        if self._hover != hover:
            self._hover = hover
            self.canvas.configure(cursor="hand2" if hover else "fleur")
            self._desenhar()

    def _mouse_leave(self, ev=None):
        self._hover = None
        self._desenhar()

    def _mouse_down(self, ev):
        self._arrastando = (ev.x_root, ev.y_root, self.raiz.winfo_x(),
                            self.raiz.winfo_y(), False)

    def _mouse_move(self, ev):
        if not self._arrastando:
            return
        x0, y0, wx, wy, ja_arrastando = self._arrastando
        dx, dy = ev.x_root - x0, ev.y_root - y0
        # passou do limiar uma vez, segue o mouse ate' soltar
        if ja_arrastando or arrastou(dx, dy):
            self.raiz.geometry(f"+{wx + dx}+{wy + dy}")
            self._arrastando = (x0, y0, wx, wy, True)

    def _mouse_up(self, ev):
        arr = self._arrastando
        self._arrastando = None
        arrastado = bool(arr and arr[4])
        if arrastado:
            gravar_posicao(self.raiz.winfo_x(), self.raiz.winfo_y())
        alvo = alvo_do_clique(1, ev.x, ev.y, arrastado)
        if alvo:
            self.ao_clique(alvo)

    def _mouse_up_direito(self, ev):
        """Clique DIREITO em qualquer ponto: abre/fecha o seletor de microfone."""
        alvo = alvo_do_clique(3, ev.x, ev.y)
        if alvo:
            self.ao_clique(alvo)
