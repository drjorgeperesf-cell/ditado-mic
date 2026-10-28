# -*- coding: utf-8 -*-
"""Seletor de microfone na pilula (pedido do usuário, 11/10/2026: "quero clicar
e conseguir selecionar qual microfone, manualmente tambem").

COMO ABRE — so por clique dele, NUNCA sozinho:
- clique DIREITO em qualquer ponto da pilula;
- clique esquerdo SEM arrasto (limiar de 5 px) na zona do nome.
O unico caminho e' `_clique_da_pilula(app, "seletor")` -> `alternar()`
(`testes/teste_seletor_mic.py` confere por AST: nenhum outro ponto do codigo
chama `alternar` nem cria um `SeletorMic`).

O QUE MOSTRA: "Automatico (fifine)" com "recomendado"; UM item por APARELHO
FISICO (`captura.agrupar_aparelhos`: o Windows lista o mesmo mic 3-4 vezes, um
por driver); o Bluetooth CINZA ("Bluetooth — nao usar": usar o fone como mic
forca o perfil de chamada); e "Atualizar lista". O item marcado e' o que vale
no proximo ditado (a mesma regra de `escolher_dispositivo`).

ESCOLHER: uma thread daemon grava a CHAVE normalizada em `manual_mic_nome`
(vale no PROXIMO Alt direito: o app relê o config a cada gravacao) e, se o app
estava em ERRO, ele volta a ocioso pela fila do app. O menu da bandeja nao
precisa de aviso: ele e' remontado no clique direito, antes de abrir.

O QUE NUNCA FAZ:
- roubar o foco: WS_EX_NOACTIVATE antes e depois de aparecer (sem isso o
  Ctrl+V do proximo ditado colaria no lugar errado); so um Canvas; nenhum
  pedido de foco nem de captura de teclado/mouse; nenhum menu nativo do Tk
  (ha varredura do fonte por essas chamadas);
- som ou notificacao;
- ler a lista do PortAudio na thread do Tk (thread daemon, lista em memoria);
- recarregar a lista: "Atualizar lista" so ENFILEIRA o pedido na fila do app
  (acoes-teclado), que so recarrega com o app ocioso ou em erro.

FECHA: ao escolher; clique na pilula; Alt direito; 12 s sem o mouse por perto;
clique fora; pilula escondida ou arrastada. O "clique fora" e' vigiado com
GetAsyncKeyState + GetCursorPos a cada 100 ms, na thread do Tk — sem gancho
novo de mouse.
"""
from __future__ import annotations

import ctypes
import re
import threading
import time
from ctypes import wintypes

import captura
import config
import winapi
from log import log

# ── geometria (pixels logicos do Tk) ─────────────────────────────────────
LARGURA = 300
TOPO = 56                    # titulo + subtitulo
ALTURA_ITEM = 34             # so o nome
ALTURA_ITEM_DUPLA = 46       # nome + detalhe embaixo ("Bluetooth — não usar")
ALTURA_SEPARADOR = 11
ALTURA_STATUS = 22
RODAPE = 8
RAIO = 16
MARGEM_TEXTO = 42            # x do nome (o radio fica em x=26)

VIGIA_MS = 100               # GetAsyncKeyState + GetCursorPos, sem gancho novo
FECHAR_SEM_USO_S = 12.0      # 12 s sem o mouse no seletor (nem na pilula)
ATUALIZAR_TETO_S = 4.0       # a fila do app nao respondeu: desiste calado

# ── cores (a paleta da janelinha ≡) ──────────────────────────────────────
FUNDO = "#15161c"
HOVER = "#2b2d38"
TEXTO = "#f5f5f7"
SECUNDARIO = "#9698a5"
APAGADO = "#5c5e6a"
ACENTO = "#0a84ff"
ANEL = "#6a6c7a"
ANEL_APAGADO = "#3f414b"
LINHA = "#292b35"

TITULO = "Microfone"
SUBTITULO = "Vale a partir do próximo ditado"
DETALHE_BT = "Bluetooth — não usar"
ROTULO_ATUALIZAR = "Atualizar lista"
VK_BOTOES_DO_MOUSE = (0x01, 0x02, 0x04)      # esquerdo, direito, meio

