"""
Compressor de Video - programa desktop simples para compactar videos usando FFmpeg.

Uso: selecione um video, escolha pasta de saida, nome do arquivo final, nivel
de qualidade e codec, clique em "Compactar". O programa chama o FFmpeg
(instalado no sistema) via subprocess e mostra o tamanho antes/depois ao
terminar.

Aceleracao por GPU: ao abrir, o programa detecta se o FFmpeg instalado tem
suporte a um encoder de video por hardware (NVIDIA NVENC, AMD AMF ou Intel
Quick Sync) e, se tiver, oferece a opcao de usar a GPU tanto para decodificar
quanto para codificar o video - isso costuma acelerar MUITO a compressao,
porque tira o trabalho pesado da CPU.

Dependencia externa: FFmpeg precisa estar instalado e acessivel no PATH do
Windows (o programa verifica isso ao abrir). Veja o aviso em check_ffmpeg().
"""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Configuracao das opcoes que aparecem na interface
# ---------------------------------------------------------------------------

# Mapeia o texto amigavel do dropdown para o valor de qualidade que vai pro
# FFmpeg. Para codificacao por CPU (libx264/libx265) isso e o CRF; para
# codificacao por GPU e usado como QP (Quantization Parameter) - as duas
# escalas sao parecidas na pratica (menor = mais qualidade/arquivo maior).
QUALITY_PRESETS = {
    "Alta qualidade (arquivo maior)": 18,
    "Balanceado": 23,
    "Maxima compressao (arquivo menor)": 30,
}

# Mapeia o texto amigavel do dropdown para o nome do encoder de software do
# FFmpeg. Quando a aceleracao por GPU esta ativa, o encoder real usado e
# trocado pelo equivalente de hardware (ver HW_ENCODER_FAMILIES abaixo).
CODEC_OPTIONS = {
    "H.264 (libx264) - mais compativel": "libx264",
    "H.265 (libx265) - arquivos menores": "libx265",
}

# Familias de encoder por hardware que o programa sabe usar, em ordem de
# preferencia quando mais de uma estiver disponivel no sistema.
HW_ENCODER_FAMILIES = {
    "nvenc": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "label": "NVIDIA NVENC"},
    "amf": {"h264": "h264_amf", "hevc": "hevc_amf", "label": "AMD AMF"},
    "qsv": {"h264": "h264_qsv", "hevc": "hevc_qsv", "label": "Intel Quick Sync"},
}

