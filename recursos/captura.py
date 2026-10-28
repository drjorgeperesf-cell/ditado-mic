# -*- coding: utf-8 -*-
"""Captura de audio: WAV 16 kHz mono s16, escrito INCREMENTALMENTE.

Substitui o AVAudioEngine do Mac por `sounddevice` (PortAudio/WASAPI).
Pontos que vieram de incidentes reais do Mac e continuam valendo aqui:

- **Watchdog de "engine parou" e' obrigatorio.** No Mac, o fone Bluetooth so
  troca pro perfil de mic DEPOIS que alguem abre o input; a mudanca de taxa
  PARAVA o engine e ninguem religava: 0 byte pelo resto da gravacao. No
  Windows o BT tambem troca A2DP<->HFP. Watchdog de 250 ms, teto de 6
  religamentos por gravacao.
- **Escrita incremental**: o WAV cresce durante a fala; uma queda no meio nao
  perde tudo.
- **Pico acumulado** e' o gate que separa alucinacao de ditado intencional, e
  tambem o alerta PASSIVO de "mic mudo" (so cor, sem som, sem notificacao, e
  NUNCA por pausa no meio — o usuário pausa pra ler questoes).

TESTABILIDADE: `Gravador` aceita uma `fonte` substituivel. Em teste, a fonte e'
um WAV de arquivo (`FonteArquivo`), e o caminho de "audio gravado" do app roda
inteiro sem microfone e sem a voz do usuário.

COM (causa raiz de 11/10/2026, "A gravacao nao iniciou"): o PortAudio 19.7 chama
`CoMarshalInterThreadInterfaceInStream` no start() do WASAPI. A thread
'captura' nao tem COM e o processo nao tinha MTA -> CO_E_NOTINITIALIZED ->
paUnanticipatedHostError (o texto 'WdmSyncIoctl GLE 0x490' do log e' erro
VELHO da enumeracao, nao a causa). O WASAPI so funcionava depois do 1o uso de
UI Automation (uia.py), que deixava um MTA implicito no processo. Conserto:
`_garantir_mta()` no topo de `FonteMicrofone._abrir_em`.
"""
from __future__ import annotations

import array
import ctypes
import re
import threading
import time
import wave
from pathlib import Path

import config
from log import log

TAXA = 16000
CANAIS = 1
MAX_RELIGAR = 6
WATCHDOG_S = 0.25
_AUDIO_LOCK = threading.RLock()
_FONTES_ATIVAS = 0
_RECARGAS = 0            # quantas vezes a lista do PortAudio foi de fato recarregada

# ── COM: o MTA do processo ────────────────────────────────────────────
_OLE32 = None
_MTA_VIA = None          # None = ainda nao tentou; senao como foi garantido (ou "falhou")
_MTA_COOKIE = None       # cookie do CoIncrementMTAUsage: guardado PARA SEMPRE
_MTA_GUARDIAO = None     # plano B: a thread 'mta-guardiao'
_MTA_GUARDIAO_FIM = threading.Event()   # nunca e' setado: a guardia vive com o processo
_COINIT_MULTITHREADED = 0x0
_CO_E_NOTINITIALIZED = 0x800401F0

# ── nome estavel do aparelho ──────────────────────────────────────────
_MME_MAX = 31            # o MME corta o nome do aparelho em 31 caracteres
_API_NUNCA = "Windows WDM-KS"
_ORDEM_API = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}
_ORDEM_ALTERNATIVA = {"MME": 0, "Windows DirectSound": 1}
_PREFIXOS_MIC = ("microfone (", "microphone (")
_RE_PREFIXO_NUMERO = re.compile(r"^\d+-\s*")      # o "2- " da reenumeracao USB
_APELIDOS_DO_PADRAO = ("mapeador de som da microsoft", "microsoft sound mapper",
                       "driver de captura de som primário",
                       "primary sound capture driver")


class MicrofoneIndisponivel(RuntimeError):
    pass


