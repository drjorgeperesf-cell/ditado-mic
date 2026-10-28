# -*- coding: utf-8 -*-
"""A tecla de ditar = **Alt direito**. Decisão explícita do usuário (20/09/2026):

    "eu quero o alt direito pra puxar o mic e o ctrl direito pra formatar"

Isso SUBSTITUI a proibição anterior do Alt direito no inventário. Mas a razão
da proibição continua verdadeira e precisa ser neutralizada aqui, porque ela é
física: **Alt direito É o AltGr** no teclado dele (ele alterna ABNT2 e
US-Internacional com Win+Espaço). Três armadilhas, todas tratadas nesta
máquina de estado — que é PURA de propósito, para poder ser testada com
sequências de eventos, sem tocar no teclado vivo.

────────────────────────────────────────────────────────────────────────
ARMADILHA 1 — o LCtrl FANTASMA
Em layout com AltGr, o Windows injeta um **VK_LCONTROL sintético** logo antes
do VK_RMENU, com o MESMO carimbo de tempo. Ele não é Ctrl de verdade:
  - não pode disparar nem atrapalhar o ramo do Ctrl DIREITO (não dá: o ramo
    de formatar só olha VK_RCONTROL = 0xA3, e o fantasma é o ESQUERDO);
  - não pode contar como "outra tecla" e cancelar o ditado;
  - não pode zerar o buffer do corretor como se fosse um atalho.
Detecção: LCtrl com scancode 0x1D seguido, no MESMO carimbo de tempo (janela
de FANTASMA_MS), de um RMENU. Como o fantasma vem ANTES do RMENU, na hora em
que ele chega o ditado ainda nem começou — por construção ele é inofensivo
para o cancelamento. O que sobra é marcá-lo para o corretor.

────────────────────────────────────────────────────────────────────────
ARMADILHA 2 — os acentos dele não podem quebrar
Regra: **Alt direito SOZINHO dita.** Se qualquer outra tecla (que não o LCtrl
fantasma) for pressionada enquanto o Alt direito está segurado, DENTRO da
janela de `janela_combinacao_s` (0,35 s, o mesmo debounce do hotkey), isso é
um AltGr+tecla: a gravação é **cancelada em silêncio** (não cola, não avisa,
WAV descartado) e a tecla passa normalmente. Depois dessa janela ele já está
ditando: tecla solta não cancela mais nada.

────────────────────────────────────────────────────────────────────────
ARMADILHA 3 — o "Alt poison"
Alt SOZINHO, ao SOLTAR, ativa a barra de menus de muitos apps. Ele já sofreu
isso num app de ditado ("não digito no terminal"). Neutralização: no keydown do Alt
direito, injetamos um par down/up de uma tecla NEUTRA (VK 0xE8, não atribuída)
carimbado com a nossa marca. A sequência que o app vê deixa de ser
"alt-down, alt-up" e passa a ser "alt-down, neutra, alt-up" — e o Windows só
abre o menu quando o Alt vem sozinho.
**Nada é suprimido**: suprimir o keyup do Alt deixaria o modificador PRESO, que
é o pior bug possível aqui (teclado travado). `teste_tecla_altgr.py` prova que,
depois de um ditado, VK_MENU/VK_RMENU/VK_CONTROL/VK_LCONTROL estão soltos.
"""
from __future__ import annotations

import time

VK_RMENU = 0xA5          # Alt direito == AltGr
VK_LMENU = 0xA4
VK_MENU = 0x12
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3       # continua sendo o botão de FORMATAR/CORRIGIR
VK_CONTROL = 0x11

SC_LCTRL = 0x1D          # scancode do LCtrl (o fantasma usa este)
VK_NEUTRA = 0xE8         # VK não atribuída: quebra o "Alt sozinho" sem efeito

FANTASMA_MS = 4          # carimbos de tempo iguais (ou quase) = mesmo evento

# Ações que a máquina devolve. Quem executa é o app (aqui é tudo puro).
INICIAR_OU_PARAR = "toggle"
CANCELAR_SILENCIOSO = "cancelar_silencioso"
QUEBRAR_MENU = "quebrar_menu"
FORMATAR = "formatar"


