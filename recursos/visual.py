# -*- coding: utf-8 -*-
"""Acabamento da interface: vetores suaves e alpha real, sem tomar o foco.

Pillow ja e' dependencia do icone da bandeja. A capsula usa a mesma imagem
RGBA no preview e no Windows (UpdateLayeredWindow); nada captura a tela.
"""
from __future__ import annotations

import ctypes
import math
import os
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from contextlib import contextmanager

from PIL import Image, ImageDraw, ImageFilter, ImageFont

LARGURA, ALTURA = 420, 82
BTN_W, BTN_H, BTN_GAP, RESPIRO_X = 32, 32, 6, 9
BOTOES = ("textos", "formatar", "min", "close")
FUNDO, TEXTO, SECUNDARIO = "#1b1c23", "#f5f5f7", "#9698a5"
CORES = {
    "ocioso": "#9698a5", "iniciando": "#ffd60a",
    "gravando": "#ff453a", "transcrevendo": "#0a84ff",
    "polindo": "#bf5af2", "erro": "#ffd60a",
}
ESCALA = 3

# Zonas de clique (pixels logicos da pilula). O NOME (titulo + subtitulo) abre
# o seletor de microfone; o icone do mic e os botoes continuam como sempre.
ZONA_MIC = (23, 18, 65, 60)
ZONA_NOME = (72, 14, 228, 64)
SUBTITULO_X = 80
SUBTITULO_LARGURA = 146          # de x=80 ate' 4 px antes do separador (x=230)
SUBTITULO_TAMANHOS = (10.5, 10, 9.5)
SUBTITULO_ERRO = "Clique para escolher o microfone"


def _na_zona(zona, x, y):
    x0, y0, x1, y1 = zona
    return x0 <= x <= x1 and y0 <= y <= y1


def botoes_x():
    xs, x = {}, LARGURA - 22 - BTN_W
    for nome in reversed(BOTOES):
        xs[nome] = x
        x -= BTN_W + BTN_GAP
        if nome == "close":
            x -= RESPIRO_X
    return xs


def botao_em(x, y):
    if not 23 <= y <= 23 + BTN_H:
        return None
    return next((n for n, bx in botoes_x().items()
                 if bx <= x <= bx + BTN_W), None)


def alvo_em(x, y):
    """O que esta sob o mouse: um botao, 'mic' (o icone), 'nome' (abre o
    seletor de microfone) ou None (o resto: so arrasta)."""
    botao = botao_em(x, y)
    if botao:
        return botao
    if _na_zona(ZONA_MIC, x, y):
        return "mic"
    if _na_zona(ZONA_NOME, x, y):
        return "nome"
    return None


@lru_cache(maxsize=24)
def fonte(tamanho, peso="normal"):
    pasta = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    nome = "consola.ttf" if peso == "mono" else (
        "seguisb.ttf" if peso == "semibold" else "segoeui.ttf")
    try:
        return ImageFont.truetype(str(pasta / nome), round(tamanho * ESCALA))
    except OSError:
        return ImageFont.load_default(size=round(tamanho * ESCALA))


def largura_texto(texto, tamanho, peso="normal"):
    """Largura em pixels LOGICOS (a imagem e' desenhada em ESCALA x)."""
    return fonte(tamanho, peso).getlength(texto) / ESCALA


@lru_cache(maxsize=64)
def caber(texto, largura=SUBTITULO_LARGURA, tamanhos=SUBTITULO_TAMANHOS,
          peso="normal"):
    """(texto, tamanho) que cabe em `largura`: diminui a fonte um pouco e, se
    ainda nao couber no menor tamanho, corta com reticencias. Nome de mic
    comprido nunca invade o separador nem os botoes."""
    for tam in tamanhos:
        if largura_texto(texto, tam, peso) <= largura:
            return texto, tam
    tam = tamanhos[-1]
    t = texto
    while t and largura_texto(t + "…", tam, peso) > largura:
        t = t[:-1]
    return ((t.rstrip() + "…") if t else "…"), tam


