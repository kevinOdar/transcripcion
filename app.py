import io
import json
import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, ttk

import sv_ttk

SUMMARY_MODEL = "claude-haiku-4-5"
SUMMARY_SYSTEM_PROMPT = (
    "Resumís transcripciones de audio en español. Devolvé primero un resumen breve "
    "de 2-3 frases y luego una lista de los puntos más importantes, en formato claro y directo."
)
ANTHROPIC_KEYS_URL = "https://console.anthropic.com/settings/keys"

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

FONT_FAMILY = "Segoe UI"

# sv_ttk se encarga del look de los widgets ttk (botones, combos, entradas, tarjetas
# redondeadas) tanto en claro como en oscuro. Estos colores son solo para lo que
# sv_ttk no puede tematizar automáticamente: el área de texto (tk.Text clásico) y
# algunos acentos propios (encabezado, estado). Se releen cada vez que se cambia
# de tema con el interruptor de modo oscuro.
PALETTES = {
    "light": {
        "bg": "#fafafa",
        "text_bg": "#ffffff",
        "text_fg": "#1c1c1c",
        "border": "#e0e0e0",
        "muted": "#6b6b6b",
        "accent": "#005fb8",
        "select_bg": "#cfe0f5",
        "success": "#1f9d55",
        "danger": "#d13438",
    },
    "dark": {
        "bg": "#1c1c1c",
        "text_bg": "#242424",
        "text_fg": "#fafafa",
        "border": "#3a3a3a",
        "muted": "#a0a0a0",
        "accent": "#57c8ff",
        "select_bg": "#294a70",
        "success": "#3fb950",
        "danger": "#ff6b6b",
    },
}


def _config_path():
    # Se guarda fuera de la carpeta del repo (en %APPDATA%) para no arriesgar
    # que la API key termine commiteada por accidente.
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    config_dir = os.path.join(base, "TranscriptorAudio")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "config.json")


def _load_saved_api_key():
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f).get("anthropic_api_key") or None
    except (OSError, ValueError):
        return None


def _save_api_key(key):
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump({"anthropic_api_key": key}, f)
    except OSError:
        pass


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


class Dropdown(ttk.Frame):
    """Selector de opciones propio.

    El popdown nativo de ttk.Combobox es un tk.Listbox del sistema operativo:
    no se puede redondear ni integrar con la paleta de la app. Este widget
    dibuja su propio menú (un Toplevel sin bordes con una tarjeta "Card.TFrame"
    redondeada) para que combine con el resto del diseño en claro y oscuro.
    """

    def __init__(self, parent, variable, values, palette_getter, width=None, placeholder=None):
        super().__init__(parent)
        self._var = variable
        self._values = values
        self._palette_getter = palette_getter
        self._placeholder = placeholder
        self._popup = None

        texts = list(values) + ([placeholder] if placeholder else [])
        auto_width = max((len(t) for t in texts), default=10) + 3

        self.button = ttk.Button(
            self, text=self._display_text(), command=self._toggle, width=width or auto_width,
        )
        self.button.pack(fill="x")
        self._var.trace_add("write", lambda *_: self.button.config(text=self._display_text()))

    def _display_text(self):
        return f"{self._var.get() or self._placeholder}  ▾"

    def set_enabled(self, enabled: bool):
        self.button.config(state="normal" if enabled else "disabled")

    def _toggle(self):
        if self._popup is not None:
            self._close()
        else:
            self._open()

    def _open(self):
        palette = self._palette_getter()

        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=palette["bg"])
        self._popup = popup

        card = ttk.Frame(popup, style="Card.TFrame", padding=6)
        card.pack(fill="both", expand=True)

        for value in self._values:
            row = tk.Label(
                card, text=value, anchor="w", padx=10, pady=7,
                bg=palette["text_bg"], fg=palette["text_fg"], font=(FONT_FAMILY, 10),
                cursor="hand2",
            )
            row.pack(fill="x")
            row.bind("<Enter>", lambda _e, w=row: w.config(bg=palette["select_bg"]))
            row.bind("<Leave>", lambda _e, w=row: w.config(bg=palette["text_bg"]))
            row.bind("<Button-1>", lambda _e, v=value: self._select(v))

        self.update_idletasks()
        width = max(self.button.winfo_width(), popup.winfo_reqwidth())
        x = self.button.winfo_rootx()
        y = self.button.winfo_rooty() + self.button.winfo_height() + 2
        popup.geometry(f"{width}x{popup.winfo_reqheight()}+{x}+{y}")

        popup.bind("<FocusOut>", lambda _e: self._close())
        popup.bind("<Escape>", lambda _e: self._close())
        popup.focus_set()

    def _select(self, value):
        self._var.set(value)
        self._close()

    def _close(self):
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None


class TranscriberApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Transcriptor de audio")
        self.root.geometry("880x640")
        self.root.minsize(640, 440)

        self.audio_path = tk.StringVar()
        self.model_size = tk.StringVar(value="")
        self.language_label = tk.StringVar(value="Detectar automáticamente")
        self.dark_mode = tk.BooleanVar(value=False)

        self._model_cache = {}  # (size) -> WhisperModel
        self._queue = queue.Queue()
        self._worker_running = False
        self._summarizing = False
        self._cancel_event = threading.Event()
        self._transcribe_duration = 0.0

        sv_ttk.set_theme("light")
        self._build_ui()
        self._refresh_theme_colors()
        self._refresh_text_actions_state()

    def _toggle_theme(self):
        sv_ttk.set_theme("dark" if self.dark_mode.get() else "light")
        self._refresh_theme_colors()

    def _refresh_theme_colors(self):
        palette = PALETTES["dark" if self.dark_mode.get() else "light"]
        style = ttk.Style(self.root)

        # sv_ttk tematiza los widgets ttk; aquí solo ajustamos los acentos propios
        # y el área de texto (tk.Text clásico, fuera del alcance de sv_ttk).
        style.configure("Header.TLabel", foreground=palette["accent"], font=(FONT_FAMILY, 18, "bold"))
        style.configure("StatusNeutral.TLabel", foreground=palette["muted"], font=(FONT_FAMILY, 10))
        style.configure("StatusBusy.TLabel", foreground=palette["accent"], font=(FONT_FAMILY, 10, "bold"))
        style.configure("StatusSuccess.TLabel", foreground=palette["success"], font=(FONT_FAMILY, 10, "bold"))
        style.configure("StatusError.TLabel", foreground=palette["danger"], font=(FONT_FAMILY, 10, "bold"))

        self.root.configure(bg=palette["bg"])
        self.text_frame.configure(bg=palette["border"])
        self.text_area.configure(
            bg=palette["text_bg"],
            fg=palette["text_fg"],
            insertbackground=palette["text_fg"],
            selectbackground=palette["select_bg"],
            selectforeground=palette["text_fg"],
        )

    def _current_palette(self):
        return PALETTES["dark" if self.dark_mode.get() else "light"]

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=18)
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="🎙 Transcriptor de audio", style="Header.TLabel").pack(side="left")
        ttk.Checkbutton(
            header, text="Modo oscuro", style="Switch.TCheckbutton",
            variable=self.dark_mode, command=self._toggle_theme,
        ).pack(side="right", pady=4)

        form_card = ttk.Frame(container, style="Card.TFrame", padding=16)
        form_card.pack(fill="x", pady=(0, 12))

        file_frame = ttk.Frame(form_card, style="Card.TFrame")
        file_frame.pack(fill="x")
        ttk.Label(file_frame, text="Archivo:").pack(side="left")
        entry = ttk.Entry(file_frame, textvariable=self.audio_path, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=8)
        self.browse_btn = ttk.Button(file_frame, text="Examinar...", command=self._browse_file)
        self.browse_btn.pack(side="left")

        options_frame = ttk.Frame(form_card, style="Card.TFrame")
        options_frame.pack(fill="x", pady=(14, 0))

        ttk.Label(options_frame, text="Modelo:").pack(side="left")
        self.model_combo = Dropdown(
            options_frame, self.model_size, MODEL_SIZES, self._current_palette,
            placeholder="Selecciona un modelo",
        )
        self.model_combo.pack(side="left", padx=(6, 18))

        ttk.Label(options_frame, text="Idioma:").pack(side="left")
        self.lang_combo = Dropdown(
            options_frame, self.language_label, list(LANGUAGES.keys()), self._current_palette,
        )
        self.lang_combo.pack(side="left", padx=6)

        self.transcribe_btn = ttk.Button(
            options_frame, text="Transcribir", style="Accent.TButton", command=self._start_transcription
        )
        self.transcribe_btn.pack(side="right")

        self.cancel_btn = ttk.Button(
            options_frame, text="Cancelar", command=self._cancel_transcription, state="disabled"
        )
        self.cancel_btn.pack(side="right", padx=(0, 6))

        progress_card = ttk.Frame(container, style="Card.TFrame", padding=(16, 12))
        progress_card.pack(fill="x", pady=(0, 12))
        self.status_label = ttk.Label(progress_card, text="", style="StatusNeutral.TLabel")
        self.status_label.pack(side="left")
        # No se empaqueta todavía: solo se muestra mientras hay una transcripción
        # en curso (ver _set_busy), para no dejar una línea vacía en reposo.
        self.progress = ttk.Progressbar(progress_card, mode="determinate", maximum=100)

        # Esta fila se empaqueta ANTES que el área de texto (que expande) y con
        # side="bottom": así reserva su espacio primero y nunca queda aplastada
        # a 0px cuando el contenido no entra completo en la ventana.
        bottom_frame = ttk.Frame(container)
        bottom_frame.pack(side="bottom", fill="x", pady=(12, 0))
        self.save_btn = ttk.Button(bottom_frame, text="Guardar como .txt", command=self._save_txt)
        self.save_btn.pack(side="left")
        self.copy_btn = ttk.Button(bottom_frame, text="Copiar al portapapeles", command=self._copy_clipboard)
        self.copy_btn.pack(side="left", padx=6)
        self.clear_btn = ttk.Button(bottom_frame, text="Limpiar", command=self._clear_text)
        self.clear_btn.pack(side="left")
        self.summarize_btn = ttk.Button(
            bottom_frame, text="Resumir con IA", style="Accent.TButton", command=self._start_summary
        )
        self.summarize_btn.pack(side="right")
        self._text_action_buttons = (self.save_btn, self.copy_btn, self.clear_btn, self.summarize_btn)

        # Frame con 1px de "borde" (su propio color de fondo asoma alrededor del
        # texto) ya que tk.Text no tiene esquinas redondeadas ni borde temático.
        self.text_frame = tk.Frame(container, bd=0)
        self.text_frame.pack(side="top", fill="both", expand=True)
        self.text_area = scrolledtext.ScrolledText(
            self.text_frame,
            wrap="word",
            font=(FONT_FAMILY, 11),
            relief="flat",
            bd=0,
            padx=14,
            pady=12,
        )
        self.text_area.pack(fill="both", expand=True, padx=1, pady=1)

    def _browse_file(self):
        path = filedialog.askopenfilename(title="Selecciona un archivo de audio", filetypes=AUDIO_FILETYPES)
        if path:
            self.audio_path.set(path)

    def _set_busy(self, busy: bool, status: str = ""):
        self._worker_running = busy
        locked_state = "disabled" if busy else "normal"
        self.transcribe_btn.config(state=locked_state)
        self.browse_btn.config(state=locked_state)
        self.model_combo.set_enabled(not busy)
        self.lang_combo.set_enabled(not busy)
        self.cancel_btn.config(state="normal" if busy else "disabled")
        if busy:
            for btn in self._text_action_buttons:
                btn.config(state="disabled")
            self._transcribe_duration = 0.0
            self.progress.pack(side="right", fill="x", expand=True, padx=(12, 0))
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
            self.status_label.config(style="StatusBusy.TLabel")
        else:
            self._refresh_text_actions_state()
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=100)
            self.progress["value"] = 0
            self.progress.pack_forget()
            self.status_label.config(style="StatusNeutral.TLabel")
        if status:
            self.status_label.config(text=status)

    def _refresh_text_actions_state(self):
        has_text = bool(self.text_area.get("1.0", "end-1c").strip())
        state = "normal" if has_text else "disabled"
        for btn in self._text_action_buttons:
            btn.config(state=state)

    def _cancel_transcription(self):
        if not self._worker_running:
            return
        self._cancel_event.set()
        self.cancel_btn.config(state="disabled")
        self.status_label.config(text="Cancelando...", style="StatusBusy.TLabel")

    def _start_transcription(self):
        path = self.audio_path.get().strip()
        if not path:
            messagebox.showwarning("Falta archivo", "Selecciona primero un archivo de audio.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("Archivo no encontrado", f"No existe el archivo:\n{path}")
            return
        model_size = self.model_size.get()
        if not model_size:
            messagebox.showwarning("Falta modelo", "Selecciona un modelo antes de transcribir.")
            return
        if self._worker_running or self._summarizing:
            return

        self.text_area.delete("1.0", tk.END)
        self._cancel_event.clear()
        self._set_busy(True, "Cargando modelo...")

        language = LANGUAGES.get(self.language_label.get())

        thread = threading.Thread(
            target=self._transcribe_worker, args=(path, model_size, language), daemon=True
        )
        thread.start()
        self.root.after(100, self._poll_queue)

    def _start_summary(self):
        text = self.text_area.get("1.0", tk.END).strip()
        if not text:
            messagebox.showinfo("Nada que resumir", "No hay texto para resumir todavía.")
            return
        if self._worker_running or self._summarizing:
            return

        api_key = self._get_api_key()
        if not api_key:
            return

        self._summarizing = True
        self.summarize_btn.config(state="disabled")
        self.status_label.config(text="Generando resumen con IA...", style="StatusBusy.TLabel")

        thread = threading.Thread(target=self._summarize_worker, args=(text, api_key), daemon=True)
        thread.start()
        self.root.after(100, self._poll_queue)

    def _get_api_key(self):
        key = os.environ.get("ANTHROPIC_API_KEY") or _load_saved_api_key()
        if key:
            return key
        key = self._prompt_for_api_key()
        if not key:
            return None
        _save_api_key(key)
        return key

    def _prompt_for_api_key(self):
        # No existe un "iniciar sesión con Anthropic" público para apps de
        # terceros (eso es exclusivo del CLI oficial de Anthropic). Lo más
        # simple es un botón que abre directo la página para generar la key.
        palette = self._current_palette()
        result = {"key": None}

        win = tk.Toplevel(self.root)
        win.title("API key de Anthropic")
        win.configure(bg=palette["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        body = ttk.Frame(win, padding=20)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body, wraplength=360, justify="left",
            text="Para resumir con IA hace falta una API key de Anthropic.\n"
                 "Se guarda localmente en tu carpeta de usuario, no en el proyecto.",
        ).pack(anchor="w")

        ttk.Button(
            body, text="Obtener API key en console.anthropic.com",
            command=lambda: webbrowser.open(ANTHROPIC_KEYS_URL),
        ).pack(fill="x", pady=(12, 12))

        entry_var = tk.StringVar()
        entry = ttk.Entry(body, textvariable=entry_var, show="•", width=44)
        entry.pack(fill="x")
        entry.focus_set()

        btn_row = ttk.Frame(body)
        btn_row.pack(fill="x", pady=(16, 0))

        def confirm(_event=None):
            result["key"] = entry_var.get().strip() or None
            win.destroy()

        def cancel(_event=None):
            win.destroy()

        ttk.Button(btn_row, text="Cancelar", command=cancel).pack(side="left")
        ttk.Button(btn_row, text="Guardar", style="Accent.TButton", command=confirm).pack(side="right")

        entry.bind("<Return>", confirm)
        win.bind("<Escape>", cancel)
        win.protocol("WM_DELETE_WINDOW", cancel)

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{x}+{y}")

        self.root.wait_window(win)
        return result["key"]

    def _summarize_worker(self, text, api_key):
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=SUMMARY_MODEL,
                max_tokens=1024,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
            summary = "".join(block.text for block in response.content if block.type == "text")
            self._queue.put(("summary_done", summary))
        except Exception as exc:  # noqa: BLE001
            self._queue.put(("summary_error", str(exc)))

    def _show_summary_window(self, summary):
        palette = self._current_palette()
        win = tk.Toplevel(self.root)
        win.title("Resumen")
        win.geometry("560x420")
        win.configure(bg=palette["bg"])

        text_wrap = tk.Frame(win, bg=palette["border"])
        text_wrap.pack(fill="both", expand=True, padx=16, pady=16)
        text_widget = tk.Text(
            text_wrap, wrap="word", font=(FONT_FAMILY, 11),
            bg=palette["text_bg"], fg=palette["text_fg"],
            insertbackground=palette["text_fg"], relief="flat", bd=0, padx=14, pady=12,
        )
        text_widget.pack(fill="both", expand=True, padx=1, pady=1)
        text_widget.insert("1.0", summary)
        text_widget.config(state="disabled")

        btn_frame = ttk.Frame(win, padding=(16, 0, 16, 16))
        btn_frame.pack(fill="x")

        def copy_summary():
            self.root.clipboard_clear()
            self.root.clipboard_append(summary)

        ttk.Button(btn_frame, text="Copiar", command=copy_summary).pack(side="left")
        ttk.Button(btn_frame, text="Cerrar", command=win.destroy).pack(side="right")

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
            if self._cancel_event.is_set():
                self._queue.put(("cancelled",))
                return

            segments, info = model.transcribe(
                path,
                language=language,
                beam_size=5,
                vad_filter=True,
            )
            self._queue.put(("transcribe_total", info.duration or 0.0))

            prev_end = None
            sentences_in_paragraph = 0
            for segment in segments:
                if self._cancel_event.is_set():
                    self._queue.put(("cancelled",))
                    return

                text = segment.text.strip()
                if text:
                    # Un párrafo nuevo empieza si hubo una pausa notable (cambio de idea)
                    # o si ya se acumularon varias oraciones seguidas, para que el texto
                    # respire visualmente aunque el audio no tenga silencios largos.
                    pause = (segment.start - prev_end) if prev_end is not None else None
                    new_paragraph = prev_end is not None and (
                        pause > 0.8 or sentences_in_paragraph >= 3
                    )
                    if new_paragraph or prev_end is None:
                        sentences_in_paragraph = 0
                    self._queue.put(("segment_text", text, new_paragraph))
                    sentences_in_paragraph += 1
                    prev_end = segment.end
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
                    text, new_paragraph = item[1], item[2]
                    if self.text_area.index("end-1c") != "1.0":
                        self.text_area.insert(tk.END, "\n\n" if new_paragraph else "\n")
                    self.text_area.insert(tk.END, text)
                    self.text_area.see(tk.END)
                elif kind == "done":
                    detected = item[1]
                    self._set_busy(False, f"✓ Listo.{detected}")
                    self.status_label.config(style="StatusSuccess.TLabel")
                    return
                elif kind == "error":
                    self._set_busy(False, "⚠ Error.")
                    self.status_label.config(style="StatusError.TLabel")
                    messagebox.showerror("Error al transcribir", item[1])
                    return
                elif kind == "cancelled":
                    self._set_busy(False, "Cancelado.")
                    return
                elif kind == "summary_done":
                    self._summarizing = False
                    self.summarize_btn.config(state="normal")
                    self.status_label.config(text="✓ Resumen generado.", style="StatusSuccess.TLabel")
                    self._show_summary_window(item[1])
                elif kind == "summary_error":
                    self._summarizing = False
                    self.summarize_btn.config(state="normal")
                    self.status_label.config(text="⚠ Error al resumir.", style="StatusError.TLabel")
                    messagebox.showerror("Error al resumir", item[1])
        except queue.Empty:
            pass

        if self._worker_running or self._summarizing:
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
        self._refresh_text_actions_state()


def main():
    root = tk.Tk()
    app = TranscriberApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
