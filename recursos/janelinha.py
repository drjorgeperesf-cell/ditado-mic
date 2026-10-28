# -*- coding: utf-8 -*-
"""Janelinha "Textos ditados" (botao ≡) + botao Limpar.

- ultimos 200 ditados agrupados por dia ("Hoje"/"Ontem"/"sab 29/08");
- **corte duro de 300 chars ANTES de medir** o layout (ditado de 20 min nao
  pode virar 4000 chars pro medidor); o texto INTEIRO vai na copia;
- clique no item COPIA (checa o retorno da escrita) e mostra "copiado" por
  1,4 s; item com versao formatada ganha badge "formatado" e o clique passa a
  copiar o FORMATADO;
- leitura do banco em thread daemon, `mode=ro` + `PRAGMA query_only=ON`: **o
  ditado nunca espera pelo banco**;
- janela criada SOB DEMANDA a cada abertura e destruida ao fechar (nunca fica
  caixa orfa); NOACTIVATE, nunca rouba o teclado do editor;
- os TRES caminhos de esconder o painel fecham a janelinha junto.

BOTAO LIMPAR — maquina de 3 estados `normal | confirma | limpando`:
a confirmacao e' DESENHADA DENTRO da janelinha e `limpar` so roda a partir de
`confirma`. **Sem o clique em "Sim, apagar" nada e' apagado, e NENHUMA TECLA
alcanca o Sim** (Esc so cancela). Fechar a janelinha zera o estado.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import clipboard
import config
import historico
import winapi
import visual
from log import log

LARGURA, ALTURA = 420, 520
MAX_CHARS = 300
MAX_LINHAS = 3
COPIADO_S = 1.4
CONF_MSG = "Apagar TODOS os ditados e áudios? Sem volta."

FUNDO = "#15161c"
SUPERFICIE = "#22232b"
SUPERFICIE_HOVER = "#2b2d38"
TEXTO = "#f5f5f7"
SECUNDARIO = "#9698a5"
ACENTO = "#0a84ff"


def preview(txt, medir, largura, max_linhas=MAX_LINHAS):
    """Mede no maximo 300 caracteres, limitando o card a tres linhas.

    O conteudo completo continua no alvo da copia. `medir` e' injetado para
    que o corte seja verificavel sem abrir janela nem tocar no clipboard.
    """
    restante = resumir(txt)
    linhas = []
    for _ in range(max_linhas):
        if not restante:
            break
        ultimo = len(linhas) == max_linhas - 1
        if medir(restante) <= largura:
            linhas.append(restante)
            break
        sufixo = "…" if ultimo else ""
        # Busca binaria: 200 cards nao devem fazer dezenas de milhares de
        # consultas a fonte na thread da interface.
        inicio, limite, fim = 1, len(restante), 0
        while inicio <= limite:
            meio = (inicio + limite) // 2
            if medir(restante[:meio].rstrip() + sufixo) <= largura:
                fim = meio
                inicio = meio + 1
            else:
                limite = meio - 1
        if fim <= 0:
            linhas.append("…")
            break
        corte = restante.rfind(" ", 0, fim + 1)
        if corte > 0:
            fim = corte
        linhas.append(restante[:fim].rstrip() + sufixo)
        if ultimo:
            break
        restante = restante[fim:].lstrip()
    return "\n".join(linhas)


def rotulo_duracao(valor):
    try:
        segundos = max(0, int(float(valor or 0)))
    except (ValueError, TypeError, OverflowError):
        return ""
    if not segundos:
        return ""
    if segundos < 60:
        return f"{segundos} s"
    minutos, segundos = divmod(segundos, 60)
    return f"{minutos} min" + (f" {segundos:02d} s" if segundos else "")


def _retangulo_redondo(canvas, x1, y1, x2, y2, raio=14, **kw):
    return canvas.create_polygon(
        x1 + raio, y1, x2 - raio, y1, x2, y1, x2, y1 + raio,
        x2, y2 - raio, x2, y2, x2 - raio, y2, x1 + raio, y2,
        x1, y2, x1, y2 - raio, x1, y1 + raio, x1, y1,
        smooth=True, splinesteps=24, **kw)

_JANELA = {"obj": None}


def rotulo_do_dia(ts) -> str:
    if not ts:
        return "Sem data"
    d = datetime.fromtimestamp(ts).date()
    hoje = datetime.now().date()
    if d == hoje:
        return "Hoje"
    if d == hoje - timedelta(days=1):
        return "Ontem"
    dias = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
    return f"{dias[d.weekday()]} {d.strftime('%d/%m')}"


def agrupar(itens):
    """Agrupa por dia preservando a ordem; "Sem data" vai pro FIM."""
    grupos, ordem, sem_data = {}, [], []
    for it in itens:
        if not it.get("ts"):
            sem_data.append(it)
            continue
        r = rotulo_do_dia(it["ts"])
        if r not in grupos:
            grupos[r] = []
            ordem.append(r)
        grupos[r].append(it)
    saida = [(r, grupos[r]) for r in ordem]
    if sem_data:
        saida.append(("Sem data", sem_data))
    return saida


def resumir(txt: str) -> str:
    """Corte duro ANTES de medir."""
    t = " ".join((txt or "").split())
    return t[:MAX_CHARS] + ("…" if len(t) > MAX_CHARS else "")


class Janelinha:
    def __init__(self, app):
        self.app = app
        self.estado_limpar = "normal"     # normal | confirma | limpando
        self.itens = []
        self.raiz = None
        self.hwnd = 0
        self._carregando = True
        self._fechada = False
        self._canvas = None
        self._barra = None
        self.corpo = None
        self._subtitulo = None
        self._fonte_preview = None
        self._depois = set()
        self._pronta = threading.Event()

    def abrir(self):
        """Nasce SOB DEMANDA, na thread UNICA do tkinter (ver `tkloop.py`) —
        nunca um `Tk()` proprio: dois interpretadores Tcl em threads
        diferentes derrubam o processo na saida."""
        import tkloop
        if not tkloop.iniciar():
            return False
        try:
            tkloop.executar_e_esperar(self._montar_janela, timeout=8.0)
        except Exception as e:
            log(f"janelinha: nao subiu ({type(e).__name__})")
            self.fechar()
            return False
        self._pronta.set()
        threading.Thread(target=self._carregar, daemon=True).start()
        return True

    def _montar_janela(self):
        import tkinter as tk
        import tkinter.font as tkfont
        import tkloop
        self.raiz = tk.Toplevel(tkloop.raiz())
        self.raiz.withdraw()
        self.raiz.overrideredirect(True)
        self.raiz.attributes("-topmost", True)
        x, y = self._perto_da_pilula()
        self.raiz.geometry(f"{LARGURA}x{ALTURA}+{x}+{y}")
        self.raiz.configure(bg=FUNDO)
        self.raiz.update_idletasks()
        self.hwnd = winapi.user32.GetAncestor(int(self.raiz.winfo_id()), 2) or \
            int(self.raiz.winfo_id())
        winapi.nao_ativavel(self.hwnd, topo=True)
        familias = set(tkfont.families(self.raiz))
        self._familia = ("Segoe UI Variable" if "Segoe UI Variable" in familias
                         else "Segoe UI")
        self._fonte_preview = tkfont.Font(root=self.raiz, family=self._familia,
                                          size=11)
        self._montar(tk)
        self.raiz.update_idletasks()
        visual.arredondar_janela(self.hwnd, self.raiz.winfo_width(),
                                self.raiz.winfo_height())
        self.raiz.deiconify()
        winapi.nao_ativavel(self.hwnd, topo=True)

    def _perto_da_pilula(self):
        l, t, r, b = winapi.area_de_trabalho()
        p = self.app.pilula
        if p is not None and p.raiz is not None:
            try:
                px, py = p.raiz.winfo_x(), p.raiz.winfo_y()
                x = max(l, min(px, r - LARGURA))
                acima = py - ALTURA - 12
                abaixo = py + p.raiz.winfo_height() + 8
                # Painel no topo abre o historico abaixo; nao cobre a capsula.
                y = (acima if acima >= t else abaixo if abaixo + ALTURA <= b
                     else max(t, min(acima, b - ALTURA)))
                return x, y
            except Exception:
                pass
        return r - LARGURA - 24, b - ALTURA - 90

    def _montar(self, tk):
        topo = tk.Frame(self.raiz, bg=FUNDO)
        topo.pack(fill="x", padx=24, pady=(23, 18))
        titulos = tk.Frame(topo, bg=FUNDO)
        titulos.pack(side="left", fill="x", expand=True)
        tk.Label(titulos, text="Textos", bg=FUNDO, fg=TEXTO, anchor="w",
                 font=(self._familia, 21, "bold")).pack(fill="x")
        self._subtitulo = tk.Label(titulos, text="Seus ditados, sempre à mão.",
                                   bg=FUNDO, fg=SECUNDARIO, anchor="w",
                                   font=(self._familia, 10))
        self._subtitulo.pack(fill="x", pady=(4, 0))
        self._botao(tk, topo, "Fechar", self.fechar,
                    fg=SECUNDARIO).pack(side="right", anchor="n", pady=(8, 0))
        self.corpo = tk.Frame(self.raiz, bg=FUNDO)
        self.corpo.pack(fill="both", expand=True)
        rodape = tk.Frame(self.raiz, bg=FUNDO)
        rodape.pack(fill="x")
        tk.Frame(rodape, bg="#292b35", height=1).pack(fill="x", padx=24)
        acoes = tk.Frame(rodape, bg=FUNDO)
        acoes.pack(fill="x", padx=24, pady=(12, 16))
        tk.Label(acoes, text="Clique em um texto para copiar", bg=FUNDO,
                 fg=SECUNDARIO, font=(self._familia, 9)).pack(side="left")
        self._botao(tk, acoes, "Limpar", self._limpar_clicado,
                    fg=SECUNDARIO, hover="#ff6961").pack(side="right")
        self.raiz.bind("<Escape>", lambda e: self._escape())
        self.raiz.bind("<MouseWheel>", self._rolar_roda)
        self._redesenhar()

    def _botao(self, tk, pai, texto, acao, fg=ACENTO, hover=TEXTO, bg=FUNDO):
        """Acao de mouse sem bordas classicas nem foco de teclado."""
        lb = tk.Label(pai, text=texto, bg=bg, fg=fg, cursor="hand2",
                      font=(self._familia, 10), padx=1, pady=2)
        lb.bind("<Button-1>", lambda _e: acao())
        lb.bind("<Enter>", lambda _e: lb.configure(fg=hover))
        lb.bind("<Leave>", lambda _e: lb.configure(fg=fg))
        return lb

    def _escape(self):
        """Esc SO cancela a confirmacao — nunca alcanca o "Sim, apagar"."""
        if self.estado_limpar == "confirma":
            self.estado_limpar = "normal"
            self._redesenhar()
        else:
            self.fechar()

    def _carregar(self):
        try:
            itens = historico.ultimos(config.get("textos_limite", 200))
        except Exception as e:
            log(f"janelinha: leitura do banco falhou ({type(e).__name__})")
            itens = []
        import tkloop
        def pronto():
            if self._fechada:
                return
            self.itens = itens
            self._carregando = False
            self._redesenhar()
        tkloop.executar(pronto)

    def _redesenhar(self):
        import tkinter as tk
        if self.raiz is None or self._fechada:
            return
        posicao = self._canvas.yview()[0] if self._canvas is not None else 0.0
        self._canvas = self._barra = None
        for w in self.corpo.winfo_children():
            w.destroy()
        total = len(self.itens)
        self._subtitulo.configure(text=("Seus ditados, sempre à mão."
            if not total else f"{total} ditado{'s' if total != 1 else ''} no histórico"))
        if self.estado_limpar == "confirma":
            cx = tk.Frame(self.corpo, bg="#2c2026")
            cx.pack(fill="x", padx=24, pady=(0, 12))
            tk.Label(cx, text="Apagar histórico?", bg="#2c2026", fg=TEXTO,
                     anchor="w", font=(self._familia, 11, "bold")).pack(
                         fill="x", padx=16, pady=(14, 5))
            tk.Label(cx, text=CONF_MSG, bg="#2c2026", fg="#d6afb7",
                     wraplength=320, justify="left", anchor="w",
                     font=(self._familia, 10)).pack(fill="x", padx=16)
            b = tk.Frame(cx, bg="#2c2026")
            b.pack(fill="x", padx=16, pady=(12, 13))
            self._botao(tk, b, "Sim, apagar", self._limpar_confirmar,
                        fg="#ff6961", bg="#2c2026").pack(side="right")
            self._botao(tk, b, "Cancelar", self._limpar_cancelar,
                        fg=SECUNDARIO, bg="#2c2026").pack(side="right", padx=18)
        elif self.estado_limpar == "limpando":
            tk.Label(self.corpo, text="Apagando histórico…", bg=FUNDO,
                     fg=SECUNDARIO, font=(self._familia, 10)).pack(pady=16)
        if not self.itens:
            self._vazio(tk)
            return
        cont = tk.Frame(self.corpo, bg=FUNDO)
        cont.pack(fill="both", expand=True, padx=(24, 15), pady=(0, 14))
        self._barra = tk.Canvas(cont, width=5, bg=FUNDO, highlightthickness=0,
                                 bd=0, cursor="arrow")
        self._barra.pack(side="right", fill="y", padx=(7, 0))
        self._canvas = tk.Canvas(cont, bg=FUNDO, highlightthickness=0, bd=0,
                                  yscrollincrement=20)
        self._canvas.pack(side="left", fill="both", expand=True)
        lista = tk.Frame(self._canvas, bg=FUNDO)
        janela = self._canvas.create_window((0, 0), window=lista, anchor="nw")
        self._canvas.configure(yscrollcommand=self._atualizar_barra)
        lista.bind("<Configure>", lambda _e: self._canvas and
                   self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas and
                          self._canvas.itemconfigure(janela, width=e.width))
        self._barra.bind("<Button-1>", self._arrastar_barra)
        self._barra.bind("<B1-Motion>", self._arrastar_barra)
        for rotulo, grupo in agrupar(self.itens):
            tk.Label(lista, text=rotulo, bg=FUNDO, fg=SECUNDARIO,
                     font=(self._familia, 10, "bold"), anchor="w").pack(
                         fill="x", padx=2, pady=(10, 9))
            for it in grupo:
                self._item(tk, lista, it)
        self.raiz.update_idletasks()
        self._canvas.yview_moveto(posicao)

    def _vazio(self, tk):
        cx = tk.Frame(self.corpo, bg=FUNDO)
        cx.pack(fill="both", expand=True, padx=40, pady=20)
        meio = tk.Frame(cx, bg=FUNDO)
        meio.place(relx=0.5, rely=0.42, anchor="center")
        tk.Label(meio, text="···", bg=FUNDO, fg=ACENTO,
                 font=(self._familia, 34, "bold")).pack(pady=(0, 13))
        tk.Label(meio, text=("Carregando seus textos…" if self._carregando else
                            "Sua voz vira texto."), bg=FUNDO, fg=TEXTO,
                 font=(self._familia, 16, "bold")).pack()
        tk.Label(meio, text=("" if self._carregando else
                            "Seus próximos ditados aparecem aqui."),
                 bg=FUNDO, fg=SECUNDARIO,
                 font=(self._familia, 10)).pack(pady=(9, 0))

    def _rolar_roda(self, evento):
        if self._canvas is not None and evento.delta:
            unidades = max(1, abs(int(evento.delta)) // 120)
            self._canvas.yview_scroll(-unidades if evento.delta > 0 else unidades,
                                      "units")
        return "break"

    def _atualizar_barra(self, inicio, fim):
        if self._barra is None:
            return
        inicio, fim = float(inicio), float(fim)
        self._barra.delete("all")
        altura = self._barra.winfo_height()
        if fim - inicio >= 0.999 or altura <= 1:
            return
        topo = max(3, int(inicio * altura))
        base = min(altura - 3, max(topo + 24, int(fim * altura)))
        _retangulo_redondo(self._barra, 0, topo, 5, base, raio=2,
                           fill="#555763", outline="")

    def _arrastar_barra(self, evento):
        if self._canvas is not None:
            altura = max(1, self._barra.winfo_height())
            inicio, fim = self._canvas.yview()
            self._canvas.yview_moveto(evento.y / altura - (fim - inicio) / 2)

    def _item(self, tk, pai, it):
        hora = (datetime.fromtimestamp(it["ts"]).strftime("%H:%M")
                if it.get("ts") else "--:--")
        tem_fmt = bool(it.get("formatado"))
        alvo = it.get("formatado") if tem_fmt else it.get("texto")
        largura = LARGURA - 60
        texto = preview(alvo, self._fonte_preview.measure, largura - 36)
        linhas = max(1, texto.count("\n") + 1)
        altura = 48 + linhas * int(self._fonte_preview.metrics("linespace"))
        card = tk.Canvas(pai, width=largura, height=altura, bg=FUNDO,
                         highlightthickness=0, bd=0, cursor="hand2")
        card.pack(fill="x", pady=(0, 8))
        fundo = _retangulo_redondo(card, 0, 0, largura, altura, raio=16,
                                   fill=SUPERFICIE, outline="")
        card.create_text(18, 18, text=hora, fill=SECUNDARIO, anchor="w",
                         font=(self._familia, 9))
        duracao = rotulo_duracao(it.get("duracao"))
        if duracao:
            card.create_text(largura - 18, 18, text=duracao, fill=SECUNDARIO,
                             anchor="e", font=(self._familia, 9))
        if tem_fmt:
            _retangulo_redondo(card, 67, 7, 143, 29, raio=7,
                               fill="#143350", outline="")
            card.create_text(105, 18, text="Formatado", fill="#70b9ff",
                             font=(self._familia, 8), anchor="center")
        card.create_text(18, 36, text=texto, fill=TEXTO, anchor="nw",
                         font=self._fonte_preview)

        copias = {"n": 0}
        def copiar(_ev=None, alvo=alvo):
            ok = clipboard.escrever_texto(alvo or "")
            copias["n"] += 1
            numero = copias["n"]
            card.delete("copia")
            card.create_rectangle(0, 0, largura, altura, fill=FUNDO,
                                   outline="", tags="copia")
            _retangulo_redondo(card, 0, 0, largura, altura, raio=16,
                               fill=SUPERFICIE_HOVER, outline="", tags="copia")
            card.create_text(largura / 2, altura / 2,
                             text="Copiado" if ok else "Não foi possível copiar",
                             fill=ACENTO if ok else "#ff6961",
                             font=(self._familia, 11), tags="copia")
            self._agendar(int(COPIADO_S * 1000), lambda:
                copias["n"] == numero and card.winfo_exists() and
                card.delete("copia"))
        card.bind("<Button-1>", copiar)
        card.bind("<Enter>", lambda _e: card.itemconfigure(fundo,
                                                          fill=SUPERFICIE_HOVER))
        card.bind("<Leave>", lambda _e: card.itemconfigure(fundo, fill=SUPERFICIE))

    def _agendar(self, espera, callback):
        pendente = {}
        def pronto():
            self._depois.discard(pendente["id"])
            if not self._fechada:
                callback()
        pendente["id"] = self.raiz.after(espera, pronto)
        self._depois.add(pendente["id"])

    # -- botao Limpar ---------------------------------------------------
    def _limpar_clicado(self):
        if self.estado_limpar == "limpando":
            return
        self.estado_limpar = "confirma"
        self._redesenhar()

    def _limpar_cancelar(self):
        self.estado_limpar = "normal"
        self._redesenhar()

    def _limpar_confirmar(self):
        """SO roda a partir de `confirma`."""
        if self.estado_limpar != "confirma":
            log("limpar: recusado — estado nao era 'confirma'")
            return
        self.estado_limpar = "limpando"
        self._redesenhar()

        def tarefa():
            try:
                r = historico.limpar_tudo(confirmado=True)
            except Exception as e:
                log(f"limpar: falhou ({type(e).__name__})")
                r = {"ok": False, "banco": False, "arquivos": False}
            itens = ([] if r["banco"] else
                     historico.ultimos(config.get("textos_limite", 200)))
            msg = ("Limpo." if r["ok"] else
                   f"Banco: {'ok' if r['banco'] else 'falhou'} · "
                   f"Arquivos: {'ok' if r['arquivos'] else 'falhou'}")
            log(f"limpar: {msg}")
            import tkloop
            def pronto():
                if self._fechada:
                    return
                self.itens = itens
                self.estado_limpar = "normal"
                self._redesenhar()
            tkloop.executar(pronto)
        threading.Thread(target=tarefa, daemon=True).start()

    def fechar(self):
        self.estado_limpar = "normal"
        self._fechada = True
        import tkloop
        pronto = threading.Event()

        def _fim():
            try:
                if self.raiz is not None:
                    for tarefa in self._depois:
                        self.raiz.after_cancel(tarefa)
                    self._depois.clear()
                    self.raiz.destroy()
            except Exception:
                pass
            # Font e widgets guardam o interpretador Tcl mesmo depois de
            # destroy(). A ultima referencia tambem deve morrer na dona.
            self._fonte_preview = None
            self._subtitulo = None
            self.corpo = None
            self.raiz = None
            self.hwnd = 0
            self._canvas = self._barra = None
            pronto.set()
        if tkloop.executar(_fim):
            pronto.wait(timeout=3.0)
        _JANELA["obj"] = None


def alternar(app):
    j = _JANELA.get("obj")
    if j is not None:
        j.fechar()
        return None
    j = Janelinha(app)
    _JANELA["obj"] = j
    if not j.abrir():
        _JANELA["obj"] = None
        return None
    return j


def fechar_se_aberta():
    """Os TRES caminhos de esconder o painel chamam isto."""
    j = _JANELA.get("obj")
    if j is not None:
        j.fechar()