_LOCK = threading.Lock()
_ATUAL = {"obj": None}           # o seletor aberto (no maximo um)
_ULTIMOS_MICS = {"mics": None}   # ultima lista lida (abre na hora; atualiza atras)
_NOMES: dict = {}                # chave -> nome mais longo ja visto
_FAMILIA = {"nome": None}

_RE_APARELHO = re.compile(r"^[^()]*\((.*?)\)?\s*$")   # "Tipo (Aparelho)"
_RE_NUMERO = re.compile(r"^\d+-\s*")                  # o "2- " da reenumeracao USB
_GENERICOS = ("microphone", "microfone", "mic")


# ════════════════════════════════════════════════════════════════════
# Logica PURA (testavel sem janela e sem PortAudio)
def rotulo_curto(nome) -> str:
    """'Microfone (2- fifine Microphone)' -> 'fifine Microphone'. O que esta
    entre os parenteses e' o aparelho; sem o 'N- ' da reenumeracao USB. Vale
    para o nome cortado do MME (sem o ')') e para a chave ja normalizada."""
    limpo = " ".join(str(nome or "").split())
    s = limpo
    m = _RE_APARELHO.match(s)
    if m and m.group(1).strip():
        s = m.group(1)
    s = _RE_NUMERO.sub("", s.strip()).strip()
    return s or limpo


def rotulo_compacto(nome) -> str:
    """Para o subtitulo da pilula: 'fifine Microphone' -> 'fifine'."""
    palavras = rotulo_curto(nome).split()
    while len(palavras) > 1 and palavras[-1].casefold() in _GENERICOS:
        palavras.pop()
    return " ".join(palavras)


def _habilitado(aparelho) -> bool:
    """Bluetooth NUNCA e' escolhido por aqui: usar o fone como mic forca o
    perfil de chamada (o audio estraga e o fone para de tocar). Quem quiser
    mesmo assim usa o CONFIGURAR-MIC, que pede SIM."""
    return not aparelho.get("bluetooth")


def itens_do_seletor(mics) -> list[dict]:
    """Os itens de escolha (pilula E bandeja), a partir de uma lista JA LIDA.

    Cada item: tipo ('auto' | 'mic' | 'ausente'), chave (o que se grava em
    manual_mic_nome; '' = automatico), nome, rotulo, detalhe, habilitado,
    marcado. Marcado = o que vale no proximo ditado: o automatico, ou o
    aparelho que `escolher_dispositivo` abriria AGORA com a escolha dele."""
    manual = (config.get("manual_mic_nome", "") or "").strip()
    preferido = (config.get("mic_preferido_nome", "") or "").strip()
    aparelhos = captura.agrupar_aparelhos(mics)
    for a in aparelhos:
        _NOMES[a["chave"]] = a["nome"]
    try:
        idx = captura.escolher_dispositivo(mics)
    except Exception:            # MicrofoneIndisponivel: a escolha nao esta na lista
        idx = None
    if idx is None and not manual and not preferido:
        ativo = next((a for a in aparelhos if a["padrao"]), None)   # padrao do Windows
    else:
        ativo = next((a for a in aparelhos
                      if any(e["idx"] == idx for e in a["entradas"])), None)
    if preferido:
        rotulo_auto = f"Automático ({preferido.casefold()})"
        detalhe_auto = ("não encontrado agora" if not manual and ativo is None
                        else "recomendado")
    else:
        rotulo_auto, detalhe_auto = "Automático (padrão do Windows)", ""
    itens = [{"tipo": "auto", "chave": "", "nome": "", "rotulo": rotulo_auto,
              "detalhe": detalhe_auto, "habilitado": True, "marcado": not manual}]
    curtos = [rotulo_curto(a["nome"]) for a in aparelhos]
    for a, curto in zip(aparelhos, curtos):
        habilitado = _habilitado(a)
        if not habilitado:
            detalhe = DETALHE_BT
        elif not manual and a is ativo:
            detalhe = "em uso"
        elif a["virtual"]:
            detalhe = "virtual"
        else:
            detalhe = ""
        itens.append({
            "tipo": "mic", "chave": a["chave"], "nome": a["nome"],
            # dois aparelhos com o mesmo rotulo curto: mostra o nome inteiro
            "rotulo": curto if curtos.count(curto) == 1 else a["nome"],
            "detalhe": detalhe, "habilitado": habilitado,
            "marcado": bool(manual) and a is ativo})
    if manual and ativo is None:
        # a escolha dele nao esta na lista agora: aparece marcada e cinza
        chave = captura.chave_mic(manual)
        itens.append({"tipo": "ausente", "chave": chave, "nome": manual,
                      "rotulo": rotulo_curto(_NOMES.get(chave) or manual),
                      "detalhe": "não encontrado agora", "habilitado": False,
                      "marcado": True})
    return itens


