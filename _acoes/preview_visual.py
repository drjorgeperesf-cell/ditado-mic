# -*- coding: utf-8 -*-
"""Preview com dados ficticios e janelas fora da tela, sem entrada sintetica."""
from pathlib import Path
import os
import sys
import tempfile
import time
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "recursos"))


def main():
    with tempfile.TemporaryDirectory(prefix="microfone-preview-") as pasta:
        os.environ["MICROFONE_DADOS"] = str(Path(pasta)/"dados")
        os.environ["MICROFONE_CONFIG_DIR"] = str(Path(pasta)/"config")
        from PIL import Image
        import historico
        import janelinha
        import pilula
        import tkloop
        import visual
        import winapi

        destino = RAIZ / "previews"
        destino.mkdir(exist_ok=True)
        estados = ("ocioso", "gravando", "transcrevendo", "polindo", "erro")
        out = Image.new("RGB", (560, 490), "#eff0f4")
        for i, estado in enumerate(estados):
            img = visual.desenhar_pilula(estado, nivel=.65, segundos=27,
                                        detalhe="Alt direito · FIFINE", fase=.3)
            out.paste(img, (70, 10+i*96), img)
        out.save(destino / "barra-estados.png")
        out.crop((0, 0, 560, 202)).save(destino / "barra-nova.png")
        foco = winapi.janela_em_foco()
        pilula.posicao_padrao = lambda *a, **k: (-5000, -5000)
        pilula.posicao_visivel = lambda *a, **k: True
        pilula.limitar_a_tela = lambda x, y, *a, **k: (x, y)
        p = pilula.Pilula()
        p._manter_visivel = lambda: None
        j = janelinha.Janelinha(SimpleNamespace(pilula=None))
        j._perto_da_pilula = lambda: (-5000, -5000)
        agora = time.time()
        historico.ultimos = lambda *a: [
            {"ts": agora, "texto": "Preparar a apresentação de amanhã. Rever os pontos principais e organizar os próximos passos.",
             "formatado": "Apresentação de amanhã:\n\nPreparação:\n- Rever os pontos principais.\n- Organizar os próximos passos.", "duracao": 18},
            {"ts": agora-1800, "texto": "Anotar as ideias da reunião e separar as tarefas para a semana.", "duracao": 12},
            {"ts": agora-86400, "texto": "Enviar o documento revisado amanhã de manhã e conferir o horário da reunião.", "duracao": 9},
            {"ts": agora-86500, "texto": "Texto de exemplo para conferir a rolagem do histórico.", "duracao": 8},
        ]
        try:
            assert p.iniciar(), "pilula nao montou"
            assert p._alpha_real, "alpha nativo indisponivel"
            p.definir_estado("gravando", nivel=.65, segundos=27)
            assert j.abrir(), "historico nao montou"
            for _ in range(30):
                if not j._carregando:
                    break
                time.sleep(.05)
            def capturar():
                j.raiz.update_idletasks()
                # Evento vai somente ao Canvas deste preview, sem SendInput.
                antes = j._canvas.yview()
                j._rolar_roda(SimpleNamespace(delta=-120))
                depois = j._canvas.yview()
                assert depois[0] > antes[0], "rolagem nao avancou"
                j._limpar_clicado()
                assert j.estado_limpar == "confirma"
                j._escape()
                assert j.estado_limpar == "normal"
            tkloop.executar_e_esperar(capturar)
            print("Alpha nativo: OK; scroll/confirmacao: OK; foco preservado:",
                  winapi.janela_em_foco() == foco)
            print("Preview salvo:", destino)
        finally:
            j.fechar()
            p.encerrar()
            tkloop.encerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
