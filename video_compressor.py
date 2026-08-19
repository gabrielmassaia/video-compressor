"""
Compressor de Vídeo - programa desktop simples para compactar vídeos usando FFmpeg.

Uso: selecione um vídeo, escolha a pasta de saída, o nome do arquivo final,
o nível de qualidade e o codec, e clique em "Compactar". O programa chama o
FFmpeg (instalado no sistema) via subprocess e mostra o tamanho antes/depois
ao terminar, além de uma estimativa de tempo calculada a partir de uma
amostra rápida do próprio vídeo.

Aceleração por GPU: ao abrir, o programa detecta se há uma GPU com suporte a
codificação por hardware (NVIDIA NVENC, AMD AMF ou Intel Quick Sync) e, se
houver, oferece a opção de usar a GPU tanto para decodificar quanto para
codificar o vídeo - isso costuma acelerar MUITO a compressão, porque tira o
trabalho pesado da CPU.

Dependência externa: FFmpeg (com ffprobe) precisa estar instalado e
acessível no PATH do Windows (o programa verifica isso ao abrir). Veja o
aviso em check_ffmpeg().
"""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Configuração das opções que aparecem na interface
# ---------------------------------------------------------------------------

# Mapeia o texto amigável do dropdown para o valor de qualidade que vai pro
# FFmpeg. Para codificação por CPU (libx264/libx265) isso é o CRF; para
# codificação por GPU é usado como QP (Quantization Parameter) - as duas
# escalas são parecidas na prática (menor = mais qualidade/arquivo maior).
QUALITY_PRESETS = {
    "Alta qualidade (arquivo maior)": 18,
    "Balanceado": 23,
    "Máxima compressão (arquivo menor)": 30,
}

# Mapeia o texto amigável do dropdown para o nome do encoder de software do
# FFmpeg. Quando a aceleração por GPU está ativa, o encoder real usado é
# trocado pelo equivalente de hardware (ver HW_ENCODER_FAMILIES abaixo).
CODEC_OPTIONS = {
    "H.264 (libx264) - mais compatível": "libx264",
    "H.265 (libx265) - arquivos menores": "libx265",
}

# Famílias de encoder por hardware que o programa sabe usar, em ordem de
# preferência quando mais de uma estiver disponível no sistema.
HW_ENCODER_FAMILIES = {
    "nvenc": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "label": "NVIDIA NVENC"},
    "amf": {"h264": "h264_amf", "hevc": "hevc_amf", "label": "AMD AMF"},
    "qsv": {"h264": "h264_qsv", "hevc": "hevc_qsv", "label": "Intel Quick Sync"},
}