def rotulo_mic_ativo() -> str:
    """Nome curto do mic que vale no proximo ditado, para o subtitulo da
    pilula ('Alt direito · FIFINE'). SO config + nomes ja vistos: roda na
    thread do Tk a cada quadro, entao NUNCA consulta o PortAudio."""
    manual = (config.get("manual_mic_nome", "") or "").strip()
    if manual:
        return rotulo_compacto(_NOMES.get(captura.chave_mic(manual)) or manual)
    preferido = (config.get("mic_preferido_nome", "") or "").strip()
    return rotulo_compacto(preferido) if preferido else ""


def gravar_escolha(chave, app=None) -> bool:
    """Grava a escolha: a CHAVE normalizada ('' = automatico). Vale no proximo
    Alt direito. Se o app estava em ERRO, volta a ocioso (pela fila dele).
    Usada pela pilula (numa thread daemon) e pela bandeja."""
    chave = captura.chave_mic(chave) if chave else ""
    try:
        config.gravar({"manual_mic_nome": chave})
    except Exception as e:
        log(f"mic: escolha NAO gravada ({type(e).__name__})")
        return False
    log(f"mic: escolhido manualmente ({chave or 'automatico'})")
    if app is not None:
        try:
            app.limpar_erro()
        except Exception:
            pass
    return True


def motivo_para_fechar(agora, ultimo_uso, botao_apertado, no_seletor,
                       na_pilula, pilula_visivel=True, pilula_mexeu=False,
                       teto_s=FECHAR_SEM_USO_S):
    """Decisao PURA do vigia (a cada 100 ms). None = continua aberto."""
    if not pilula_visivel:
        return "pilula escondida"
    if pilula_mexeu:
        return "pilula arrastada"
    if botao_apertado and not no_seletor and not na_pilula:
        return "clique fora"
    if agora - ultimo_uso >= teto_s:
        return f"{teto_s:g} s sem uso"
    return None


def linhas(itens, status="") -> list[dict]:
    """Layout PURO: cada linha com y0/y1 e o que ela e'. itens=None = carregando."""
    out, y = [], TOPO

    def linha(tipo, altura, item=None):
        nonlocal y
        out.append({"tipo": tipo, "y0": y, "y1": y + altura, "item": item})
        y += altura
    if itens is None:
        linha("carregando", ALTURA_ITEM)
    else:
        for it in itens:
            linha(it["tipo"], ALTURA_ITEM_DUPLA if it.get("detalhe") else ALTURA_ITEM, it)
    linha("separador", ALTURA_SEPARADOR)
    linha("atualizar", ALTURA_ITEM)
    if status:
        linha("status", ALTURA_STATUS)
    return out


def altura(itens, status="") -> int:
    return linhas(itens, status)[-1]["y1"] + RODAPE


def interativa(ln) -> bool:
    if ln["tipo"] == "atualizar":
        return True
    it = ln.get("item")
    return bool(it) and ln["tipo"] in ("auto", "mic") and bool(it.get("habilitado"))


def elidir(texto, medir, largura) -> str:
    """Corta com reticencias para caber em `largura` (medir = Font.measure)."""
    if medir(texto) <= largura:
        return texto
    t = texto
    while t and medir(t + "…") > largura:
        t = t[:-1]
    return (t.rstrip() + "…") if t else "…"


# ════════════════════════════════════════════════════════════════════
# Windows (instancia PROPRIA do user32: os argtypes daqui nao vazam)
_U32 = None


def _u32():
    global _U32
    if _U32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        u.GetCursorPos.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.IsWindowVisible.argtypes = (wintypes.HWND,)
        u.IsWindowVisible.restype = wintypes.BOOL
        _U32 = u
    return _U32


def _cursor():
    p = wintypes.POINT()
    if not _u32().GetCursorPos(ctypes.byref(p)):
        return None
    return p.x, p.y