def _ole32():
    """Instancia PROPRIA do ole32: os argtypes daqui nao vazam para o uia.py."""
    global _OLE32
    if _OLE32 is None:
        o = ctypes.WinDLL("ole32")
        o.CoIncrementMTAUsage.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
        o.CoIncrementMTAUsage.restype = ctypes.c_long
        o.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        o.CoInitializeEx.restype = ctypes.c_long
        o.CoGetApartmentType.argtypes = (ctypes.POINTER(ctypes.c_int),
                                         ctypes.POINTER(ctypes.c_int))
        o.CoGetApartmentType.restype = ctypes.c_long
        _OLE32 = o
    return _OLE32


def _incrementar_mta():
    """Plano A: CoIncrementMTAUsage. Devolve o cookie, ou None se falhou."""
    try:
        cookie = ctypes.c_void_p()
        hr = _ole32().CoIncrementMTAUsage(ctypes.byref(cookie))
    except Exception:            # Windows sem a API (anterior ao 8) ou sem ole32
        return None
    return cookie if hr == 0 else None


def _iniciar_guardiao_mta():
    """Plano B: uma thread daemon entra no MTA e fica parada para sempre —
    enquanto ela vive, o MTA do processo existe. Devolve a thread, ou None."""
    pronto, res = threading.Event(), {}

    def guardiao():
        try:
            res["hr"] = _ole32().CoInitializeEx(None, _COINIT_MULTITHREADED)
        except Exception:
            res["hr"] = None
        ok = res["hr"] in (0, 1)       # S_OK / S_FALSE
        pronto.set()
        if ok:
            _MTA_GUARDIAO_FIM.wait()   # nunca volta: nada de CoUninitialize

    t = threading.Thread(target=guardiao, name="mta-guardiao", daemon=True)
    t.start()
    pronto.wait(2.0)
    return t if res.get("hr") in (0, 1) else None


def _garantir_mta():
    """Garante um MTA no processo, UMA vez, antes de qualquer start() do WASAPI.

    Com o MTA do processo vivo, toda thread sem COM (a 'captura' inclusive)
    vira 'MTA implicito' e o marshal do PortAudio no start() funciona.
    - Plano A: CoIncrementMTAUsage. O cookie fica numa variavel de modulo e
      NUNCA e' devolvido: devolver derrubaria o MTA com stream aberto.
    - Plano B (hr != 0): thread daemon 'mta-guardiao' com CoInitializeEx(MTA)
      esperando um Event para sempre.
    De proposito NAO inicializa COM dentro da thread 'captura': um
    CoUninitialize no fim dela, com stream do PortAudio ainda vivo ou com
    objeto COM pendurado, e' a mesma classe do heap corrompido de 07/10.
    """
    global _MTA_VIA, _MTA_COOKIE, _MTA_GUARDIAO
    with _AUDIO_LOCK:
        if _MTA_VIA is not None:
            return _MTA_VIA
        cookie = _incrementar_mta()
        if cookie is not None:
            _MTA_COOKIE = cookie
            _MTA_VIA = "CoIncrementMTAUsage"
        else:
            t = _iniciar_guardiao_mta()
            if t is not None:
                _MTA_GUARDIAO = t
                _MTA_VIA = "thread mta-guardiao"
            else:
                _MTA_VIA = "falhou"
        if _MTA_VIA == "falhou":
            log("captura: MTA do processo NAO garantido "
                "(CoIncrementMTAUsage e thread guardia falharam)")
        else:
            log(f"captura: MTA do processo garantido ({_MTA_VIA})")
        return _MTA_VIA


_APT_TIPO = {0: "STA", 1: "MTA", 2: "NA", 3: "STA principal"}
_APT_QUALIF = {1: " implicito", 2: " (NA sobre MTA)", 3: " (NA sobre STA)",
               4: " (NA sobre MTA implicito)", 5: " (NA sobre STA principal)",
               6: " (STA de app)"}


def _apartamento_com() -> str:
    """Apartamento COM da thread ATUAL, so para o log. 'sem COM' (com o
    processo sem MTA) foi a causa de 11/10/2026."""
    try:
        t, q = ctypes.c_int(-1), ctypes.c_int(-1)
        hr = _ole32().CoGetApartmentType(ctypes.byref(t), ctypes.byref(q)) & 0xFFFFFFFF
    except Exception:
        return "?"
    if hr == _CO_E_NOTINITIALIZED:
        return "sem COM"
    if hr:
        return f"hr=0x{hr:08X}"
    return _APT_TIPO.get(t.value, str(t.value)) + _APT_QUALIF.get(q.value, "")


