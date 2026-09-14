# Transcriptor de audio

App de escritorio (Windows) para transcribir archivos de audio a texto de forma local, usando [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (implementación optimizada de Whisper de OpenAI). No requiere conexión a internet salvo la primera vez que se usa cada modelo (se descarga y queda en caché).

## Instalación

```powershell
python -m venv venv
./venv/Scripts/pip install -r requirements.txt
```

## Uso

Doble clic en `run.bat`, o desde PowerShell:

```powershell
./venv/Scripts/python app.py
```

**Importante:** hay que ejecutar siempre con el Python del `venv` (no el Python del sistema), porque ahí están instaladas las dependencias. Si lo abres/ejecutas desde VS Code con el botón "Run", asegúrate de que el intérprete seleccionado sea `venv/Scripts/python.exe` (ya está configurado en `.vscode/settings.json`, pero si sigue fallando con "No module named 'faster_whisper'", selecciónalo manualmente con Ctrl+Shift+P → "Python: Select Interpreter").

1. Clic en **Examinar...** y selecciona el archivo de audio (mp3, m4a, wav, flac, ogg, aac, etc.).
2. Elige el modelo (más grande = mejor calidad pero más lento) y el idioma (o déjalo en automático).
3. Clic en **Transcribir**. La primera vez que uses un modelo se descargará desde Hugging Face (puede tardar unos minutos).
4. El texto transcrito aparece en pantalla. Puedes guardarlo como `.txt` o copiarlo al portapapeles.

## Notas

- Todo corre en CPU (no se detectó GPU NVIDIA compatible con CUDA en este equipo).
- El modelo `large-v3` da la mejor calidad pero es más lento; `medium` es un buen equilibrio; `small` es el más rápido.
- No se muestran marcas de tiempo por frase, solo el texto completo transcrito.
