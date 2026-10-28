# Ditado Mic

Ditado por voz para o Windows, open source e 100% local.
Aperte uma tecla, fale, aperte de novo: o texto aparece no cursor, em qualquer programa.

- A transcrição roda no seu PC (whisper.cpp). Seu áudio não sai da máquina.
- Sem conta, sem chave de API, sem assinatura.
- Funciona em português (troque `language` no `config.json` para outro idioma).

## Instalar (Windows 10/11)

1. Instale o Python 3.10 ou mais novo: https://www.python.org/downloads/ (marque "Add python.exe to PATH").
2. Baixe este repositório (botão verde **Code** → **Download ZIP**) e extraia numa pasta fixa, por exemplo `Documentos\DitadoMic`.
3. Dê duplo clique em **`INSTALAR.bat`**. Ele:
   - cria o ambiente Python e instala 6 pacotes pequenos;
   - baixa o whisper.cpp (9 MB) e o modelo de voz `small` (190 MB), conferindo o SHA-256;
   - pergunta se quer iniciar junto com o Windows (responda `S`);
   - sobe tudo e confere.

Leva uns 5 minutos, quase todo no download.

## Usar

1. Clique onde quer escrever.
2. **Alt direito**: começa a gravar. Fale.
3. **Alt direito** de novo: o texto é colado no cursor.

Outras teclas e atalhos:

| Arquivo | Para quê |
|---|---|
| `CONFIGURAR-TECLA.bat` | trocar a tecla de ditar |
| `CONFIGURAR-MIC.bat` | escolher o microfone |
| `PAINEL.bat` | painel no navegador (histórico e estatísticas) |
| `STATUS.bat` | ver o que está rodando |
| `REINICIAR.bat` | reiniciar o app e o motor de voz |
| `TESTAR.bat` | conferir a instalação |
| `DESINSTALAR.bat` | remover tarefas e dados do app |

O dicionário pessoal (nomes e siglas que o ditado erra) fica em
`%USERPROFILE%\.config\ditado-mic\dicionario.txt`. Ele só ajuda o reconhecimento, nunca troca palavras.

## Notas

- **Alt direito é o AltGr** em teclados ABNT2 e US-Internacional. O app trata isso: Alt direito *sozinho* dita; Alt direito + outra tecla continua digitando o acento normalmente.
- **Placa NVIDIA?** O instalador baixa a versão para CPU, que funciona em qualquer PC. Com placa NVIDIA, baixe a versão CUDA do whisper.cpp (https://github.com/ggml-org/whisper.cpp/releases), copie para a pasta `whisper\Release\` e, se quiser, troque o modelo em `_acoes\subir_servidor.py`.
- **Formatar com IA (opcional):** a tecla Ctrl direito reescreve o último ditado em tópicos usando o CLI `claude` (Claude Code), se ele estiver instalado. Sem ele, o ditado normal funciona do mesmo jeito.
- Tudo roda em `127.0.0.1`. Nada abre porta para a rede.

## Licença

MIT. Veja `LICENSE`. O whisper.cpp (MIT) e o modelo Whisper da OpenAI (MIT) são baixados de seus repositórios oficiais.