VIDEO_FILETYPES = [
    ("Arquivos de video", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v"),
    ("Todos os arquivos", "*.*"),
]

# Caracteres invalidos em nomes de arquivo no Windows.
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# No Windows, essa flag evita que uma janela de console preta pisque atras
# do app toda vez que o FFmpeg for chamado via subprocess.
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Verificacao do FFmpeg
# ---------------------------------------------------------------------------

def check_ffmpeg() -> bool:
    """Retorna True se o executavel 'ffmpeg' estiver acessivel no PATH.

    shutil.which() e a forma correta de checar isso: ele procura o
    executavel nas mesmas pastas que o Windows procura, sem precisar
    rodar o programa. Nao depende de caminho relativo, entao continua
    funcionando normalmente depois de empacotado com o PyInstaller.
    """
    return shutil.which("ffmpeg") is not None


FFMPEG_MISSING_MESSAGE = (
    "FFmpeg nao foi encontrado no PATH do sistema.\n\n"
    "Este programa depende do FFmpeg para compactar videos, mas ele nao "
    "vem embutido no executavel - precisa ser instalado separadamente.\n\n"
    "Como instalar no Windows:\n"
    "1) Abra o PowerShell e rode:\n"
    "   winget install ffmpeg\n"
    "   (ou baixe em https://www.gyan.dev/ffmpeg/builds/ e adicione a "
    "pasta 'bin' extraida ao PATH do Windows)\n"
    "2) Feche e abra este programa novamente.\n\n"
    "O botao 'Compactar' ficara desabilitado ate o FFmpeg ser encontrado."
)


# ---------------------------------------------------------------------------
# Deteccao de hardware (CPU, RAM, GPU) - usada so para acelerar a compressao
# e informar o usuario; nada disso e obrigatorio para o programa funcionar.
# ---------------------------------------------------------------------------

def get_cpu_count() -> int:
    return os.cpu_count() or 1


def get_total_ram_gb():
    """Le a RAM total do Windows via ctypes (API GlobalMemoryStatusEx),
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
    """Pergunta ao FFmpeg instalado quais encoders de video por hardware ele
    SABE compilar (isso so depende de como o FFmpeg foi compilado, nao do
    que esta instalado na maquina - um "full build" normalmente vem com
    NVENC, AMF e Quick Sync juntos, mesmo que so uma GPU exista de fato)."""
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
    """Descobre quais fabricantes de GPU estao FISICAMENTE instalados na
    maquina, consultando o WMI via PowerShell. Isso e o que garante que a
    aceleracao oferecida na UI realmente bate com o hardware do usuario -
    so checar o que o FFmpeg foi compilado pra suportar nao e suficiente."""
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


# Fabricante de GPU -> familia de encoder correspondente no FFmpeg.
VENDOR_TO_FAMILY = {"nvidia": "nvenc", "amd": "amf", "intel": "qsv"}


def detect_hw_encoder_family():
    """Cruza a GPU realmente instalada na maquina com o que esse FFmpeg
    sabe codificar, e retorna a familia utilizavel ("nvenc"/"amf"/"qsv"),
    ou None se nao houver GPU com suporte disponivel."""
    supported = get_ffmpeg_supported_hw_families()
    if not supported:
        return None

    installed_vendors = get_installed_gpu_vendors()

    # Prioridade quando ha mais de uma GPU (ex: notebook com iGPU Intel +
    # dGPU dedicada): NVIDIA > AMD > Intel, pela ordem tipica de desempenho.
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


# ---------------------------------------------------------------------------
# Interface grafica
# ---------------------------------------------------------------------------

class VideoCompressorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Compressor de Video")
        self.root.geometry("600x560")
        self.root.resizable(False, False)

        # Variaveis ligadas aos campos da UI
        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.output_name = tk.StringVar()
        self.output_ext_label = tk.StringVar(value="")
        self.quality_label = tk.StringVar(value="Balanceado")
        self.codec_label = tk.StringVar(value=next(iter(CODEC_OPTIONS)))
        self.use_gpu = tk.BooleanVar(value=False)
        self.hw_info_text = tk.StringVar(value="Detectando hardware disponivel...")
        self.status_text = tk.StringVar(value="Aguardando...")
        self.result_text = tk.StringVar(value="")

        self.ffmpeg_ok = check_ffmpeg()
        self.hw_family = None  # preenchido depois pela deteccao em background

        self._build_ui()

        # Se o FFmpeg nao estiver disponivel, avisa assim que a janela abre.
        if not self.ffmpeg_ok:
            self.root.after(200, self._warn_ffmpeg_missing)

        # A deteccao de hardware (rodar "ffmpeg -encoders", ler RAM) roda em
        # background pra nao atrasar a abertura da janela.
        threading.Thread(target=self._detect_hardware, daemon=True).start()

    # -- construcao da UI ---------------------------------------------------

    def _build_ui(self):
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        # --- Secao: arquivos -------------------------------------------------
        file_frame = ttk.LabelFrame(main, text="Arquivo")
        file_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(file_frame, text="Video de entrada:").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2)
        )
        ttk.Entry(file_frame, textvariable=self.input_path, width=58, state="readonly").grid(
            row=1, column=0, sticky="we", padx=(10, 6)
        )
        ttk.Button(file_frame, text="Selecionar...", command=self._select_input).grid(
            row=1, column=1, padx=(0, 10)
        )

        ttk.Label(file_frame, text="Pasta de saida:").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2)
        )
        ttk.Entry(file_frame, textvariable=self.output_dir, width=58, state="readonly").grid(
            row=3, column=0, sticky="we", padx=(10, 6)
        )
        ttk.Button(file_frame, text="Selecionar...", command=self._select_output_dir).grid(
            row=3, column=1, padx=(0, 10)
        )

        ttk.Label(file_frame, text="Nome do arquivo de saida:").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 2)
        )
        name_row = ttk.Frame(file_frame)
        name_row.grid(row=5, column=0, columnspan=2, sticky="we", padx=10, pady=(0, 10))
        ttk.Entry(name_row, textvariable=self.output_name, width=45).pack(side="left")
        ttk.Label(name_row, textvariable=self.output_ext_label).pack(side="left", padx=(4, 0))

        file_frame.columnconfigure(0, weight=1)

        # --- Secao: opcoes de compressao -------------------------------------
        options_frame = ttk.LabelFrame(main, text="Opcoes de compressao")
        options_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(options_frame, text="Qualidade / compressao:").grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2)
        )
        ttk.Combobox(
            options_frame,
            textvariable=self.quality_label,
            values=list(QUALITY_PRESETS.keys()),
            state="readonly",
            width=28,
        ).grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

        ttk.Label(options_frame, text="Codec de saida:").grid(
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
            text="Usar aceleracao de GPU (indisponivel)",
            variable=self.use_gpu,
        )
        self.gpu_checkbox.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))
        self.gpu_checkbox.state(["disabled"])

        # --- Secao: hardware detectado ---------------------------------------
        hw_frame = ttk.LabelFrame(main, text="Sistema detectado")
        hw_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(hw_frame, textvariable=self.hw_info_text, justify="left").pack(
            anchor="w", padx=10, pady=8
        )

        # --- Botao compactar + progresso -------------------------------------
        self.compress_button = ttk.Button(
            main, text="Compactar", command=self._start_compression
        )
        self.compress_button.pack(pady=(4, 8))
        if not self.ffmpeg_ok:
            self.compress_button.state(["disabled"])

        # Barra de progresso (modo indeterminado: nao da pra saber o % exato
        # so lendo stdout do ffmpeg de forma simples, entao usamos uma barra
        # "ocupado" + texto de status, como combinado)
        self.progress = ttk.Progressbar(main, mode="indeterminate", length=460)
        self.progress.pack(pady=(0, 6))

        ttk.Label(main, textvariable=self.status_text).pack(pady=(0, 8))
        ttk.Label(main, textvariable=self.result_text, justify="left").pack(anchor="w")

    def _warn_ffmpeg_missing(self):
        messagebox.showwarning("FFmpeg nao encontrado", FFMPEG_MISSING_MESSAGE)

    # -- deteccao de hardware em background -------------------------------

    def _detect_hardware(self):
        cpu_count = get_cpu_count()
        ram_gb = get_total_ram_gb()
        hw_family = detect_hw_encoder_family() if self.ffmpeg_ok else None
        self.root.after(0, self._on_hardware_detected, cpu_count, ram_gb, hw_family)

    def _on_hardware_detected(self, cpu_count, ram_gb, hw_family):
        self.hw_family = hw_family
        ram_text = f"{ram_gb:.0f} GB" if ram_gb else "desconhecida"

        if hw_family:
            gpu_text = f"{HW_ENCODER_FAMILIES[hw_family]['label']} (aceleracao de GPU disponivel)"
        else:
            gpu_text = "nenhuma aceleracao de video por GPU detectada neste FFmpeg"

        self.hw_info_text.set(
            f"CPU: {cpu_count} nucleos logicos   |   RAM: {ram_text}   |   GPU: {gpu_text}"
        )

        if hw_family:
            label = HW_ENCODER_FAMILIES[hw_family]["label"]
            self.gpu_checkbox.configure(text=f"Usar aceleracao de GPU ({label} detectado)")
            self.gpu_checkbox.state(["!disabled"])
            # Como a maquina suporta, ja deixa marcado por padrao para
            # aproveitar a velocidade (o usuario pode desmarcar se preferir
            # a qualidade um pouco maior da codificacao por CPU).
            self.use_gpu.set(True)
        else:
            self.gpu_checkbox.configure(text="Usar aceleracao de GPU (indisponivel neste sistema)")

    # -- selecao de arquivos --------------------------------------------

    def _select_input(self):
        path = filedialog.askopenfilename(
            title="Selecione o video de entrada", filetypes=VIDEO_FILETYPES
        )
        if not path:
            return
        self.input_path.set(path)

        # Sugere automaticamente um nome de saida e mostra a extensao (fixa,
        # igual a de entrada, para manter o mesmo container de video).
        base_name = os.path.splitext(os.path.basename(path))[0]
        ext = os.path.splitext(path)[1] or ".mp4"
        self.output_name.set(f"{base_name}_compactado")
        self.output_ext_label.set(ext)

    def _select_output_dir(self):
        path = filedialog.askdirectory(title="Selecione a pasta de saida")
        if path:
            self.output_dir.set(path)

    # -- compressao -------------------------------------------------------

    def _start_compression(self):
        """Valida os campos e dispara a compressao em uma thread separada
        (senao a janela do tkinter congela enquanto o ffmpeg roda)."""

        if not self.ffmpeg_ok:
            messagebox.showerror("Erro", "FFmpeg nao esta disponivel.")
            return

        input_path = self.input_path.get()
        output_dir = self.output_dir.get()

        if not input_path or not os.path.isfile(input_path):
            messagebox.showerror("Erro", "Selecione um arquivo de video valido.")
            return

        if not output_dir or not os.path.isdir(output_dir):
            messagebox.showerror("Erro", "Selecione uma pasta de saida valida.")
            return

        ext = self.output_ext_label.get() or ".mp4"
        output_name = self._sanitize_filename(self.output_name.get(), input_path)
        output_path = os.path.join(output_dir, f"{output_name}{ext}")

        crf = QUALITY_PRESETS[self.quality_label.get()]
        base_codec = CODEC_OPTIONS[self.codec_label.get()]
        use_gpu = bool(self.use_gpu.get() and self.hw_family)

        # Evita clicar duas vezes enquanto ja esta processando
        self.compress_button.state(["disabled"])
        self.result_text.set("")
        if use_gpu:
            label = HW_ENCODER_FAMILIES[self.hw_family]["label"]
            self.status_text.set(f"Processando com aceleracao de GPU ({label})...")
        else:
            self.status_text.set("Processando via CPU (software)... isso pode levar alguns minutos.")
        self.progress.start(10)

        thread = threading.Thread(
            target=self._run_ffmpeg,
            args=(input_path, output_path, crf, base_codec, ext.lower(), use_gpu, self.hw_family),
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _sanitize_filename(name: str, input_path: str) -> str:
        """Remove caracteres invalidos em nomes de arquivo do Windows. Se o
        campo ficar vazio, volta pro nome padrao sugerido."""
        cleaned = INVALID_FILENAME_CHARS.sub("", name).strip()
        if cleaned:
            return cleaned
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        return f"{base_name}_compactado"

    def _run_ffmpeg(self, input_path, output_path, crf, base_codec, ext, use_gpu, hw_family):
        """Roda o ffmpeg de fato. Executa em thread separada da UI."""

        cmd = ["ffmpeg", "-y"]  # -y sobrescreve o arquivo de saida se ja existir

        if use_gpu and hw_family:
            # "auto" deixa o FFmpeg escolher o metodo de decodificacao por
            # hardware disponivel (D3D11VA/DXVA2/CUDA/QSV conforme a GPU) -
            # isso tira a decodificacao do video de entrada da CPU tambem,
            # que e o gargalo mais comum quando so a codificacao usa GPU.
            cmd += ["-hwaccel", "auto"]

        cmd += ["-i", input_path]

        if use_gpu and hw_family:
            encoder = hw_encoder_name(hw_family, base_codec)
            cmd += ["-c:v", encoder] + hw_quality_args(hw_family, crf)
        else:
            cmd += ["-c:v", base_codec, "-crf", str(crf), "-preset", "medium"]

        cmd += ["-c:a", "aac", "-b:a", "128k"]

        # Correcao de compatibilidade conhecida: players da Apple/Windows so
        # reconhecem H.265 dentro de MP4/MOV se a tag do codec for "hvc1".
        if base_codec == "libx265" and ext in (".mp4", ".mov", ".m4v"):
            cmd += ["-tag:v", "hvc1"]

        cmd.append(output_path)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=SUBPROCESS_FLAGS,
            )
        except FileNotFoundError:
            self.root.after(
                0, self._on_error, "FFmpeg nao foi encontrado ao tentar executar."
            )
            return
        except Exception as exc:  # erro inesperado de sistema
            self.root.after(0, self._on_error, f"Erro inesperado: {exc}")
            return

        if result.returncode != 0:
            # Pega so as ultimas linhas do stderr do ffmpeg pra nao poluir o popup
            error_lines = result.stderr.strip().splitlines()
            error_summary = "\n".join(error_lines[-8:]) if error_lines else "Erro desconhecido."
            hint = ""
            if use_gpu:
                hint = (
                    "\n\nDica: se o erro for relacionado ao encoder de GPU, "
                    "desmarque 'Usar aceleracao de GPU' e tente novamente "
                    "(codificacao por CPU), ou atualize o driver de video."
                )
            self.root.after(0, self._on_error, f"FFmpeg falhou:\n\n{error_summary}{hint}")
            return

        self.root.after(0, self._on_success, input_path, output_path)

    # -- callbacks de finalizacao (rodam na thread da UI via root.after) ---

    def _on_success(self, input_path, output_path):
        self.progress.stop()
        self.status_text.set("Concluido!")
        self.compress_button.state(["!disabled"])

        original_size = os.path.getsize(input_path)
        final_size = os.path.getsize(output_path)
        reduction = (1 - final_size / original_size) * 100 if original_size else 0

        self.result_text.set(
            f"Arquivo original: {self._format_size(original_size)}\n"
            f"Arquivo compactado: {self._format_size(final_size)}\n"
            f"Reducao: {reduction:.1f}%\n"
            f"Salvo em: {output_path}"
        )

    def _on_error(self, message: str):
        self.progress.stop()
        self.status_text.set("Erro na compressao.")
        self.compress_button.state(["!disabled"])
        messagebox.showerror("Erro na compressao", message)

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
