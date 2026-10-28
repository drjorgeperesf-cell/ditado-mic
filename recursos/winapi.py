# -*- coding: utf-8 -*-
"""Camada ctypes do Windows: gancho de teclado, SendInput, foco e janelas.

Mapa macOS -> Windows (inventario secao 7):
  CGEventTap kCGEventFlagsChanged  ->  SetWindowsHookExW(WH_KEYBOARD_LL)
      `RegisterHotKey` NAO serve: ele nao ve modificador sozinho.
      O hook ve o scancode e a flag LLKHF_EXTENDED, que e' como se distingue
      Ctrl/Alt/Win DIREITO do esquerdo — exatamente o papel do bit
      device-dependent (NX_DEVICERCMDKEYMASK) do Mac.
  CGEventSourceUserData (marca propria)  ->  SendInput dwExtraInfo
  CGEventKeyboardSetUnicodeString        ->  SendInput KEYEVENTF_UNICODE
  NSPanel NonactivatingPanel             ->  WS_EX_NOACTIVATE|TOPMOST|TOOLWINDOW
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ── constantes ───────────────────────────────────────────────────────
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012
WM_TIMER = 0x0113
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_APPS = 0x5D
VK_BACK = 0x08
VK_C, VK_V = 0x43, 0x56

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

DWMWA_CLOAKED = 14

# Marca dos nossos proprios eventos sintéticos. O hook le dwExtraInfo e
# descarta os que vieram daqui — analogo direto do MARCA_SINTETICO 0xC0FFEE
# do corretor do Mac (`CGEventSourceUserData`).
MARCA_SINTETICO = 0x00C0FFEE

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE,
                                     wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                  wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD))
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetTimer.argtypes = (wintypes.HWND, ULONG_PTR, wintypes.UINT, ctypes.c_void_p)
user32.SetTimer.restype = ULONG_PTR
user32.KillTimer.argtypes = (wintypes.HWND, ULONG_PTR)
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD))
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = (
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
user32.GetKeyboardLayout.restype = wintypes.HANDLE
user32.ToUnicodeEx.argtypes = (
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
    wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, wintypes.HANDLE)
user32.ToUnicodeEx.restype = ctypes.c_int
try:
    user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_longlong)
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    _SETLONG, _GETLONG = user32.SetWindowLongPtrW, user32.GetWindowLongPtrW
except AttributeError:  # 32 bits
    _SETLONG, _GETLONG = user32.SetWindowLongW, user32.GetWindowLongW


# ════════════════════════════════════════════════════════════════════
# Gancho de teclado de baixo nivel
class GanchoTeclado:
    """WH_KEYBOARD_LL numa thread propria com bomba de mensagens.

    - PASSIVO por padrao: devolve a tecla ao sistema (nunca engole), a menos
      que o callback peca supressao explicita para aquele vk.
    - Distingue press de release pela MENSAGEM (WM_KEYDOWN/UP), nunca por uma
      mascara agregada — o bug do Mac (Cmd esquerdo segurado virava segundo
      press) e estruturalmente impossivel aqui.
    - Ignora autorrepeticao: mantem um mapa vk->esta_pressionada.
    - Ignora eventos com dwExtraInfo == MARCA_SINTETICO (os nossos).
    - Watchdog: se o hook cair, remonta. Analogo do watchdog do CGEventTap.
    """

    def __init__(self, ao_evento, suprimir_vks=frozenset()):
        """`ao_evento(vk, pressionada, extended, scancode, quando_ms)`.

        Roda DENTRO do hook: tem que ser rapido. O `scancode` e o `quando_ms`
        sao o que permite reconhecer o **LCtrl fantasma** que o AltGr injeta
        (mesmo carimbo de tempo do RMENU que vem logo atras).
        """
        self._cb = ao_evento
        self._suprimir = set(suprimir_vks)
        self._down: dict[int, bool] = {}
        self._hook = None
        self._proc = None
        self._thread = None
        self._tid = 0
        self._parar = threading.Event()
        self.remontagens = 0
        self.renovacoes = 0

    def _renovar(self):
        # O Windows pode remover WH_KEYBOARD_LL sem avisar se um callback
        # exceder LowLevelHooksTimeout. Handle/thread vivos nao provam o hook.
        novo = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not novo:
            return False
        antigo, self._hook = self._hook, novo
        if antigo:
            user32.UnhookWindowsHookEx(antigo)
        self._down = {vk: True for vk, down in self._down.items()
                      if down and (user32.GetAsyncKeyState(vk) & 0x8000)}
        self.renovacoes += 1
        return True

    # -- o proprio hook ------------------------------------------------
    def _low_level(self, ncode, wparam, lparam):
        if ncode == 0:
            try:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.dwExtraInfo != MARCA_SINTETICO:
                    vk = int(kb.vkCode)
                    ext = bool(kb.flags & LLKHF_EXTENDED)
                    sc = int(kb.scanCode)
                    quando = int(kb.time)
                    down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    if down:
                        repetida = self._down.get(vk, False)
                        self._down[vk] = True
                        if not repetida:
                            self._cb(vk, True, ext, sc, quando)
                    else:
                        self._down[vk] = False
                        self._cb(vk, False, ext, sc, quando)
                    if vk in self._suprimir:
                        return 1
            except Exception:
                pass
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _loop(self):
        self._tid = kernel32.GetCurrentThreadId()
        self._proc = HOOKPROC(self._low_level)
        while not self._parar.is_set():
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
            if not self._hook:
                time.sleep(1.0)
                continue
            timer = user32.SetTimer(None, 0, 30000, None)
            msg = wintypes.MSG()
            while not self._parar.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                if msg.message == WM_TIMER and msg.wParam == timer:
                    self._renovar()
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if timer:
                user32.KillTimer(None, timer)
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None
            if not self._parar.is_set():
                self.remontagens += 1
                time.sleep(0.2)

    def iniciar(self):
        if self._thread and self._thread.is_alive():
            return
        self._parar.clear()
        self._thread = threading.Thread(target=self._loop, name="gancho-teclado",
                                        daemon=True)
        self._thread.start()
        for _ in range(100):           # espera o hook subir (<=1 s)
            if self._hook:
                return True
            time.sleep(0.01)
        return bool(self._hook)

    def parar(self):
        self._parar.set()
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)

    @property
    def vivo(self) -> bool:
        return bool(self._hook) and bool(self._thread and self._thread.is_alive())


# ════════════════════════════════════════════════════════════════════
# SendInput
def _enviar(inputs) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _ki(vk, up=False, extended=False):
    fl = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTunion(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=fl, time=0,
                                             dwExtraInfo=MARCA_SINTETICO)))


def _ki_unicode(ch, up=False):
    fl = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTunion(ki=KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=fl,
                                             time=0, dwExtraInfo=MARCA_SINTETICO)))


VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_NEUTRA = 0xE8                      # VK nao atribuida (quebra "Alt sozinho")

# Modificadores conferidos um a um, ESQUERDO e DIREITO separados. Mandar
# keyup no VK agregado (VK_MENU/VK_CONTROL) nao solta de forma confiavel o
# lado direito — e com a tecla de ditar sendo o Alt DIREITO isso deixaria de
# fora justamente o que mais importa aqui.
MODIFICADORES = (VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL,
                 VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN)


def _modificadores_presos(exceto=()) -> list[int]:
    """Modificadores que o usuario ainda esta segurando.

    Enviar Ctrl+V com Shift preso vira Ctrl+Shift+V; com o Alt direito preso
    vira Ctrl+Alt+V. Solta-se e devolve-se ao fim.
    """
    return [vk for vk in MODIFICADORES
            if vk not in exceto and (user32.GetAsyncKeyState(vk) & 0x8000)]


def modificadores_presos_agora() -> list[int]:
    """Para diagnostico e teste: quem esta preso NESTE instante."""
    return _modificadores_presos()


def quebrar_alt_sozinho():
    """Injeta um par down/up de uma tecla NEUTRA (VK 0xE8).

    Alt SOZINHO, ao soltar, abre a barra de menus de muitos apps ("Alt
    poison" — isso ja aconteceu: "nao digito no terminal"). Com um
    evento no meio, a sequencia deixa de ser "alt-down, alt-up" e o menu nao
    abre.

    NAO suprimimos o keyup do Alt: suprimir deixaria o modificador PRESO, que
    e' o pior bug possivel aqui. Os eventos levam `MARCA_SINTETICO`, entao o
    nosso proprio gancho os ignora.
    """
    return _enviar([_ki(VK_NEUTRA), _ki(VK_NEUTRA, up=True)]) == 2


def atalho_ctrl(vk_letra) -> bool:
    """Ctrl+<letra> sintetico no app em foco. Devolve False se o SendInput
    nao aceitou TODOS os eventos (UIPI numa janela elevada, por exemplo).

    Solta TODO modificador preso antes (inclusive o **Alt direito**, que agora
    e' a tecla de ditar, e o LCtrl fantasma que o AltGr injeta junto) — senao
    o Ctrl+V vira Ctrl+Alt+V. Devolve os que eram do usuario no fim.
    """
    presos = _modificadores_presos(exceto=(VK_LCONTROL, VK_RCONTROL))
    seq = [_ki(m, up=True, extended=(m in (VK_RMENU, VK_RCONTROL,
                                           VK_LWIN, VK_RWIN)))
           for m in presos]
    # o Ctrl fantasma do AltGr tambem precisa sair do caminho
    if user32.GetAsyncKeyState(VK_LCONTROL) & 0x8000:
        seq.append(_ki(VK_LCONTROL, up=True))
    seq += [_ki(VK_CONTROL), _ki(vk_letra), _ki(vk_letra, up=True),
            _ki(VK_CONTROL, up=True)]
    n = _enviar(seq)
    ok = (n == len(seq))
    if presos:
        # devolve SO o que era fisicamente do usuario e ainda esta preso
        devolver = [_ki(m, extended=(m in (VK_RMENU, VK_RCONTROL,
                                           VK_LWIN, VK_RWIN)))
                    for m in presos
                    if user32.GetAsyncKeyState(m) & 0x8000]
        if devolver:
            _enviar(devolver)
    return ok


def digitar_unicode(texto: str, bloco: int = 16, pausa: float = 0.008) -> bool:
    """Digita texto literal, independente do layout (KEYEVENTF_UNICODE).

    Blocos de 16 chars com 8 ms entre eles — mesmos numeros do corretor do Mac
    (BLOCO_UNICODE = 16, 0.008 s).
    """
    ok = True
    for i in range(0, len(texto), bloco):
        pedaco = texto[i:i + bloco]
        seq = []
        for ch in pedaco:
            for cp in ([ch] if ord(ch) <= 0xFFFF else
                       list(ch.encode("utf-16-le").decode("utf-16-le"))):
                seq.append(_ki_unicode(cp))
                seq.append(_ki_unicode(cp, up=True))
        if _enviar(seq) != len(seq):
            ok = False
        time.sleep(pausa)
    return ok


def apagar_atras(n: int, pausa: float = 0.004) -> bool:
    """N backspaces, 4 ms entre eles (numero do corretor do Mac)."""
    ok = True
    for _ in range(max(0, int(n))):
        if _enviar([_ki(VK_BACK), _ki(VK_BACK, up=True)]) != 2:
            ok = False
        time.sleep(pausa)
    return ok


# ════════════════════════════════════════════════════════════════════
# Janelas e foco
def janela_em_foco():
    return user32.GetForegroundWindow()


def pid_da_janela(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def titulo_da_janela(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def exe_da_janela(hwnd) -> str:
    """Nome do executavel do processo dono da janela, minusculo.

    E' o analogo do `bundle id` do Mac (usado nas regras por app).
    """
    from proc import _k32, PROCESS_QUERY_LIMITED_INFORMATION
    pid = pid_da_janela(hwnd)
    if not pid:
        return ""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        _k32.CloseHandle(h)
    return ""


def app_em_foco() -> str:
    return exe_da_janela(janela_em_foco())


def nao_ativavel(hwnd, topo=True):
    """WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW.

    `WS_EX_NOACTIVATE` e' o ponto critico: sem ele, clicar na pilula rouba o
    foco do editor e o Ctrl+V cola no lugar errado.
    `WS_EX_TOOLWINDOW` tira a janela do Alt-Tab e da barra de tarefas.
    """
    ex = _GETLONG(hwnd, GWL_EXSTYLE)
    novo = ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if topo:
        novo |= WS_EX_TOPMOST
    _SETLONG(hwnd, GWL_EXSTYLE, novo)
    if topo:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    return novo


def reassertar_topo(hwnd):
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def esta_encoberta(hwnd) -> bool | None:
    """DWMWA_CLOAKED — o analogo do "existe mas nao compoe" do WindowServer.
    None = nao sei (nunca tratar "nao sei" como "quebrado", licao 6.11)."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        val = wintypes.DWORD()
        hr = dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED,
                                       ctypes.byref(val), ctypes.sizeof(val))
        if hr != 0:
            return None
        return bool(val.value)
    except Exception:
        return None


def area_de_trabalho() -> tuple[int, int, int, int]:
    """Retangulo util da tela primaria (sem a barra de tarefas)."""
    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
    r = RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
        return r.left, r.top, r.right, r.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def retangulo_do_monitor_do_cursor() -> tuple[int, int, int, int]:
    """Area util do monitor onde o cursor esta."""
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]
    # Assinaturas explicitas: sem elas o HMONITOR de 64 bits volta truncado para int.
    user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
    user32.MonitorFromPoint.argtypes = (POINT, wintypes.DWORD)
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.POINTER(MONITORINFO))
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    hm = user32.MonitorFromPoint(p, 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    return area_de_trabalho()