def _retangulo(hwnd):
    if not hwnd:
        return None
    r = wintypes.RECT()
    if not _u32().GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def _dentro(ponto, ret) -> bool:
    return (bool(ponto) and bool(ret) and ret[0] <= ponto[0] < ret[2]
            and ret[1] <= ponto[1] < ret[3])


def _botao_do_mouse_apertado() -> bool:
    """Algum botao do mouse apertado agora (0x8000) ou desde a ultima consulta
    (0x0001: so uma dica — por isso e' drenado quando o seletor abre)."""
    u = _u32()
    return any(u.GetAsyncKeyState(vk) & 0x8001 for vk in VK_BOTOES_DO_MOUSE)


def _retangulo_redondo(canvas, x1, y1, x2, y2, raio, **kw):
    return canvas.create_polygon(
        x1 + raio, y1, x2 - raio, y1, x2, y1, x2, y1 + raio,
        x2, y2 - raio, x2, y2, x2 - raio, y2, x1 + raio, y2,
        x1, y2, x1, y2 - raio, x1, y1 + raio, x1, y1,
        smooth=True, splinesteps=16, **kw)


def _familia(raiz) -> str:
    if _FAMILIA["nome"] is None:
        import tkinter.font as tkfont
        familias = set(tkfont.families(raiz))
        _FAMILIA["nome"] = ("Segoe UI Variable" if "Segoe UI Variable" in familias
                            else "Segoe UI")
    return _FAMILIA["nome"]