def _descrever(sd, device):
    """(nome, api) do dispositivo na lista ATUAL do PortAudio; (None, '?') se nao der."""
    try:
        info = sd.query_devices(device, "input")
        nome = info.get("name")
    except Exception:
        return None, "?"
    try:
        api = sd.query_hostapis(info["hostapi"])["name"]
    except Exception:
        api = "?"
    return nome, api


class FonteArquivo:
    """Fonte de teste: le um WAV de disco em blocos, como se fosse o mic."""

    def __init__(self, caminho, tempo_real=False):
        self.caminho = Path(caminho)
        self.tempo_real = tempo_real
        self._parar = threading.Event()

    def ler(self, ao_bloco):
        with wave.open(str(self.caminho), "rb") as w:
            if w.getframerate() != TAXA or w.getnchannels() != CANAIS \
                    or w.getsampwidth() != 2:
                raise ValueError(
                    f"{self.caminho.name}: esperado 16 kHz mono s16, veio "
                    f"{w.getframerate()} Hz / {w.getnchannels()} ch / "
                    f"{w.getsampwidth() * 8} bits")
            bloco = TAXA // 10
            while not self._parar.is_set():
                q = w.readframes(bloco)
                if not q:
                    break
                ao_bloco(q)
                if self.tempo_real:
                    time.sleep(bloco / TAXA)

    def parar(self):
        self._parar.set()

    @property
    def viva(self):
        return not self._parar.is_set()