def _rgb(cor):
    return tuple(int(cor[i:i + 2], 16) for i in (1, 3, 5))


def desenhar_pilula(estado="ocioso", nivel=0.0, segundos=0, mic_mudo=False,
                    manual=False, detalhe="Alt direito", hover=None, fase=0.0,
                    escala_saida=1.0):
    """Imagem independente de Tk/Windows, inclusive para inspecao offline."""
    s = ESCALA
    img = Image.new("RGBA", (LARGURA * s, ALTURA * s))
    sombra = Image.new("RGBA", img.size)
    d = ImageDraw.Draw(sombra)
    d.rounded_rectangle((10*s, 13*s, 410*s, 73*s), radius=31*s,
                        fill=(0, 0, 0, 105))
    img.alpha_composite(sombra.filter(ImageFilter.GaussianBlur(4*s)))
    d = ImageDraw.Draw(img)

    def rect(box, raio, fill, outline=None, width=1):
        d.rounded_rectangle(tuple(round(v*s) for v in box), round(raio*s),
                            fill=fill, outline=outline, width=round(width*s))

    def linha(pontos, fill, width=1.6):
        pontos = [(round(x*s), round(y*s)) for x, y in pontos]
        esp = max(1, round(width*s))
        d.line(pontos, fill=fill, width=esp, joint="curve")
        r = esp / 2
        for x, y in (pontos[0], pontos[-1]):
            d.ellipse((x-r, y-r, x+r, y+r), fill=fill)

    def texto(x, y, t, tamanho, fill=TEXTO, peso="normal"):
        d.text((x*s, y*s), t, font=fonte(tamanho, peso), fill=fill, anchor="lm")

    rect((10, 8, 410, 70), 31, FUNDO, "#3b3c45", .7)
    cor = CORES.get(estado, CORES["ocioso"])
    if estado == "ocioso":
        mic_fundo = "#30313b"
    else:
        rgb = _rgb(cor)
        mic_fundo = tuple(round(v*.22 + b*.78) for v, b in zip(rgb, _rgb(FUNDO)))
    rect((23, 18, 65, 60), 21, mic_fundo)
    if hover == "mic":
        d.ellipse((23*s, 18*s, 65*s, 60*s), outline="#6a6c7a", width=s)
    if estado == "gravando":
        # O VU so acompanha amostras reais; pausa nao anima uma fala falsa.
        anel = "#ffd60a" if mic_mudo else cor
        d.ellipse((21*s, 16*s, 67*s, 62*s), outline=anel,
                  width=round((1.2 + min(1., nivel))*s))
        rect((38, 33, 50, 45), 3, cor)
    else:
        tinta = TEXTO if estado == "ocioso" else cor
        rect((40, 28, 48, 41), 4, None, tinta, 1.6)
        d.arc((36*s, 32*s, 52*s, 46*s), 0, 180, fill=tinta, width=round(1.6*s))
        linha([(44, 46), (44, 50)], tinta)
        linha([(40, 50), (48, 50)], tinta)
    if manual:
        d.ellipse((58*s, 17*s, 64*s, 23*s), fill="#ffd60a", outline=FUNDO, width=s)
    titulos = {"ocioso": "Microfone", "iniciando": "Conectando",
               "gravando": "Gravando", "transcrevendo": "Transcrevendo",
               "polindo": "Formatando", "erro": "Sem microfone"}
    dicas = {"textos": ("Textos", "Ver histórico"),
             "formatar": ("Formatar", "Texto selecionado"),
             "min": ("Ocultar", "Continua pelo atalho"),
             "close": ("Recolher", "Alt direito reabre"),
             "nome": ("Escolher microfone", None)}      # None: subtitulo normal
    dica = dicas.get(hover) if estado == "ocioso" else None
    texto(80, 29, dica[0] if dica else titulos.get(estado, "Microfone"),
          12.5, peso="semibold")
    if estado in ("gravando", "iniciando"):
        texto(80, 49, f"{segundos//60:02d}:{segundos%60:02d}", 11, SECUNDARIO, "mono")
    else:
        # ERRO: o subtitulo vira o convite ao seletor (so texto e cor; nada
        # pisca, nada abre sozinho)
        sub = {"transcrevendo": "Processando áudio", "polindo": "Preparando texto",
               "erro": SUBTITULO_ERRO}.get(estado, detalhe)
        if dica and dica[1]:
            sub = dica[1]
        sub, tam = caber(sub)
        texto(SUBTITULO_X, 49, sub, tam,
              CORES["erro"] if estado == "erro" else SECUNDARIO)
    if estado == "gravando":
        fatores = (.45, .8, 1., .65, .35)
        for i, fator in enumerate(fatores):
            h = 3 + 25*min(1., max(0., nivel))*fator
            rect((195+i*5, 39-h/2, 198+i*5, 39+h/2), 1.5, cor)
    elif estado in ("iniciando", "transcrevendo", "polindo"):
        for i in range(3):
            opacidade = .35 + .65*(.5 + .5*math.sin(fase*5-i*1.3))
            rgb = tuple(round(v*opacidade + b*(1-opacidade))
                        for v, b in zip(_rgb(cor), _rgb(FUNDO)))
            d.ellipse(((199+i*7)*s, 37*s, (202+i*7)*s, 40*s), fill=rgb)
    linha([(230, 27), (230, 51)], "#393a43", .7)
    for nome, x in botoes_x().items():
        if hover == nome:
            rect((x, 23, x+BTN_W, 55), 11, "#343640")
        cx, cy = x+BTN_W/2, 39
        tinta = "#b6b8c5" if nome == "close" else "#e3e4eb"
        if nome == "textos":
            for yy in (-5, 0, 5):
                linha([(cx-3, cy+yy), (cx+6, cy+yy)], tinta, 1.5)
                d.ellipse(((cx-7)*s, (cy+yy-1)*s, (cx-5)*s, (cy+yy+1)*s), fill=tinta)
        elif nome == "formatar":
            p = [(cx,cy-8),(cx+2.2,cy-2.2),(cx+8,cy),
                 (cx+2.2,cy+2.2),(cx,cy+8),(cx-2.2,cy+2.2),
                 (cx-8,cy),(cx-2.2,cy-2.2),(cx,cy-8)]
            linha(p, tinta, 1.35)
            linha([(cx+8,cy-9),(cx+8,cy-5)], tinta, 1.1)
            linha([(cx+6,cy-7),(cx+10,cy-7)], tinta, 1.1)
        elif nome == "min":
            linha([(cx-6,cy),(cx+6,cy)], tinta)
        elif nome == "close":
            linha([(cx-4,cy-4),(cx+4,cy+4)], tinta, 1.4)
            linha([(cx+4,cy-4),(cx-4,cy+4)], tinta, 1.4)
    return img.resize((round(LARGURA*escala_saida), round(ALTURA*escala_saida)),
                      Image.Resampling.LANCZOS)


