"""
Compressor de Video - programa desktop simples para compactar videos usando FFmpeg.

Uso: selecione um video, escolha pasta de saida, nivel de qualidade e codec,
clique em "Compactar". O programa chama o FFmpeg (instalado no sistema) via
subprocess e mostra o tamanho antes/depois ao terminar.

Dependencia externa: FFmpeg precisa estar instalado e acessivel no PATH do
Windows (o programa verifica isso ao abrir). Veja o aviso em check_ffmpeg().
"""

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Configuracao das opcoes que aparecem na interface
# ---------------------------------------------------------------------------

# Mapeia o texto amigavel do dropdown para o valor de CRF que vai pro FFmpeg.
# CRF = "Constant Rate Factor": quanto MENOR, mais qualidade e MAIOR o arquivo;
# quanto MAIOR, mais compressao e MENOR o arquivo. Faixa util: 18 a 32.
QUALITY_PRESETS = {
    "Alta qualidade (arquivo maior)": 18,
    "Balanceado": 23,
    "Maxima compressao (arquivo menor)": 30,
}

# Mapeia o texto amigavel do dropdown para o nome do encoder do FFmpeg.
CODEC_OPTIONS = {
    "H.264 (libx264) - mais compativel": "libx264",
    "H.265 (libx265) - arquivos menores": "libx265",
}

VIDEO_FILETYPES = [
    ("Arquivos de video", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v"),
    ("Todos os arquivos", "*.*"),
]

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
# Interface grafica
# ---------------------------------------------------------------------------

class VideoCompressorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Compressor de Video")
        self.root.geometry("560x430")
        self.root.resizable(False, False)

        # Variaveis ligadas aos campos da UI
        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.quality_label = tk.StringVar(value="Balanceado")
        self.codec_label = tk.StringVar(value=next(iter(CODEC_OPTIONS)))
        self.status_text = tk.StringVar(value="Aguardando...")
        self.result_text = tk.StringVar(value="")

        self.ffmpeg_ok = check_ffmpeg()

        self._build_ui()

        # Se o FFmpeg nao estiver disponivel, avisa assim que a janela abre.
        if not self.ffmpeg_ok:
            self.root.after(200, self._warn_ffmpeg_missing)

    # -- construcao da UI ---------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        # Arquivo de entrada
        ttk.Label(main, text="Arquivo de video de entrada:").grid(
            row=0, column=0, columnspan=2, sticky="w", **pad
        )
        entry_input = ttk.Entry(main, textvariable=self.input_path, width=55, state="readonly")
        entry_input.grid(row=1, column=0, sticky="we", padx=(12, 6))
        ttk.Button(main, text="Selecionar...", command=self._select_input).grid(
            row=1, column=1, padx=(0, 12)
        )

        # Pasta de saida
        ttk.Label(main, text="Pasta de saida:").grid(
            row=2, column=0, columnspan=2, sticky="w", **pad
        )
        entry_output = ttk.Entry(main, textvariable=self.output_dir, width=55, state="readonly")
        entry_output.grid(row=3, column=0, sticky="we", padx=(12, 6))
        ttk.Button(main, text="Selecionar...", command=self._select_output_dir).grid(
            row=3, column=1, padx=(0, 12)
        )

        # Qualidade
        ttk.Label(main, text="Qualidade / compressao:").grid(
            row=4, column=0, sticky="w", **pad
        )
        quality_combo = ttk.Combobox(
            main,
            textvariable=self.quality_label,
            values=list(QUALITY_PRESETS.keys()),
            state="readonly",
            width=30,
        )
        quality_combo.grid(row=5, column=0, sticky="w", padx=12)

        # Codec
        ttk.Label(main, text="Codec de saida:").grid(row=4, column=1, sticky="w", **pad)
        codec_combo = ttk.Combobox(
            main,
            textvariable=self.codec_label,
            values=list(CODEC_OPTIONS.keys()),
            state="readonly",
            width=28,
        )
        codec_combo.grid(row=5, column=1, sticky="w")

        # Botao compactar
        self.compress_button = ttk.Button(
            main, text="Compactar", command=self._start_compression
        )
        self.compress_button.grid(row=6, column=0, columnspan=2, pady=(20, 6))
        if not self.ffmpeg_ok:
            self.compress_button.state(["disabled"])

        # Barra de progresso (modo indeterminado: nao da pra saber o % exato
        # so lendo stdout do ffmpeg de forma simples, entao usamos uma barra
        # "ocupado" + texto de status, como combinado)
        self.progress = ttk.Progressbar(main, mode="indeterminate", length=400)
        self.progress.grid(row=7, column=0, columnspan=2, pady=(4, 4))

        ttk.Label(main, textvariable=self.status_text).grid(
            row=8, column=0, columnspan=2, pady=(2, 10)
        )

        ttk.Label(main, textvariable=self.result_text, justify="left").grid(
            row=9, column=0, columnspan=2, sticky="w", padx=12
        )

        main.columnconfigure(0, weight=1)

    def _warn_ffmpeg_missing(self):
        messagebox.showwarning("FFmpeg nao encontrado", FFMPEG_MISSING_MESSAGE)

    # -- selecao de arquivos --------------------------------------------

    def _select_input(self):
        path = filedialog.askopenfilename(
            title="Selecione o video de entrada", filetypes=VIDEO_FILETYPES
        )
        if path:
            self.input_path.set(path)

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

        crf = QUALITY_PRESETS[self.quality_label.get()]
        codec = CODEC_OPTIONS[self.codec_label.get()]

        # Monta o nome do arquivo de saida: mesmo nome + sufixo, mesma extensao
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        ext = os.path.splitext(input_path)[1] or ".mp4"
        output_path = os.path.join(output_dir, f"{base_name}_compactado{ext}")

        # Evita clicar duas vezes enquanto ja esta processando
        self.compress_button.state(["disabled"])
        self.result_text.set("")
        self.status_text.set("Processando... isso pode levar alguns minutos.")
        self.progress.start(10)

        thread = threading.Thread(
            target=self._run_ffmpeg,
            args=(input_path, output_path, crf, codec, ext.lower()),
            daemon=True,
        )
        thread.start()

    def _run_ffmpeg(self, input_path, output_path, crf, codec, ext):
        """Roda o ffmpeg de fato. Executa em thread separada da UI."""

        cmd = [
            "ffmpeg",
            "-y",  # sobrescreve o arquivo de saida se ja existir
            "-i", input_path,
            "-c:v", codec,
            "-crf", str(crf),
            "-preset", "medium",
            "-c:a", "aac",
            "-b:a", "128k",
        ]

        # Correcao de compatibilidade conhecida: players da Apple/Windows so
        # reconhecem H.265 dentro de MP4/MOV se a tag do codec for "hvc1".
        if codec == "libx265" and ext in (".mp4", ".mov", ".m4v"):
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
            self.root.after(0, self._on_error, f"FFmpeg falhou:\n\n{error_summary}")
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
