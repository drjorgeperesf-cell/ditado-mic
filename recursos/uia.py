# -*- coding: utf-8 -*-
"""UI Automation minima por ctypes: ler o texto SELECIONADO sem tocar no clipboard.

E' o analogo direto do `AXSelectedText` do Mac (Accessibility API):
  CoCreateInstance(CUIAutomation) -> IUIAutomation::GetFocusedElement
  -> GetCurrentPattern(UIA_TextPatternId) -> ITextPattern::GetSelection
  -> ITextRangeArray::GetElement(0) -> ITextRange::GetText(-1)

Chamadas via vtable crua, sem `comtypes` (dependencia nova precisaria de
justificativa e este caminho tem ~100 linhas).

TETO DE TEMPO: 0,35 s, como o `AX_TIMEOUT_S` do Mac. App que nao responde
(Chrome/PWA/Electron devolviam -25204 no Mac) vira `None` = "o app nao
informa" — e o caminho de reserva (Ctrl+C) assume. `None` NUNCA e' tratado
como "selecao vazia".
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

ole32 = ctypes.WinDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")

CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 0x2
UIA_TextPatternId = 10014
S_OK = 0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(self))


CLSID_CUIAutomation = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
IID_IUIAutomation = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"

# Indices de vtable (IUnknown = 0,1,2)
_IUIAutomation_GetFocusedElement = 8
_IUIAutomationElement_GetCurrentPattern = 16
_IUIAutomationTextPattern_GetSelection = 5
_IUIAutomationTextRangeArray_get_Length = 3
_IUIAutomationTextRangeArray_GetElement = 4
# 12, NAO 11: o 11 e' GetEnclosingElement (1 ponteiro de saida). Com os argumentos
# de GetText ele escrevia em 0xFFFFFFFF dentro do COM (incidente 07/10/2026).
_IUIAutomationTextRange_GetText = 12
_IUnknown_Release = 2


def _chamar(ptr, indice, restype, argtypes, *args):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[indice])
    return fn(ptr, *args)


def _release(ptr):
    if ptr:
        try:
            _chamar(ptr, _IUnknown_Release, ctypes.c_ulong, ())
        except Exception:
            pass


def _ler_agora():
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    aut = elem = pat = arr = rng = None
    try:
        aut = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(GUID(CLSID_CUIAutomation)), None,
                                    CLSCTX_INPROC_SERVER,
                                    ctypes.byref(GUID(IID_IUIAutomation)),
                                    ctypes.byref(aut))
        if hr != S_OK or not aut:
            return None
        elem = ctypes.c_void_p()
        if _chamar(aut, _IUIAutomation_GetFocusedElement, ctypes.c_long,
                   (ctypes.POINTER(ctypes.c_void_p),),
                   ctypes.byref(elem)) != S_OK or not elem:
            return None
        pat = ctypes.c_void_p()
        if _chamar(elem, _IUIAutomationElement_GetCurrentPattern, ctypes.c_long,
                   (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                   UIA_TextPatternId, ctypes.byref(pat)) != S_OK or not pat:
            return None
        arr = ctypes.c_void_p()
        if _chamar(pat, _IUIAutomationTextPattern_GetSelection, ctypes.c_long,
                   (ctypes.POINTER(ctypes.c_void_p),),
                   ctypes.byref(arr)) != S_OK or not arr:
            return None
        n = ctypes.c_int()
        if _chamar(arr, _IUIAutomationTextRangeArray_get_Length, ctypes.c_long,
                   (ctypes.POINTER(ctypes.c_int),),
                   ctypes.byref(n)) != S_OK or n.value < 1:
            return None
        rng = ctypes.c_void_p()
        if _chamar(arr, _IUIAutomationTextRangeArray_GetElement, ctypes.c_long,
                   (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                   0, ctypes.byref(rng)) != S_OK or not rng:
            return None
        bstr = ctypes.c_void_p()
        if _chamar(rng, _IUIAutomationTextRange_GetText, ctypes.c_long,
                   (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                   -1, ctypes.byref(bstr)) != S_OK or not bstr:
            return None
        try:
            return ctypes.wstring_at(bstr)
        finally:
            oleaut32.SysFreeString(bstr)
    except Exception:
        return None
    finally:
        for p in (rng, arr, pat, elem, aut):
            _release(p)
        ole32.CoUninitialize()


def ler_selecao(timeout_s=0.35):
    """Texto selecionado, ou None quando o app nao informa / estourou o teto.

    None != "" : "" e' selecao vazia de verdade; None e' "nao sei".
    """
    out = [None]

    def alvo():
        out[0] = _ler_agora()

    t = threading.Thread(target=alvo, name="uia-selecao", daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return None                      # estourou o teto: nao sei
    return out[0]