@contextmanager
def _pixels_fisicos():
    """Tk conserva coordenadas logicas; o compositor recebe pixels fisicos.

    A consulta e' apenas temporaria nesta thread, sem alterar o DPI dos
    widgets, do processo ou do app onde o usuário digita. Regioes GDI recebem
    pixels fisicos; UpdateLayeredWindow conserva o contexto logico de Tk.
    """
    import winapi
    trocar = getattr(winapi.user32, "SetThreadDpiAwarenessContext", None)
    anterior = None
    if trocar:
        trocar.argtypes, trocar.restype = (ctypes.c_void_p,), ctypes.c_void_p
        anterior = trocar(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    try:
        yield
    finally:
        if anterior:
            trocar(anterior)


def tamanho_fisico(hwnd):
    import winapi
    u = winapi.user32
    u.GetWindowRect.argtypes, u.GetWindowRect.restype = (
        wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL
    with _pixels_fisicos():
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            raise ctypes.WinError(ctypes.get_last_error())
        return r.right-r.left, r.bottom-r.top


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _BLEND(ctypes.Structure):
    _fields_ = [("op", ctypes.c_byte), ("flags", ctypes.c_byte),
                ("alpha", ctypes.c_byte), ("format", ctypes.c_byte)]


class _HEADER(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                ("height", wintypes.LONG), ("planes", wintypes.WORD),
                ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG),
                ("yppm", wintypes.LONG), ("used", wintypes.DWORD),
                ("important", wintypes.DWORD)]


def apresentar(hwnd, img):
    return _apresentar_em_pixels(hwnd, img)


def _apresentar_em_pixels(hwnd, img):
    """Alpha pre-multiplicado. Libera todos os handles GDI em cada frame."""
    import winapi
    u = winapi.user32
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    u.GetDC.argtypes, u.GetDC.restype = (wintypes.HWND,), wintypes.HDC
    u.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = (wintypes.HDC,), wintypes.HDC
    g.DeleteDC.argtypes = (wintypes.HDC,)
    g.CreateDIBSection.argtypes = (wintypes.HDC, ctypes.POINTER(_HEADER), wintypes.UINT,
                                  ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
    g.CreateDIBSection.restype = wintypes.HBITMAP
    g.SelectObject.argtypes, g.SelectObject.restype = (wintypes.HDC, wintypes.HANDLE), wintypes.HANDLE
    g.DeleteObject.argtypes = (wintypes.HANDLE,)
    u.UpdateLayeredWindow.argtypes = (wintypes.HWND, wintypes.HDC,
        ctypes.POINTER(wintypes.POINT), ctypes.POINTER(_SIZE), wintypes.HDC,
        ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(_BLEND), wintypes.DWORD)
    u.UpdateLayeredWindow.restype = wintypes.BOOL
    winapi._SETLONG(hwnd, winapi.GWL_EXSTYLE,
                   winapi._GETLONG(hwnd, winapi.GWL_EXSTYLE) | winapi.WS_EX_LAYERED)
    # Pillow RGBa e' alpha pre-multiplicado, exigido pelo compositor Windows.
    dados = img.convert("RGBa").tobytes("raw", "BGRa")
    w, h = img.size
    dc = u.GetDC(None)
    mem = g.CreateCompatibleDC(dc)
    bitmap, anterior = None, None
    try:
        header = _HEADER(ctypes.sizeof(_HEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bitmap = g.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(bits, dados, len(dados))
        anterior = g.SelectObject(mem, bitmap)
        tamanho, origem, blend = _SIZE(w, h), wintypes.POINT(0, 0), _BLEND(0, 0, -1, 1)
        if not u.UpdateLayeredWindow(hwnd, dc, None, ctypes.byref(tamanho), mem,
                                    ctypes.byref(origem), 0, ctypes.byref(blend), 2):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if anterior:
            g.SelectObject(mem, anterior)
        if bitmap:
            g.DeleteObject(bitmap)
        if mem:
            g.DeleteDC(mem)
        if dc:
            u.ReleaseDC(None, dc)


def arredondar_janela(hwnd, largura, altura, raio=24):
    """Cantos reais para a janela Tk do historico, preservando os estilos."""
    import winapi
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    g.CreateRoundRectRgn.argtypes = (ctypes.c_int,)*6
    g.CreateRoundRectRgn.restype = wintypes.HANDLE
    g.DeleteObject.argtypes = (wintypes.HANDLE,)
    winapi.user32.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HANDLE, wintypes.BOOL)
    fisico_w, fisico_h = tamanho_fisico(hwnd)
    raio_fisico = round(raio*fisico_w/max(1, largura))
    with _pixels_fisicos():
        regiao = g.CreateRoundRectRgn(0, 0, fisico_w+1, fisico_h+1,
                                     raio_fisico*2, raio_fisico*2)
        if regiao and not winapi.user32.SetWindowRgn(hwnd, regiao, True):
            g.DeleteObject(regiao)  # Windows assume ownership apenas no sucesso.
    try:
        dwm = ctypes.WinDLL("dwmapi")
        # DWM_NCRENDERING_ENABLED; sombra nativa onde o compositor suporta.
        policy = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 2, ctypes.byref(policy), 4)
    except OSError:
        pass