VIDEO_FILETYPES = [
    ("Arquivos de vídeo", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v"),
    ("Todos os arquivos", "*.*"),
]

# Caracteres inválidos em nomes de arquivo no Windows.
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# Duração (em segundos) da amostra usada para estimar o tempo total de
# compressão antes de rodar o vídeo inteiro.
SAMPLE_DURATION_SECONDS = 5.0

# No Windows, essa flag evita que uma janela de console preta pisque atrás
# do app toda vez que o FFmpeg for chamado via subprocess.
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Verificação do FFmpeg
# ---------------------------------------------------------------------------

def check_ffmpeg() -> bool:
    """Retorna True se o executável 'ffmpeg' estiver acessível no PATH.

    shutil.which() é a forma correta de checar isso: ele procura o
    executável nas mesmas pastas que o Windows procura, sem precisar
    rodar o programa. Não depende de caminho relativo, então continua
    funcionando normalmente depois de empacotado com o PyInstaller.
    """
    return shutil.which("ffmpeg") is not None


FFMPEG_MISSING_MESSAGE = (
    "FFmpeg não foi encontrado no PATH do sistema.\n\n"
    "Este programa depende do FFmpeg para compactar vídeos, mas ele não "
    "vem embutido no executável - precisa ser instalado separadamente.\n\n"
    "Como instalar no Windows:\n"
    "1) Abra o PowerShell e rode:\n"
    "   winget install ffmpeg\n"
    "   (ou baixe em https://www.gyan.dev/ffmpeg/builds/ e adicione a "
    "pasta 'bin' extraída ao PATH do Windows)\n"
    "2) Feche e abra este programa novamente.\n\n"
    "O botão 'Compactar' ficará desabilitado até o FFmpeg ser encontrado."
)


# ---------------------------------------------------------------------------
# Detecção de hardware (CPU, RAM, GPU) - usada só para acelerar a compressão
# e informar o usuário; nada disso é obrigatório para o programa funcionar.
# ---------------------------------------------------------------------------

def get_cpu_count() -> int:
    return os.cpu_count() or 1


def get_total_ram_gb():
    """Lê a RAM total do Windows via ctypes (API GlobalMemoryStatusEx),
    sem precisar de nenhuma biblioteca externa (ex: psutil)."""
    if sys.platform != "win32":
        return None

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        return None


def get_ffmpeg_supported_hw_families() -> set:
    """Pergunta ao FFmpeg instalado quais encoders de vídeo por hardware ele
    SABE compilar (isso só depende de como o FFmpeg foi compilado, não do
    que está instalado na máquina - um "full build" normalmente vem com
    NVENC, AMF e Quick Sync juntos, mesmo que só uma GPU exista de fato)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            creationflags=SUBPROCESS_FLAGS,
            timeout=10,
        )
    except Exception:
        return set()

    output = result.stdout + result.stderr
    return {
        family
        for family, info in HW_ENCODER_FAMILIES.items()
        if info["h264"] in output
    }


def get_installed_gpu_vendors() -> set:
    """Descobre quais fabricantes de GPU estão FISICAMENTE instalados na
    máquina, consultando o WMI via PowerShell. Isso é o que garante que a
    aceleração oferecida na UI realmente bate com o hardware do usuário -
    só checar o que o FFmpeg foi compilado pra suportar não é suficiente."""
    if sys.platform != "win32":
        return set()
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
            ],
            capture_output=True,
            text=True,
            creationflags=SUBPROCESS_FLAGS,
            timeout=15,
        )
    except Exception:
        return set()

    names = result.stdout.lower()
    vendors = set()
    if "nvidia" in names:
        vendors.add("nvidia")
    if "amd" in names or "radeon" in names:
        vendors.add("amd")
    if "intel" in names:
        vendors.add("intel")
    return vendors


# Fabricante de GPU -> família de encoder correspondente no FFmpeg.
VENDOR_TO_FAMILY = {"nvidia": "nvenc", "amd": "amf", "intel": "qsv"}


def detect_hw_encoder_family():
    """Cruza a GPU realmente instalada na máquina com o que esse FFmpeg
    sabe codificar, e retorna a família utilizável ("nvenc"/"amf"/"qsv"),
    ou None se não houver GPU com suporte disponível."""
    supported = get_ffmpeg_supported_hw_families()
    if not supported:
        return None

    installed_vendors = get_installed_gpu_vendors()

    # Prioridade quando há mais de uma GPU (ex: notebook com iGPU Intel +
    # dGPU dedicada): NVIDIA > AMD > Intel, pela ordem típica de desempenho.
    for vendor in ("nvidia", "amd", "intel"):
        family = VENDOR_TO_FAMILY[vendor]
        if vendor in installed_vendors and family in supported:
            return family
    return None


def hw_encoder_name(family: str, base_codec: str) -> str:
    codec_key = "h264" if base_codec == "libx264" else "hevc"
    return HW_ENCODER_FAMILIES[family][codec_key]


def hw_quality_args(family: str, qp: int):
    """Argumentos de controle de qualidade/taxa - cada fabricante usa flags
    diferentes do FFmpeg para "QP fixo" (equivalente ao CRF do libx264)."""
    if family == "amf":
        return ["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp), "-qp_b", str(qp), "-quality", "quality"]
    if family == "nvenc":
        return ["-rc", "constqp", "-qp", str(qp), "-preset", "p5"]
    if family == "qsv":
        return ["-global_quality", str(qp)]
    return []


def build_video_args(crf, base_codec, use_gpu, hw_family):
    """Monta os argumentos de codec/qualidade do FFmpeg, escolhendo entre o
    encoder de software (CPU) e o de hardware (GPU), conforme a opção
    marcada pelo usuário e o que foi detectado no sistema."""
    if use_gpu and hw_family:
        encoder = hw_encoder_name(hw_family, base_codec)
        return ["-c:v", encoder] + hw_quality_args(hw_family, crf)
    return ["-c:v", base_codec, "-crf", str(crf), "-preset", "medium"]


# ---------------------------------------------------------------------------
# Duração do vídeo e estimativa de tempo de compressão
# ---------------------------------------------------------------------------

def probe_duration_seconds(input_path: str):
    """Usa o ffprobe (instalado junto com o FFmpeg) para descobrir a
    duração do vídeo em segundos. Retorna None se não conseguir."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_path,
            ],
            capture_output=True,
            text=True,
            creationflags=SUBPROCESS_FLAGS,
            timeout=15,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def format_duration(seconds: float) -> str:
    """Formata segundos em algo legível, tipo '2min 15s' ou '48s'."""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}min"
    if minutes:
        return f"{minutes}min {secs}s"
    return f"{secs}s"