def e_lctrl_fantasma(vk, scancode, quando_ms, rmenu_ms) -> bool:
    """O LCtrl é o fantasma que o AltGr injeta?

    `rmenu_ms` é o carimbo do RMENU que chegou junto (ou None).
    """
    if vk != VK_LCONTROL or scancode != SC_LCTRL:
        return False
    if rmenu_ms is None or quando_ms is None:
        return False
    return abs(int(rmenu_ms) - int(quando_ms)) <= FANTASMA_MS


class MaquinaAltDireito:
    """Traduz eventos crus do gancho em ações. Sem Windows, sem relógio real.

    `evento()` devolve uma LISTA de ações, na ordem em que devem acontecer.
    """

    def __init__(self, janela_combinacao_s=0.35, debounce_s=0.35,
                 quebrar_menu=True, relogio=None):
        self.janela = float(janela_combinacao_s)
        self.debounce = float(debounce_s)
        self.quebrar_menu = bool(quebrar_menu)
        self._agora = relogio or time.monotonic
        self.alt_desde = None        # instante do keydown do Alt direito
        self.combinou = False        # virou AltGr+tecla?
        self.ditando = False         # a máquina ACHA que está gravando
        self._ultimo_toggle = -1e9
        self._lctrl_pendente = None  # (quando_ms) do LCtrl ainda não julgado
        self.fantasmas = 0           # contador, só para diagnóstico

    # ── consulta ────────────────────────────────────────────────────
    @property
    def alt_segurado(self) -> bool:
        return self.alt_desde is not None

    def dentro_da_janela(self) -> bool:
        return (self.alt_desde is not None
                and (self._agora() - self.alt_desde) <= self.janela)

    # ── o evento ────────────────────────────────────────────────────
    def evento(self, vk, pressionada, extended=False, scancode=0,
               quando_ms=None):
        """Um evento do gancho. Devolve a lista de ações."""
        if vk == VK_RMENU or (vk == VK_MENU and extended):
            return self._alt_direito(pressionada, quando_ms)
        if vk == VK_RCONTROL and pressionada:
            # O Ctrl DIREITO nunca é tocado pelo fantasma (que é o ESQUERDO).
            return [FORMATAR]
        if vk == VK_LCONTROL and pressionada:
            # Guarda para julgar quando/se o RMENU chegar no mesmo carimbo.
            self._lctrl_pendente = quando_ms
            return []
        if pressionada:
            return self._outra_tecla(vk, scancode, quando_ms)
        return []

    def _alt_direito(self, pressionada, quando_ms):
        if not pressionada:
            self.alt_desde = None
            self.combinou = False
            return []
        # keydown do Alt direito
        if e_lctrl_fantasma(VK_LCONTROL, SC_LCTRL, self._lctrl_pendente,
                            quando_ms):
            self.fantasmas += 1
        self._lctrl_pendente = None
        agora = self._agora()
        self.alt_desde = agora
        self.combinou = False
        if agora - self._ultimo_toggle < self.debounce:
            # Debounce: AltGr apertado em sequência (digitando acento) não
            # abre e fecha o microfone a cada tecla.
            return [QUEBRAR_MENU] if self.quebrar_menu else []
        self._ultimo_toggle = agora
        acoes = [QUEBRAR_MENU] if self.quebrar_menu else []
        acoes.append(INICIAR_OU_PARAR)
        self.ditando = not self.ditando
        return acoes

    def _outra_tecla(self, vk, scancode, quando_ms):
        if not self.alt_segurado or self.combinou:
            return []
        if e_lctrl_fantasma(vk, scancode, quando_ms, None):
            return []                       # nunca chega aqui, mas fica claro
        if not self.dentro_da_janela():
            # Passada a janela, ele está DITANDO: tecla solta não cancela.
            return []
        self.combinou = True
        if self.ditando:
            self.ditando = False
            return [CANCELAR_SILENCIOSO]
        return []

    # ── para o corretor ─────────────────────────────────────────────
    def ctrl_e_do_altgr(self) -> bool:
        """O Ctrl que está "pressionado" agora é só o fantasma do AltGr?

        O corretor zera o buffer digitado quando vê um atalho com Ctrl. Sem
        esta pergunta, digitar um acento no US-Internacional (AltGr+e) jogaria
        fora o que ele acabou de escrever.
        """
        return self.alt_segurado