class FonteMicrofone:
    """Fonte real: sounddevice RawInputStream, 16 kHz mono int16."""

    def __init__(self, device=None, alternativas=False):
        self.device = device
        # Driver alternativo (MME/DS do MESMO aparelho) so com lista FRESCA:
        # numa lista velha o indice do MME aponta para uma posicao que agora e'
        # de OUTRO aparelho (o Bose). Quem acabou de recarregar a lista liga
        # isto: o Gravador na 2a tentativa e o CONFIGURAR-MIC.
        self.alternativas = alternativas
        self._stream = None
        self._parar = threading.Event()
        self.religamentos = 0

    def _abrir(self, ao_bloco, alternativas=None):
        if alternativas is None:
            alternativas = self.alternativas
        import sounddevice as sd
        try:
            return self._abrir_em(self.device, ao_bloco)
        except Exception as primeira_falha:
            # So tipo + mensagem do PortAudio (nunca texto de ditado) + o
            # apartamento COM desta thread: 'sem COM' aqui foi a causa de 11/10.
            nome, api = _descrever(sd, self.device)
            log(f"captura: abertura falhou em dev={self.device} api={api} "
                f"com={_apartamento_com()} "
                f"({type(primeira_falha).__name__}: {str(primeira_falha)[:260]})")
            if nome is None or not alternativas:
                raise
            # outro driver do MESMO aparelho (chave/corte do MME), MME -> DS;
            # nunca WDM-KS, nunca Bluetooth
            alternativas = sorted(
                (d for d in listar_microfones()
                 if d["idx"] != self.device
                 and d["api"] in _ORDEM_ALTERNATIVA
                 and not d["bluetooth"]
                 and mesmo_mic(nome, d["nome"])),
                key=lambda d: _ORDEM_ALTERNATIVA[d["api"]])
            for d in alternativas:
                try:
                    stream = self._abrir_em(d["idx"], ao_bloco)
                except Exception as e:
                    # antes era `except: continue` mudo — a falha do DirectSound/MME
                    # depois da reenumeracao USB ficava invisivel
                    log(f"captura: alternativa falhou em dev={d['idx']} api={d['api']} "
                        f"({type(e).__name__}: {str(e)[:200]})")
                    continue
                self.device = d["idx"]
                log(f"captura: usando driver alternativo {d['api']} (dev={d['idx']})")
                return stream
            raise primeira_falha

    def _abrir_em(self, device, ao_bloco):
        # ANTES de qualquer start(): sem MTA no processo o WASAPI falha
        # (CO_E_NOTINITIALIZED) e tudo cai no fallback. Ver _garantir_mta.
        _garantir_mta()
        import sounddevice as sd

        def cb(indata, frames, t, status):
            if status:
                pass  # overflow/underflow: nao derruba a gravacao
            ao_bloco(bytes(indata))

        extra = None
        info = sd.query_devices(device, "input")
        api = sd.query_hostapis(info["hostapi"])["name"]
        if "WASAPI" in api:
            # Compartilhado: conversao do mixer Windows para WAV 16 kHz.
            extra = sd.WasapiSettings(auto_convert=True)
        s = sd.RawInputStream(samplerate=TAXA, channels=CANAIS, dtype="int16",
                              blocksize=0, device=device,
                              callback=cb, extra_settings=extra)
        try:
            s.start()
        except Exception:
            s.close()
            raise
        return s

    def ler(self, ao_bloco):
        global _FONTES_ATIVAS
        with _AUDIO_LOCK:
            _FONTES_ATIVAS += 1
        try:
            self._ler(ao_bloco)
        finally:
            # Fecha e SO DEPOIS descadastra, no MESMO lock: nao existe instante
            # com um stream ainda fechando e _FONTES_ATIVAS == 0 (a recarga da
            # lista faria Pa_Terminate no meio do close — crash heap de 07/10).
            with _AUDIO_LOCK:
                self.parar()
                _FONTES_ATIVAS -= 1

    def _ler(self, ao_bloco):
        stream = self._abrir(ao_bloco)
        with _AUDIO_LOCK:
            self._stream = stream     # se parar() ja rodou, o finally de ler() fecha
        # WATCHDOG: poll de `active`, nao notificacao (a notificacao depende de
        # thread/runloop de entrega — licao do AVAudioEngine). Depois de
        # publicado, o PaStream so e' tocado (active/stop/close) SOB o
        # _AUDIO_LOCK: nunca no meio do close de outra thread nem de um
        # Pa_Terminate da recarga da lista.
        while not self._parar.is_set():
            time.sleep(WATCHDOG_S)
            with _AUDIO_LOCK:
                try:
                    ativo = bool(self._stream is not None and self._stream.active)
                except Exception:
                    ativo = False
            if ativo or self._parar.is_set():
                continue
            if self.religamentos >= MAX_RELIGAR:
                log(f"captura: engine parou {self.religamentos}x — desisto de religar")
                raise MicrofoneIndisponivel("O microfone desconectou durante a gravacao")
            self.religamentos += 1
            log(f"captura: engine parou — religando ({self.religamentos}/{MAX_RELIGAR})")
            # troca: o stream parado sai e fecha SOB o lock (um unico dono)
            with _AUDIO_LOCK:
                velho, self._stream = self._stream, None
                if velho is not None:
                    try:
                        velho.close()
                    except Exception:
                        pass
            if self._parar.is_set():
                break
            try:
                # mesmo dispositivo, sem driver alternativo: com captura
                # ativa a lista nao pode ser recarregada (pode estar velha)
                novo = self._abrir(ao_bloco, alternativas=False)
            except Exception as e:
                log(f"captura: religamento falhou ({type(e).__name__})")
                raise MicrofoneIndisponivel("Nao foi possivel reconectar o microfone") from e
            with _AUDIO_LOCK:
                self._stream = novo   # se parar() chegou no meio, o finally fecha

    def parar(self):
        self._parar.set()
        # Um unico dono fecha o stream: parar() roda na thread do app E no finally
        # da thread de captura; stop/close simultaneos no mesmo PaStream = double free.
        # stop/close DENTRO do _AUDIO_LOCK (RLock): a recarga da lista
        # (Pa_Terminate) espera o close acabar. O callback do PortAudio nao
        # toca nesse lock, entao o stop() (que espera o callback) nao trava.
        with _AUDIO_LOCK:
            stream, self._stream = self._stream, None
            if stream is None:
                return
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()       # mesmo se o stop falhou: nada de PaStream vazado
            except Exception:
                pass

    @property
    def viva(self):
        with _AUDIO_LOCK:
            try:
                return bool(self._stream is not None and self._stream.active)
            except Exception:
                return False


