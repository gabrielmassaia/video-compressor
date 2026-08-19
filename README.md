# Video Compressor

Programa desktop simples (Python + tkinter) para compactar vídeos usando FFmpeg, para uso pessoal.

## Funcionalidades

- Seleção do vídeo de entrada e da pasta de saída.
- Nível de qualidade/compressão via CRF (Alta qualidade / Balanceado / Máxima compressão).
- Escolha de codec de saída: H.264 (`libx264`) ou H.265 (`libx265`).
- Compressão roda em thread separada, com barra de progresso indeterminada e status.
- Mostra tamanho do arquivo original vs. final e a % de redução ao concluir.
- Verifica se o FFmpeg está no PATH ao abrir e orienta a instalação se não estiver.

## Requisitos

- Python 3.10+ (para rodar via código-fonte).
- [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) instalado e acessível no PATH do sistema.
  No Windows: `winget install ffmpeg`.

O FFmpeg **não** é embutido no executável — precisa estar instalado separadamente no sistema.

## Rodar via código-fonte

```bash
python video_compressor.py
```

## Gerar o executável (.exe)

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name VideoCompressor video_compressor.py
```

O executável final aparece em `dist/VideoCompressor.exe`. Para adicionar um ícone,
inclua `--icon=caminho\para\icone.ico` no comando acima.

## Licença

Uso pessoal.