# ════════════════════════════════════════════════════════════════════
class SeletorMic:
    """A janelinha do seletor. Nasce SOB DEMANDA na thread UNICA do tkinter
    (`tkloop`) e e' destruida ao fechar (nunca fica caixa orfa).
    `mostrar=False` monta tudo sem aparecer (teste da janela oculta)."""

    def __init__(self, app, mostrar=True):
        self.app = app
        self.mostrar = mostrar
        self.raiz = None
        self.canvas = None
        self.hwnd = 0
        self.itens = None            # None = carregando
        self.status = ""
        self._linhas = []
        self._hover = None
        self._apertado = None
        self._atualizando = False
        self._fechada = False
        self._depois = set()
        self._fontes = {}
        self._ultimo_uso = time.monotonic()
        self._ret_pilula = None

    # -- ciclo de vida --------------------------------------------------
    def abrir(self) -> bool:
        import tkloop
        if not tkloop.iniciar():
            return False
        try:
            tkloop.executar_e_esperar(self._montar, timeout=5.0)
        except Exception as e:
            log(f"seletor: nao abriu ({type(e).__name__})")
            self.fechar()
            return False
        if self._fechada or self.raiz is None:
            return False
        log("seletor: aberto (clique na pilula)")
        self._carregar_em_thread()
        return True

    def _carregar_em_thread(self, status=None):
        """A lista e' lida FORA da thread do Tk: o _AUDIO_LOCK pode estar
        preso num stop() lento do driver, e a pilula nao pode congelar."""
        threading.Thread(target=self._carregar, args=(status,),
                         name="seletor-lista", daemon=True).start()

    def _carregar(self, status=None):
        try:
            mics = captura.listar_microfones(atualizar=False)
            _ULTIMOS_MICS["mics"] = mics
        except Exception as e:
            log(f"seletor: leitura da lista falhou ({type(e).__name__})")
            # sem lista, o "Automatico" continua escolhivel
            mics, status = [], "Não deu para ler a lista agora"
        try:
            itens = itens_do_seletor(mics)
        except Exception as e:
            log(f"seletor: itens ilegiveis ({type(e).__name__})")
            itens, status = None, "Não deu para ler a lista agora"
        import tkloop
        tkloop.executar(self._preencher, itens, status)

    def _montar(self):
        import tkinter as tk
        import tkinter.font as tkfont
        import tkloop
        if self._fechada:
            return            # fechado antes de nascer (Alt direito no meio)
        self.raiz = tk.Toplevel(tkloop.raiz())
        self.raiz.withdraw()
        self.raiz.overrideredirect(True)
        self.raiz.attributes("-topmost", True)
        self.raiz.configure(bg=FUNDO)
        fam = _familia(self.raiz)
        self._fontes = {
            "titulo": tkfont.Font(root=self.raiz, family=fam, size=11, weight="bold"),
            "normal": tkfont.Font(root=self.raiz, family=fam, size=10),
            "pequena": tkfont.Font(root=self.raiz, family=fam, size=9)}
        # abre na hora com a ultima lista conhecida; a leitura nova vem atras
        if _ULTIMOS_MICS["mics"] is not None:
            try:
                self.itens = itens_do_seletor(_ULTIMOS_MICS["mics"])
            except Exception:
                self.itens = None
        w, h = LARGURA, altura(self.itens, self.status)
        x, y = self._posicao(w, h)
        self.raiz.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas = tk.Canvas(self.raiz, width=w, height=h, bg=FUNDO,
                                highlightthickness=0, bd=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Motion>", self._mover)
        self.canvas.bind("<Leave>", self._sair)
        self.canvas.bind("<Button-1>", self._apertar)
        self.canvas.bind("<ButtonRelease-1>", self._soltar)
        self._desenhar()
        self.raiz.update_idletasks()
        self.hwnd = (winapi.user32.GetAncestor(int(self.raiz.winfo_id()), 2)
                     or int(self.raiz.winfo_id()))
        # ANTES de aparecer: sem NOACTIVATE, o clique nele tiraria o foco do
        # editor e o Ctrl+V do proximo ditado colaria no lugar errado
        winapi.nao_ativavel(self.hwnd, topo=True)
        self._arredondar(w, h)
        if not self.mostrar:
            return
        self.raiz.deiconify()
        winapi.nao_ativavel(self.hwnd, topo=True)          # e DEPOIS
        self._ret_pilula = _retangulo(self._hwnd_pilula())
        _botao_do_mouse_apertado()        # drena o "apertado desde a ultima consulta"
        self._ultimo_uso = time.monotonic()
        self._agendar(VIGIA_MS, self._vigiar)
        if winapi.janela_em_foco() == self.hwnd:
            # so diagnostico: nunca pegamos (nem devolvemos) foco de ninguem
            log("seletor: ATENCAO — o seletor virou a janela em foco")

    def _arredondar(self, w, h):
        """Cantos redondos sao so' acabamento: se falharem, a janela abre
        quadrada — nunca deixa de abrir por causa disso."""
        import visual
        try:
            visual.arredondar_janela(self.hwnd, w, h, raio=RAIO)
        except Exception as e:
            log(f"seletor: cantos sem arredondar ({type(e).__name__})")

    def _hwnd_pilula(self):
        p = getattr(self.app, "pilula", None)
        return getattr(p, "hwnd", None) if p is not None else None

    def _posicao(self, w, h):
        """Acima da pilula (ou abaixo, se nao couber), alinhado a capsula."""
        if not self.mostrar:
            return -10000, -10000          # teste da janela oculta: fora da tela
        try:
            l, t, r, b = winapi.retangulo_do_monitor_do_cursor()
        except Exception:
            l, t, r, b = winapi.area_de_trabalho()
        p = getattr(self.app, "pilula", None)
        raiz_p = getattr(p, "raiz", None) if p is not None else None
        if raiz_p is not None:
            try:
                px, py = raiz_p.winfo_x(), raiz_p.winfo_y()
            except Exception:
                px = py = None
            if px is not None:
                x = px + 10                 # a capsula visivel comeca em x+10
                acima = py + 2 - h          # ... em y+8 (6 px de respiro)
                abaixo = py + 76            # ... e termina em y+70
                y = (acima if acima >= t else abaixo if abaixo + h <= b
                     else max(t, min(acima, b - h)))
                return max(l + 4, min(x, r - w - 4)), y
        return r - w - 24, b - h - 100

    def _agendar(self, ms, fn):
        if self.raiz is None or self._fechada:
            return
        caixa = {}

        def pronto():
            self._depois.discard(caixa.get("id"))
            if not self._fechada:
                fn()
        caixa["id"] = self.raiz.after(ms, pronto)
        self._depois.add(caixa["id"])

    def _vigiar(self):
        """A cada 100 ms: clique fora, 12 s sem uso, pilula sumiu ou mexeu."""
        if self._fechada or self.raiz is None:
            return
        try:
            agora = time.monotonic()
            cursor = _cursor()
            no_seletor = _dentro(cursor, _retangulo(self.hwnd))
            pil = self._hwnd_pilula()
            ret_pil = _retangulo(pil)
            na_pilula = _dentro(cursor, ret_pil)
            if no_seletor or na_pilula:
                self._ultimo_uso = agora
            visivel = (not pil) or bool(_u32().IsWindowVisible(pil))
            mexeu = (ret_pil is not None and self._ret_pilula is not None
                     and ret_pil[:2] != self._ret_pilula[:2])
            motivo = motivo_para_fechar(agora, self._ultimo_uso,
                                        _botao_do_mouse_apertado(), no_seletor,
                                        na_pilula, visivel, mexeu)
        except Exception:
            motivo = None                 # vigia que falha nunca fecha nem acusa
        if motivo:
            self.fechar(motivo)
            return
        self._agendar(VIGIA_MS, self._vigiar)

    def _preencher(self, itens, status=None):
        if self._fechada or self.raiz is None:
            return
        if itens is not None:
            self.itens = itens
        if status is not None:
            self.status = status
            self._atualizando = False
        self._redimensionar()
        self._desenhar()

    def _redimensionar(self):
        h = altura(self.itens, self.status)
        if h == int(float(self.canvas.cget("height"))):
            return
        x, y = self._posicao(LARGURA, h)
        self.raiz.geometry(f"{LARGURA}x{h}+{x}+{y}")
        self.canvas.configure(height=h)
        self.raiz.update_idletasks()
        self._arredondar(LARGURA, h)

    # -- desenho ----------------------------------------------------------
    def _desenhar(self):
        c = self.canvas
        if c is None or self._fechada:
            return
        f = self._fontes
        c.delete("all")
        c.create_text(18, 22, text=TITULO, anchor="w", fill=TEXTO, font=f["titulo"])
        c.create_text(18, 40, text=SUBTITULO, anchor="w", fill=SECUNDARIO,
                      font=f["pequena"])
        self._linhas = linhas(self.itens, self.status)
        util = LARGURA - MARGEM_TEXTO - 18
        for i, ln in enumerate(self._linhas):
            y0, y1, tipo = ln["y0"], ln["y1"], ln["tipo"]
            meio = (y0 + y1) / 2
            if tipo == "separador":
                c.create_line(18, meio, LARGURA - 18, meio, fill=LINHA)
                continue
            if tipo == "status":
                c.create_text(MARGEM_TEXTO, meio, anchor="w", fill=SECUNDARIO,
                              font=f["pequena"],
                              text=elidir(self.status, f["pequena"].measure, util))
                continue
            if tipo == "carregando":
                c.create_text(MARGEM_TEXTO, meio, text="Carregando microfones…",
                              anchor="w", fill=SECUNDARIO, font=f["normal"])
                continue
            if self._hover == i and interativa(ln):
                _retangulo_redondo(c, 8, y0 + 2, LARGURA - 8, y1 - 2, 9,
                                   fill=HOVER, outline="")
            if tipo == "atualizar":
                c.create_text(MARGEM_TEXTO, meio, anchor="w", font=f["normal"],
                              text="Atualizando…" if self._atualizando else ROTULO_ATUALIZAR,
                              fill=SECUNDARIO if self._atualizando else ACENTO)
                continue
            it = ln["item"]
            ativo = bool(it["habilitado"])
            detalhe = it.get("detalhe") or ""
            y_nome = meio - 8 if detalhe else meio
            self._radio(c, 26, y_nome, bool(it["marcado"]), ativo)
            c.create_text(MARGEM_TEXTO, y_nome, anchor="w", font=f["normal"],
                          fill=TEXTO if ativo else APAGADO,
                          text=elidir(it["rotulo"], f["normal"].measure, util))
            if detalhe:
                c.create_text(MARGEM_TEXTO, meio + 9, anchor="w", font=f["pequena"],
                              fill=SECUNDARIO if ativo else APAGADO,
                              text=elidir(detalhe, f["pequena"].measure, util))

    @staticmethod
    def _radio(c, x, y, marcado, ativo):
        r = 7
        if marcado:
            c.create_oval(x - r, y - r, x + r, y + r, outline="",
                          fill=ACENTO if ativo else APAGADO)
            c.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, outline="",
                          fill="#ffffff" if ativo else FUNDO)
        else:
            c.create_oval(x - r, y - r, x + r, y + r, width=1.5,
                          outline=ANEL if ativo else ANEL_APAGADO)

    # -- mouse (so mouse: a janela nunca tem foco de teclado) ----------------
    def _linha_em(self, y):
        for i, ln in enumerate(self._linhas):
            if ln["y0"] <= y < ln["y1"]:
                return i if interativa(ln) else None
        return None

    def _mover(self, ev):
        self._ultimo_uso = time.monotonic()
        i = self._linha_em(ev.y)
        if i != self._hover:
            self._hover = i
            try:
                self.canvas.configure(cursor="hand2" if i is not None else "arrow")
            except Exception:
                pass
            self._desenhar()

    def _sair(self, ev=None):
        if self._hover is not None:
            self._hover = None
            self._desenhar()

    def _apertar(self, ev):
        self._apertado = self._linha_em(ev.y)

    def _soltar(self, ev):
        i = self._linha_em(ev.y)
        apertado, self._apertado = self._apertado, None
        if i is None or i != apertado:
            return
        self._acionar(self._linhas[i])

    def _acionar(self, ln):
        if ln["tipo"] == "atualizar":
            self._atualizar()
            return
        it = ln.get("item")
        if it and ln["tipo"] in ("auto", "mic") and it.get("habilitado"):
            self._escolher(it)

    def _escolher(self, it):
        # config.gravar + log numa thread daemon: o Tk nao espera disco
        threading.Thread(target=gravar_escolha, args=(it["chave"], self.app),
                         name="seletor-escolha", daemon=True).start()
        self.fechar("escolheu")

    def _atualizar(self):
        """So ENFILEIRA a recarga na fila do app (acoes-teclado), que so roda
        com o app ocioso ou em erro. Quem recarrega nunca e' o seletor."""
        if self._atualizando:
            return
        self._atualizando = True
        self._desenhar()

        def terminou(ok):
            # roda na thread da fila do app: so le a lista (em memoria) e
            # devolve o resultado para a thread do Tk
            self._carregar_em_thread("Lista atualizada" if ok else
                                     "Não atualizada agora (app ocupado)")
        pedir = getattr(self.app, "pedir_recarga_mics", None)
        try:
            if pedir is None:
                raise RuntimeError("app sem fila")
            pedir(terminou)
        except Exception:
            terminou(False)
        self._agendar(int(ATUALIZAR_TETO_S * 1000), self._atualizar_expirou)

    def _atualizar_expirou(self):
        if self._atualizando:
            self._preencher(None, "Não deu para atualizar agora")

    def fechar(self, motivo=""):
        """Qualquer thread. Nunca espera o Tk (o Alt direito chama daqui)."""
        if self._fechada:
            return
        self._fechada = True
        with _LOCK:
            if _ATUAL["obj"] is self:
                _ATUAL["obj"] = None
        import tkloop

        def _fim():
            try:
                if self.raiz is not None:
                    for tarefa in list(self._depois):
                        try:
                            self.raiz.after_cancel(tarefa)
                        except Exception:
                            pass
                    self._depois.clear()
                    self.raiz.destroy()
            except Exception:
                pass
            # Font e widgets seguram o interpretador Tcl: a ultima referencia
            # morre na thread dona (licao do tkloop).
            self._fontes = {}
            self.canvas = None
            self.raiz = None
            self.hwnd = 0
        tkloop.executar(_fim)
        if motivo:
            log(f"seletor: fechado ({motivo})")


# ════════════════════════════════════════════════════════════════════
def esta_aberto() -> bool:
    with _LOCK:
        return _ATUAL["obj"] is not None


def alternar(app, mostrar=True):
    """Abre (ou fecha, se ja estiver aberto). Chamado SO pelo clique dele na
    pilula — `_clique_da_pilula(app, "seletor")`, na thread do Tk.
    `mostrar=False` so existe para o teste da janela oculta."""
    with _LOCK:
        aberto, _ATUAL["obj"] = _ATUAL["obj"], None
    if aberto is not None:
        aberto.fechar("clique na pilula")
        return None
    s = SeletorMic(app, mostrar=mostrar)
    with _LOCK:
        _ATUAL["obj"] = s
    if not s.abrir():
        with _LOCK:
            if _ATUAL["obj"] is s:
                _ATUAL["obj"] = None
        return None
    return s


def fechar_se_aberto(motivo=""):
    """Alt direito, clique em outro ponto da pilula, pilula escondida."""
    with _LOCK:
        aberto, _ATUAL["obj"] = _ATUAL["obj"], None
    if aberto is not None:
        aberto.fechar(motivo)
