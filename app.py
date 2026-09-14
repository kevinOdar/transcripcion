import io
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

MODEL_SIZES = ["small", "medium", "large-v3", "large-v3-turbo"]
LANGUAGES = {
    "Detectar automáticamente": None,
    "Español": "es",
    "Inglés": "en",
}

AUDIO_FILETYPES = [
    ("Audio", "*.mp3 *.m4a *.wav *.flac *.ogg *.aac *.wma *.mp4 *.mkv"),
    ("Todos los archivos", "*.*"),
]

# faster-whisper descarga los modelos vía huggingface_hub. Al bajarlo por primera
# vez, snapshot_download reporta el avance en bytes a través de una barra tqdm
# interna con desc="Downloading bytes"; parcheamos esa clase para reenviar el
# porcentaje a la app en lugar de imprimirla en consola.
_download_progress_callback = None
_download_progress_hook_installed = False


def _install_download_progress_hook():
    global _download_progress_hook_installed
    if _download_progress_hook_installed:
        return
    try:
        import faster_whisper.utils as fw_utils
        from tqdm.auto import tqdm as tqdm_auto

        class _ProgressTqdm(tqdm_auto):
            def __init__(self, *args, **kwargs):
                self._is_transfer_bar = kwargs.get("desc") == "Downloading bytes"
                kwargs["disable"] = False
                kwargs["file"] = io.StringIO()  # evita imprimir la barra en consola
                super().__init__(*args, **kwargs)

            def update(self, n=1):
                result = super().update(n)
                if self._is_transfer_bar and _download_progress_callback and self.total:
                    pct = max(0.0, min(100.0, self.n / self.total * 100))
                    try:
                        _download_progress_callback(pct)
                    except Exception:
                        pass
                return result

        fw_utils.disabled_tqdm = _ProgressTqdm
        _download_progress_hook_installed = True
    except Exception:
        pass


class TranscriberApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Transcriptor de audio")
        self.root.geometry("800x600")
        self.root.minsize(600, 400)

        self.audio_path = tk.StringVar()
        self.model_size = tk.StringVar(value="medium")
        self.language_label = tk.StringVar(value="Detectar automáticamente")

        self._model_cache = {}  # (size) -> WhisperModel
        self._queue = queue.Queue()
        self._worker_running = False
        self._transcribe_duration = 0.0

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        file_frame = ttk.Frame(self.root)
        file_frame.pack(fill="x", **pad)

        ttk.Label(file_frame, text="Archivo:").pack(side="left")
        entry = ttk.Entry(file_frame, textvariable=self.audio_path, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(file_frame, text="Examinar...", command=self._browse_file).pack(side="left")

        options_frame = ttk.Frame(self.root)
        options_frame.pack(fill="x", **pad)

        ttk.Label(options_frame, text="Modelo:").pack(side="left")
        model_combo = ttk.Combobox(
            options_frame, textvariable=self.model_size, values=MODEL_SIZES,
            state="readonly", width=15,
        )
        model_combo.pack(side="left", padx=(4, 16))

        ttk.Label(options_frame, text="Idioma:").pack(side="left")
        lang_combo = ttk.Combobox(
            options_frame, textvariable=self.language_label, values=list(LANGUAGES.keys()),
            state="readonly", width=22,
        )
        lang_combo.pack(side="left", padx=4)

        self.transcribe_btn = ttk.Button(
            options_frame, text="Transcribir", command=self._start_transcription
        )
        self.transcribe_btn.pack(side="right")

        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill="x", **pad)
        self.status_label = ttk.Label(progress_frame, text="Listo.")
        self.status_label.pack(side="left")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(side="right", fill="x", expand=True, padx=(12, 0))

        text_frame = ttk.Frame(self.root)
        text_frame.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.text_area = scrolledtext.ScrolledText(text_frame, wrap="word", font=("Segoe UI", 11))
        self.text_area.pack(fill="both", expand=True)

        bottom_frame = ttk.Frame(self.root)
        bottom_frame.pack(fill="x", **pad)
        ttk.Button(bottom_frame, text="Guardar como .txt", command=self._save_txt).pack(side="left")
        ttk.Button(bottom_frame, text="Copiar al portapapeles", command=self._copy_clipboard).pack(side="left", padx=6)
        ttk.Button(bottom_frame, text="Limpiar", command=self._clear_text).pack(side="left")

    def _browse_file(self):
        path = filedialog.askopenfilename(title="Selecciona un archivo de audio", filetypes=AUDIO_FILETYPES)
        if path:
            self.audio_path.set(path)

    def _set_busy(self, busy: bool, status: str = ""):
        self._worker_running = busy
        self.transcribe_btn.config(state="disabled" if busy else "normal")
        if busy:
            self._transcribe_duration = 0.0
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=100)
            self.progress["value"] = 0
        if status:
            self.status_label.config(text=status)

    def _start_transcription(self):
        path = self.audio_path.get().strip()
        if not path:
            messagebox.showwarning("Falta archivo", "Selecciona primero un archivo de audio.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("Archivo no encontrado", f"No existe el archivo:\n{path}")
            return
        if self._worker_running:
            return

        self.text_area.delete("1.0", tk.END)
        self._set_busy(True, "Cargando modelo (puede tardar la primera vez)...")

        model_size = self.model_size.get()
        language = LANGUAGES.get(self.language_label.get())

        thread = threading.Thread(
            target=self._transcribe_worker, args=(path, model_size, language), daemon=True
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _get_model(self, model_size: str):
        model = self._model_cache.get(model_size)
        if model is None:
            from faster_whisper import WhisperModel

            global _download_progress_callback
            _install_download_progress_hook()
            _download_progress_callback = lambda pct: self._queue.put(("download_progress", pct))
            try:
                model = WhisperModel(model_size, device="cpu", compute_type="int8")
            finally:
                _download_progress_callback = None
            self._model_cache[model_size] = model
        return model

    def _transcribe_worker(self, path, model_size, language):
        try:
            model = self._get_model(model_size)

            segments, info = model.transcribe(
                path,
                language=language,
                beam_size=5,
                vad_filter=True,
            )
            self._queue.put(("transcribe_total", info.duration or 0.0))

            for segment in segments:
                text = segment.text.strip()
                if text:
                    self._queue.put(("segment_text", text))
                self._queue.put(("transcribe_progress", segment.end))

            detected = f" (idioma detectado: {info.language})" if language is None else ""
            self._queue.put(("done", detected))
        except Exception as exc:  # noqa: BLE001
            self._queue.put(("error", str(exc)))

    def _poll_queue(self):
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "status":
                    self.status_label.config(text=item[1])
                elif kind == "download_progress":
                    pct = item[1]
                    if str(self.progress["mode"]) != "determinate":
                        self.progress.stop()
                        self.progress.config(mode="determinate", maximum=100)
                    self.progress["value"] = pct
                    self.status_label.config(text=f"Descargando modelo... {pct:.0f}%")
                elif kind == "transcribe_total":
                    self._transcribe_duration = item[1]
                    self.progress.stop()
                    if self._transcribe_duration > 0:
                        self.progress.config(mode="determinate", maximum=100)
                        self.progress["value"] = 0
                        self.status_label.config(text="Transcribiendo... 0%")
                    else:
                        self.progress.config(mode="indeterminate")
                        self.progress.start(12)
                        self.status_label.config(text="Transcribiendo audio...")
                elif kind == "transcribe_progress":
                    if self._transcribe_duration > 0:
                        pct = max(0.0, min(100.0, item[1] / self._transcribe_duration * 100))
                        self.progress["value"] = pct
                        self.status_label.config(
                            text=f"Transcribiendo... {pct:.0f}% ({item[1]:.0f}s / {self._transcribe_duration:.0f}s)"
                        )
                elif kind == "segment_text":
                    if self.text_area.index("end-1c") != "1.0":
                        self.text_area.insert(tk.END, " ")
                    self.text_area.insert(tk.END, item[1])
                    self.text_area.see(tk.END)
                elif kind == "done":
                    detected = item[1]
                    self._set_busy(False, f"Listo.{detected}")
                    return
                elif kind == "error":
                    self._set_busy(False, "Error.")
                    messagebox.showerror("Error al transcribir", item[1])
                    return
        except queue.Empty:
            pass

        if self._worker_running:
            self.root.after(100, self._poll_queue)

    def _save_txt(self):
        text = self.text_area.get("1.0", tk.END).strip()
        if not text:
            messagebox.showinfo("Nada que guardar", "No hay texto para guardar todavía.")
            return
        default_name = "transcripcion.txt"
        src = self.audio_path.get()
        if src:
            default_name = os.path.splitext(os.path.basename(src))[0] + ".txt"
        path = filedialog.asksaveasfilename(
            title="Guardar transcripción", defaultextension=".txt",
            initialfile=default_name, filetypes=[("Archivo de texto", "*.txt")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            messagebox.showinfo("Guardado", f"Transcripción guardada en:\n{path}")

    def _copy_clipboard(self):
        text = self.text_area.get("1.0", tk.END).strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _clear_text(self):
        self.text_area.delete("1.0", tk.END)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = TranscriberApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
