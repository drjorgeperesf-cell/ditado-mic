# CLAUDE.md

Guia para o Claude Code neste repositório (Ditado Mic, Windows).

## O que é
App de ditado por voz: tecla global → grava → whisper-server local (`127.0.0.1:7777`) → cola o texto no cursor.
Python com `ctypes` e `tkinter`; só 6 pacotes em `requirements.txt` (justificados no arquivo).

## Primeiro uso
1. Se `.venv` não existir, peça para a pessoa dar duplo clique em `INSTALAR.bat` (ou rode-o). Ele baixa o whisper.cpp e o modelo.
2. Rode `TESTAR.bat` e mostre o resultado em poucas linhas.
3. Pergunte o idioma (`language` em `%USERPROFILE%\.config\ditado-mic\config.json`; padrão `pt`) e a tecla preferida (`CONFIGURAR-TECLA.bat`).

## Estrutura
- `recursos/` o app (`app.py` orquestra; `config.py` tem os padrões; `transcricao.py` fala com o servidor).
- `_acoes/` o que os `.bat` chamam. `.bat` salvo em CRLF e fino.
- `painel/` painel web só de leitura (porta 7910).
- `whisper/` e `modelos/` são baixados pelo instalador e ficam fora do git.

## Regras
- O `.bat` não leva lógica: ela mora em `_acoes\*.py`.
- Nunca suprima o Alt direito (`hotkey_ditar_suprimir` fica `false`): quebraria AltGr+tecla.
- Não adicione dependências sem justificar em `requirements.txt`.
- Config do usuário vive fora do repositório (`%USERPROFILE%\.config\ditado-mic`).
