# Video Compressor

Programa desktop simples (Python + tkinter) para compactar vídeos usando FFmpeg, para uso pessoal.

## Funcionalidades

- Seleção do vídeo de entrada, da pasta de saída e do nome do arquivo final.
- Nível de qualidade/compressão (Alta qualidade / Balanceado / Máxima compressão).
- Escolha de codec de saída: H.264 (`libx264`) ou H.265 (`libx265`).
- **Aceleração por GPU:** detecta automaticamente a GPU instalada (NVIDIA/AMD/Intel)
  cruzando com o que o FFmpeg instalado suporta, e usa o encoder de hardware
  correspondente (NVENC/AMF/Quick Sync) tanto para decodificar quanto para
  codificar o vídeo, quando disponível. Pode ser desligado a qualquer momento
  para usar a codificação por CPU (`libx264`/`libx265`).
- Painel "Sistema detectado" mostrando núcleos de CPU, RAM total e GPU disponível.
- **Estimativa de tempo:** antes de comprimir o vídeo inteiro, roda uma amostra
  curta (~5s) com as mesmas configurações escolhidas, mede a velocidade real
  nesta máquina e extrapola para a duração total.
- Compressão roda em thread separada, com barra de progresso indeterminada e status.
- Mostra tamanho do arquivo original vs. final, tempo total gasto e a % de redução ao concluir.
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