class Gravador:
    """Grava numa geracao. TUDO e' carimbado com a geracao (licao 6.4):
    thread zumbi pos-timeout nao pode renomear/apagar o WAV da gravacao nova,
    nem adotar a sessao nova, nem escrever no estado dela."""

    def __init__(self, destino: Path, fonte=None, geracao: int = 0):
        self.destino = Path(destino)
        self._fonte_auto = fonte is None   # so refaz a fonte que ele mesmo criou
        self.fonte = fonte or FonteMicrofone(dispositivo_escolhido())
        self.geracao = geracao
        self.pico = 0.0
        self.nivel = 0.0
        self.bytes_gravados = 0
        self.t0 = 0.0
        self.t1 = 0.0
        self._wav = None
        self._lock = threading.Lock()
        self._thread = None
        self._fim = threading.Event()
        self._pronto = threading.Event()
        self.erro = None

    # -- escrita incremental -------------------------------------------
    def _bloco(self, dados: bytes):
        if not dados:
            return
        amostras = array.array("h")
        amostras.frombytes(dados[:len(dados) - (len(dados) % 2)])
        if amostras:
            p = max(abs(int(x)) for x in amostras) / 32768.0
            self.nivel = p
            if p > self.pico:
                self.pico = p
        with self._lock:
            if self._wav is not None:
                if not self.bytes_gravados:
                    self.t0 = time.monotonic()
                self._wav.writeframes(dados)
                self.bytes_gravados += len(dados)
                self._pronto.set()

    def _rodar(self):
        try:
            self.fonte.ler(self._bloco)
        except Exception as e:
            self.erro = e
            log(f"captura: fonte caiu ({type(e).__name__})")
        finally:
            self._fim.set()
            self._pronto.set()

    def iniciar(self):
        """Abre o mic. 1a tentativa: SO o dispositivo escolhido na lista em
        memoria (o WASAPI do aparelho), sem driver alternativo — a lista pode
        estar velha. Se falhar com a fonte automatica, o indice pode estar velho
        (PortAudio congela a lista no boot e o USB muda de posicao ao
        reconectar): recarrega a lista UMA vez, com a captura ja parada, e tenta
        de novo; SO nessa 2a tentativa, com a lista fresca, valem MME/DS do
        MESMO aparelho. Nunca reinicializa o PortAudio com captura ativa."""
        try:
            return self._iniciar_uma_vez()
        except MicrofoneIndisponivel as falha:
            if not self._fonte_auto:
                raise
            recargas = _RECARGAS
            try:
                novo = dispositivo_escolhido(forcar=True)
            except Exception:
                raise falha
            fresca = _RECARGAS != recargas   # a recarga e' recusada com captura ativa
            if fresca:
                log(f"captura: abertura falhou — lista recarregada, tentando dev={novo}")
            else:
                log(f"captura: abertura falhou — lista NAO recarregada (captura "
                    f"ativa), tentando dev={novo} sem driver alternativo")
            self.fonte = FonteMicrofone(novo, alternativas=fresca)
            self.erro = None
            self.pico = self.nivel = 0.0
            self.bytes_gravados = 0
            self._thread = None
            self._fim.clear()
            self._pronto.clear()
            return self._iniciar_uma_vez()

    def _iniciar_uma_vez(self):
        self.destino.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.destino), "wb")
        self._wav.setnchannels(CANAIS)
        self._wav.setsampwidth(2)
        self._wav.setframerate(TAXA)
        self.t0 = time.monotonic()
        self._thread = threading.Thread(target=self._rodar, name="captura",
                                        daemon=True)
        self._thread.start()
        timeout = float(config.get("mic_abertura_timeout_s", 4.0))
        if not self._pronto.wait(timeout=timeout) or not self.bytes_gravados:
            self.parar()
            raise MicrofoneIndisponivel("O microfone nao entregou audio") from self.erro
        if self.erro:
            self.parar()
            raise MicrofoneIndisponivel("Nao foi possivel abrir o microfone") from self.erro

    def parar(self) -> float:
        """Fecha o WAV e devolve a duracao do AUDIO (nao o elapsed)."""
        self.fonte.parar()
        self._fim.wait(timeout=3.0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self.t1 = time.monotonic()
        with self._lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                finally:
                    self._wav = None
        return self.duracao_audio

    @property
    def duracao_audio(self) -> float:
        return self.bytes_gravados / float(TAXA * 2 * CANAIS)

    @property
    def elapsed(self) -> float:
        return (self.t1 or time.monotonic()) - self.t0

    def drift(self):
        """Instrumentacao 'drift-capture' (funcao pura; quem chama decide logar).
        Razao entre audio no WAV e elapsed: 1,0 = nao perdeu amostra."""
        e = int(self.elapsed)
        if e < 10:
            return None
        try:
            razao = self.duracao_audio / e
        except ZeroDivisionError:
            return None
        return (f"drift-capture: elapsed={e}s wav={self.duracao_audio:.1f}s "
                f"razao={razao:.3f}")


# ════════════════════════════════════════════════════════════════════
def listar_microfones(atualizar=False) -> list[dict]:
    """Inputs disponiveis. Marca os que parecem Bluetooth: no Windows, usar um
    fone BT como mic forca o perfil de chamada (HFP) e destroi o audio —
    mesma fisica do Bose no Mac."""
    global _RECARGAS
    try:
        import sounddevice as sd
    except Exception:
        return []
    with _AUDIO_LOCK:
        if atualizar and not _FONTES_ATIVAS:
            # PortAudio congela a lista no boot. Atualiza SO sem captura ativa.
            sd._terminate()
            sd._initialize()
            _RECARGAS += 1
        dispositivos = sd.query_devices()
        apis = sd.query_hostapis()
    out = []
    try:
        padrao = sd.default.device[0]
    except Exception:
        padrao = None
    for i, d in enumerate(dispositivos):
        if d.get("max_input_channels", 0) <= 0:
            continue
        nome = d.get("name", "")
        bt = any(t in nome.lower() for t in
                 ("bluetooth", "hands-free", "headset", "bose", "airpods"))
        virtual = any(t in nome.lower() for t in
                      ("virtual", "emeet wave", "stereo mix", "mixagem", "cable"))
        out.append({"idx": i, "nome": nome, "bluetooth": bt,
                    "virtual": virtual, "api": apis[d["hostapi"]]["name"],
                    "padrao": (i == padrao)})
    return out


def chave_mic(nome) -> str:
    """Nome ESTAVEL do aparelho fisico: e' o que se compara e o que se grava.

    O mesmo fifine aparece como 'Microfone (2- fifine Microphone)' (WASAPI/DS),
    'Microfone (2- fifine Microphone' (o MME corta em 31) e, antes da
    reenumeracao USB, 'Microfone (fifine Microphone)'. Todos viram
    'fifine microphone': casefold, espacos colapsados, sem 'microfone (' /
    'microphone (' (e o ')' final, se houver) e sem o prefixo 'N- ' que o
    Windows poe. Idempotente: chave_mic(chave_mic(x)) == chave_mic(x)."""
    s = " ".join(str(nome or "").casefold().split())
    while True:
        antes = s
        for p in _PREFIXOS_MIC:
            if s.startswith(p):
                s = s[len(p):]
                if s.endswith(")"):
                    s = s[:-1]
                break
        s = _RE_PREFIXO_NUMERO.sub("", s).strip()
        if s == antes:
            return s


def mesmo_mic(a, b) -> bool:
    """Mesmo aparelho fisico? Chaves iguais, ou um dos nomes foi cortado pelo
    MME (31 caracteres) e a chave dele e' prefixo da chave do outro."""
    ka, kb = chave_mic(a), chave_mic(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if len(str(a or "")) == _MME_MAX and kb.startswith(ka):
        return True
    return len(str(b or "")) == _MME_MAX and ka.startswith(kb)


def _apelido_do_padrao(nome) -> bool:
    """'Mapeador de som' (MME) e 'Driver de captura de som primario' (DS) sao
    apelidos do padrao do Windows, nao aparelhos."""
    n = str(nome or "").casefold()
    return any(t in n for t in _APELIDOS_DO_PADRAO)


def agrupar_aparelhos(mics) -> list[dict]:
    """Uma entrada por APARELHO FISICO (o PortAudio lista o mesmo mic 3-4x, um
    por driver). Fora: WDM-KS (nunca usado) e os apelidos do padrao.

    Cada aparelho: `chave` (o que se grava em manual_mic_nome), `nome` (o mais
    longo, sem o corte do MME), `bluetooth`/`virtual`/`padrao`, e `entradas`
    em ordem WASAPI -> MME -> DirectSound. Ordem da 1a aparicao na lista."""
    grupos: list[list[dict]] = []
    for d in mics:
        if d.get("api") == _API_NUNCA or _apelido_do_padrao(d.get("nome")):
            continue
        for g in grupos:
            if any(mesmo_mic(d["nome"], e["nome"]) for e in g):
                g.append(d)
                break
        else:
            grupos.append([d])
    aparelhos = []
    for g in grupos:
        nome = max((e["nome"] for e in g), key=len)
        aparelhos.append({
            "chave": chave_mic(nome),
            "nome": nome,
            "bluetooth": any(e.get("bluetooth") for e in g),
            "virtual": any(e.get("virtual") for e in g),
            "padrao": any(e.get("padrao") for e in g),
            "entradas": sorted(g, key=lambda e: _ORDEM_API.get(e.get("api"), 9)),
        })
    return aparelhos


def escolher_dispositivo(mics):
    """Escolha por nome ESTAVEL (chave_mic), nunca por indice.

    - `manual_mic_nome` (escolha dele) manda; `mic_preferido_nome` (do PC, ex.
      'FIFINE') e' o automatico. Os dois aceitam a chave normalizada, o nome
      cru antigo (com ou sem o '2- ', cortado ou nao pelo MME) e um pedaco do
      nome ('FIFINE' continua valendo por substring).
    - NUNCA WDM-KS. Ordem WASAPI -> MME -> DirectSound.
    - Bluetooth so se ele escolheu manualmente (forca o perfil de chamada).
    - Um USB ausente nunca vira mic virtual."""
    alvo = (config.get("manual_mic_nome", "") or "").strip()
    preferido = (config.get("mic_preferido_nome", "") or "").strip()
    procura = alvo or preferido
    if not procura:
        return None
    usaveis = [d for d in mics if d["api"] != _API_NUNCA
               and (alvo or not d["bluetooth"])]
    candidatos = [d for d in usaveis if mesmo_mic(procura, d["nome"])]
    if not candidatos:
        k = chave_mic(procura)
        candidatos = [d for d in usaveis if k and k in chave_mic(d["nome"])]
    if not candidatos and not alvo and config.get("mic_usb_generico_permitido", False):
        usb = [d for d in usaveis if "usb" in d["nome"].lower()
               and not d["bluetooth"] and not d["virtual"]]
        # So adota um USB generico quando ha UM aparelho (nao dois ambiguos).
        if len(agrupar_aparelhos(usb)) == 1:
            candidatos = usb
    if not candidatos:
        raise MicrofoneIndisponivel(f"Microfone preferido ausente: {procura}")
    return min(candidatos, key=lambda d: _ORDEM_API.get(d["api"], 9))["idx"]


def dispositivo_escolhido(atualizar=True, forcar=False):
    """Usa a lista em memoria. So reinicializa o PortAudio (Pa_Terminate +
    Pa_Initialize) quando o mic preferido nao aparece nela — nao a cada gravacao:
    o processo caiu 2x com 0xC0000374 dentro de threads do PortAudio."""
    if forcar:   # indice velho: o nome ainda "casa", mas o dispositivo mudou
        return escolher_dispositivo(listar_microfones(atualizar=True))
    try:
        return escolher_dispositivo(listar_microfones(atualizar=False))
    except MicrofoneIndisponivel:
        if not atualizar:
            raise
    return escolher_dispositivo(listar_microfones(atualizar=True))