def estimate_compression_seconds(input_path, duration, crf, base_codec, ext, use_gpu, hw_family):
    """Estima o tempo total de compressão rodando uma amostra curta (alguns
    segundos, tirados do meio do vídeo) com as MESMAS configurações
    escolhidas pelo usuário, medindo quanto tempo real isso leva nesta
    máquina, e extrapolando pela duração total do vídeo.

    É uma estimativa aproximada (baseada em uma amostra pequena, e o
    conteúdo pode variar em complexidade ao longo do vídeo), mas reflete o
    hardware e as configurações reais, o que é bem mais confiável do que um
    número fixo qualquer. Retorna None se não for possível estimar."""

    if not duration or duration <= 0:
        return None

    sample_duration = max(1.0, min(SAMPLE_DURATION_SECONDS, duration))
    sample_start = max(0.0, duration / 2 - sample_duration / 2)

    fd, temp_path = tempfile.mkstemp(suffix=ext or ".mp4")
    os.close(fd)

    try:
        cmd = ["ffmpeg", "-y"]
        if use_gpu and hw_family:
            cmd += ["-hwaccel", "auto"]
        cmd += ["-ss", f"{sample_start:.2f}", "-i", input_path, "-t", f"{sample_duration:.2f}"]
        cmd += build_video_args(crf, base_codec, use_gpu, hw_family)
        cmd += ["-c:a", "aac", "-b:a", "128k", temp_path]

        start = time.perf_counter()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=SUBPROCESS_FLAGS,
            timeout=120,
        )
        elapsed = time.perf_counter() - start

        if result.returncode != 0 or elapsed <= 0:
            return None

        # segundos de vídeo processados por segundo real de execução
        throughput = sample_duration / elapsed
        return duration / throughput
    except Exception:
        return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Interface gráfica
# ---------------------------------------------------------------------------

class VideoCompressorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Compressor de Vídeo")
        self.root.geometry("600x560")
        self.root.resizable(False, False)

        # Variáveis ligadas aos campos da UI
        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.output_name = tk.StringVar()
        self.output_ext_label = tk.StringVar(value="")
        self.quality_label = tk.StringVar(value="Balanceado")
        self.codec_label = tk.StringVar(value=next(iter(CODEC_OPTIONS)))
        self.use_gpu = tk.BooleanVar(value=False)
        self.hw_info_text = tk.StringVar(value="Detectando hardware disponível...")
        self.status_text = tk.StringVar(value="Aguardando...")
        self.result_text = tk.StringVar(value="")

        self.ffmpeg_ok = check_ffmpeg()
        self.hw_family = None  # preenchido depois pela detecção em background

        self._build_ui()

        # Se o FFmpeg não estiver disponível, avisa assim que a janela abre.
        if not self.ffmpeg_ok:
            self.root.after(200, self._warn_ffmpeg_missing)

        # A detecção de hardware (rodar "ffmpeg -encoders", ler RAM, checar a
        # GPU via WMI) roda em background pra não atrasar a abertura da janela.
        threading.Thread(target=self._detect_hardware, daemon=True).start()

    # -- construção da UI ---------------------------------------------------

    def _build_ui(self):
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        # --- Seção: arquivos --------------------------------------------
        file_frame = ttk.LabelFrame(main, text="Arquivo")
        file_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(file_frame, text="Vídeo de entrada:").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2)
        )
        ttk.Entry(file_frame, textvariable=self.input_path, width=58, state="readonly").grid(
            row=1, column=0, sticky="we", padx=(10, 6)
        )
        ttk.Button(file_frame, text="Selecionar...", command=self._select_input).grid(
            row=1, column=1, padx=(0, 10)
        )

        ttk.Label(file_frame, text="Pasta de saída:").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2)
        )
        ttk.Entry(file_frame, textvariable=self.output_dir, width=58, state="readonly").grid(
            row=3, column=0, sticky="we", padx=(10, 6)
        )
        ttk.Button(file_frame, text="Selecionar...", command=self._select_output_dir).grid(
            row=3, column=1, padx=(0, 10)
        )

        ttk.Label(file_frame, text="Nome do arquivo de saída:").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2)
        )
        name_row = ttk.Frame(file_frame)
        name_row.grid(row=5, column=0, columnspan=2, sticky="we", padx=10, pady=(0, 10))
        ttk.Entry(name_row, textvariable=self.output_name, width=45).pack(side="left")
        ttk.Label(name_row, textvariable=self.output_ext_label).pack(side="left", padx=(4, 0))

        file_frame.columnconfigure(0, weight=1)

        # --- Seção: opções de compressão ---------------------------------
        options_frame = ttk.LabelFrame(main, text="Opções de compressão")
        options_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(options_frame, text="Qualidade / compressão:").grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2)
        )
        ttk.Combobox(
            options_frame,
            textvariable=self.quality_label,
            values=list(QUALITY_PRESETS.keys()),
            state="readonly",
            width=28,
        ).grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

        ttk.Label(options_frame, text="Codec de saída:").grid(
            row=0, column=1, sticky="w", padx=10, pady=(8, 2)
        )
        ttk.Combobox(
            options_frame,
            textvariable=self.codec_label,
            values=list(CODEC_OPTIONS.keys()),
            state="readonly",
            width=28,
        ).grid(row=1, column=1, sticky="w", padx=10, pady=(0, 8))

        self.gpu_checkbox = ttk.Checkbutton(
            options_frame,
            text="Usar aceleração de GPU (indisponível)",
            variable=self.use_gpu,
        )
        self.gpu_checkbox.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))
        self.gpu_checkbox.state(["disabled"])

        # --- Seção: hardware detectado ------------------------------------
        hw_frame = ttk.LabelFrame(main, text="Sistema detectado")
        hw_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(hw_frame, textvariable=self.hw_info_text, justify="left").pack(
            anchor="w", padx=10, pady=8
        )

        # --- Botão compactar + progresso ----------------------------------
        self.compress_button = ttk.Button(
            main, text="Compactar", command=self._start_compression
        )
        self.compress_button.pack(pady=(4, 8))
        if not self.ffmpeg_ok:
            self.compress_button.state(["disabled"])

        # Barra de progresso (modo indeterminado: não dá pra saber o %
        # exato só lendo stdout do ffmpeg de forma simples, então usamos uma
        # barra "ocupado" + texto de status com a estimativa de tempo)
        self.progress = ttk.Progressbar(main, mode="indeterminate", length=460)
        self.progress.pack(pady=(0, 6))

        ttk.Label(main, textvariable=self.status_text).pack(pady=(0, 8))
        ttk.Label(main, textvariable=self.result_text, justify="left").pack(anchor="w")

    def _warn_ffmpeg_missing(self):
        messagebox.showwarning("FFmpeg não encontrado", FFMPEG_MISSING_MESSAGE)

    # -- detecção de hardware em background --------------------------------

    def _detect_hardware(self):
        cpu_count = get_cpu_count()
        ram_gb = get_total_ram_gb()
        hw_family = detect_hw_encoder_family() if self.ffmpeg_ok else None
        self.root.after(0, self._on_hardware_detected, cpu_count, ram_gb, hw_family)

    def _on_hardware_detected(self, cpu_count, ram_gb, hw_family):
        self.hw_family = hw_family
        ram_text = f"{ram_gb:.0f} GB" if ram_gb else "desconhecida"

        if hw_family:
            gpu_text = f"{HW_ENCODER_FAMILIES[hw_family]['label']} (aceleração de GPU disponível)"
        else:
            gpu_text = "nenhuma aceleração de vídeo por GPU detectada neste sistema"

        self.hw_info_text.set(
            f"CPU: {cpu_count} núcleos lógicos   |   RAM: {ram_text}   |   GPU: {gpu_text}"
        )

        if hw_family:
            label = HW_ENCODER_FAMILIES[hw_family]["label"]
            self.gpu_checkbox.configure(text=f"Usar aceleração de GPU ({label} detectado)")
            self.gpu_checkbox.state(["!disabled"])
            # Como a máquina suporta, já deixa marcado por padrão para
            # aproveitar a velocidade (o usuário pode desmarcar se preferir
            # a qualidade um pouco maior da codificação por CPU).
            self.use_gpu.set(True)
        else:
            self.gpu_checkbox.configure(text="Usar aceleração de GPU (indisponível neste sistema)")

    # -- seleção de arquivos ------------------------------------------------

    def _select_input(self):
        path = filedialog.askopenfilename(
            title="Selecione o vídeo de entrada", filetypes=VIDEO_FILETYPES
        )
        if not path:
            return
        self.input_path.set(path)

        # Sugere automaticamente um nome de saída e mostra a extensão (fixa,
        # igual a de entrada, para manter o mesmo container de vídeo).
        base_name = os.path.splitext(os.path.basename(path))[0]
        ext = os.path.splitext(path)[1] or ".mp4"
        self.output_name.set(f"{base_name}_compactado")
        self.output_ext_label.set(ext)

    def _select_output_dir(self):
        path = filedialog.askdirectory(title="Selecione a pasta de saída")
        if path:
            self.output_dir.set(path)

    # -- compressão -----------------------------------------------------

    def _start_compression(self):
        """Valida os campos e dispara a compressão em uma thread separada
        (senão a janela do tkinter congela enquanto o ffmpeg roda)."""

        if not self.ffmpeg_ok:
            messagebox.showerror("Erro", "FFmpeg não está disponível.")
            return

        input_path = self.input_path.get()
        output_dir = self.output_dir.get()

        if not input_path or not os.path.isfile(input_path):
            messagebox.showerror("Erro", "Selecione um arquivo de vídeo válido.")
            return

        if not output_dir or not os.path.isdir(output_dir):
            messagebox.showerror("Erro", "Selecione uma pasta de saída válida.")
            return

        ext = self.output_ext_label.get() or ".mp4"
        output_name = self._sanitize_filename(self.output_name.get(), input_path)
        output_path = os.path.join(output_dir, f"{output_name}{ext}")

        crf = QUALITY_PRESETS[self.quality_label.get()]
        base_codec = CODEC_OPTIONS[self.codec_label.get()]
        use_gpu = bool(self.use_gpu.get() and self.hw_family)

        # Evita clicar duas vezes enquanto já está processando
        self.compress_button.state(["disabled"])
        self.result_text.set("")
        self.status_text.set("Analisando vídeo e estimando o tempo...")
        self.progress.start(10)

        thread = threading.Thread(
            target=self._run_ffmpeg,
            args=(input_path, output_path, crf, base_codec, ext.lower(), use_gpu, self.hw_family),
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _sanitize_filename(name: str, input_path: str) -> str:
        """Remove caracteres inválidos em nomes de arquivo do Windows. Se o
        campo ficar vazio, volta pro nome padrão sugerido."""
        cleaned = INVALID_FILENAME_CHARS.sub("", name).strip()
        if cleaned:
            return cleaned
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        return f"{base_name}_compactado"

    def _processing_message(self, use_gpu, hw_family, eta_seconds) -> str:
        if use_gpu and hw_family:
            base = f"Processando com aceleração de GPU ({HW_ENCODER_FAMILIES[hw_family]['label']})"
        else:
            base = "Processando via CPU (software)"
        if eta_seconds:
            return f"{base}... tempo estimado: ~{format_duration(eta_seconds)}"
        return f"{base}... isso pode levar alguns minutos."

    def _run_ffmpeg(self, input_path, output_path, crf, base_codec, ext, use_gpu, hw_family):
        """Roda o ffmpeg de fato. Executa em thread separada da UI."""

        # 1) Estima o tempo total rodando uma amostra curta com as mesmas
        # configurações escolhidas (ver estimate_compression_seconds).
        duration = probe_duration_seconds(input_path)
        eta_seconds = estimate_compression_seconds(
            input_path, duration, crf, base_codec, ext, use_gpu, hw_family
        )
        message = self._processing_message(use_gpu, hw_family, eta_seconds)
        self.root.after(0, self.status_text.set, message)

        # 2) Roda a compressão de verdade, do vídeo inteiro.
        cmd = ["ffmpeg", "-y"]  # -y sobrescreve o arquivo de saída se já existir

        if use_gpu and hw_family:
            # "auto" deixa o FFmpeg escolher o método de decodificação por
            # hardware disponível (D3D11VA/DXVA2/CUDA/QSV conforme a GPU) -
            # isso tira a decodificação do vídeo de entrada da CPU também,
            # que é o gargalo mais comum quando só a codificação usa GPU.
            cmd += ["-hwaccel", "auto"]

        cmd += ["-i", input_path]
        cmd += build_video_args(crf, base_codec, use_gpu, hw_family)
        cmd += ["-c:a", "aac", "-b:a", "128k"]

        # Correção de compatibilidade conhecida: players da Apple/Windows só
        # reconhecem H.265 dentro de MP4/MOV se a tag do codec for "hvc1".
        if base_codec == "libx265" and ext in (".mp4", ".mov", ".m4v"):
            cmd += ["-tag:v", "hvc1"]

        cmd.append(output_path)

        try:
            start = time.perf_counter()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=SUBPROCESS_FLAGS,
            )
            elapsed = time.perf_counter() - start
        except FileNotFoundError:
            self.root.after(
                0, self._on_error, "FFmpeg não foi encontrado ao tentar executar."
            )
            return
        except Exception as exc:  # erro inesperado de sistema
            self.root.after(0, self._on_error, f"Erro inesperado: {exc}")
            return

        if result.returncode != 0:
            # Pega só as últimas linhas do stderr do ffmpeg pra não poluir o popup
            error_lines = result.stderr.strip().splitlines()
            error_summary = "\n".join(error_lines[-8:]) if error_lines else "Erro desconhecido."
            hint = ""
            if use_gpu:
                hint = (
                    "\n\nDica: se o erro for relacionado ao encoder de GPU, "
                    "desmarque 'Usar aceleração de GPU' e tente novamente "
                    "(codificação por CPU), ou atualize o driver de vídeo."
                )
            self.root.after(0, self._on_error, f"FFmpeg falhou:\n\n{error_summary}{hint}")
            return

        self.root.after(0, self._on_success, input_path, output_path, elapsed)

    # -- callbacks de finalização (rodam na thread da UI via root.after) ---

    def _on_success(self, input_path, output_path, elapsed_seconds):
        self.progress.stop()
        self.status_text.set("Concluído!")
        self.compress_button.state(["!disabled"])

        original_size = os.path.getsize(input_path)
        final_size = os.path.getsize(output_path)
        reduction = (1 - final_size / original_size) * 100 if original_size else 0

        self.result_text.set(
            f"Arquivo original: {self._format_size(original_size)}\n"
            f"Arquivo compactado: {self._format_size(final_size)}\n"
            f"Redução: {reduction:.1f}%\n"
            f"Tempo total: {format_duration(elapsed_seconds)}\n"
            f"Salvo em: {output_path}"
        )

    def _on_error(self, message: str):
        self.progress.stop()
        self.status_text.set("Erro na compressão.")
        self.compress_button.state(["!disabled"])
        messagebox.showerror("Erro na compressão", message)

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        mb = num_bytes / (1024 * 1024)
        return f"{mb:.2f} MB"


def main():
    root = tk.Tk()
    VideoCompressorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
