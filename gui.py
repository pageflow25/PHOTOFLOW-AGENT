"""
gui.py — Interface gráfica de monitoramento do PhotoFlow Print Agent.

Executa o loop do agente em background thread e exibe:
  - Status do agente e da impressora em tempo real
  - Log em tempo real com cores por nível
  - Editor das configurações (.env) com listagem de impressoras

Entrada: python gui.py  |  PhotoFlow-Agent.exe
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────── #
# Ponte de logging: redireciona mensagens do agente para a fila da GUI
# ─────────────────────────────────────────────────────────────────────────── #

class _GuiLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[logging.LogRecord]") -> None:
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait(record)
        except queue.Full:
            pass


# ─────────────────────────────────────────────────────────────────────────── #
# Paleta e constantes visuais
# ─────────────────────────────────────────────────────────────────────────── #

BG        = "#f1f5f9"
PANEL_BG  = "#ffffff"
HEADER_BG = "#1e40af"
ACCENT    = "#2563eb"
SUCCESS   = "#16a34a"
ERROR     = "#dc2626"
WARNING   = "#d97706"
TEXT      = "#0f172a"
SUBTEXT   = "#64748b"
BORDER    = "#e2e8f0"
LOG_BG    = "#0f172a"

FONT      = "Segoe UI"
MONO      = "Consolas"

# Campos do .env que o editor deve exibir (chave, rótulo, hint, is_secret)
ENV_FIELDS: List[Tuple[str, str, str, bool]] = [
    ("APP_URL",                   "URL do servidor",               "https://seu-app.vercel.app",   False),
    ("API_KEY",                   "Chave da API",                  "Mesma chave do servidor",       True ),
    ("AGENT_ID",                  "ID do agente",                  "stand-1",                       False),
    ("PRINTER_NAME",              "Impressora",                    "Vazio = autodetect Citizen",    False),
    ("POLL_INTERVAL_SECONDS",     "Intervalo de polling (s)",      "5",                             False),
    ("BATCH_SIZE",                "Fotos por ciclo",               "3",                             False),
    ("CLAIM_TIMEOUT_MINUTES",     "Timeout do claim (min)",        "5",                             False),
    ("HEARTBEAT_INTERVAL_SECONDS","Heartbeat (s)",                 "30",                            False),
    ("HTTP_TIMEOUT_SECONDS",      "Timeout HTTP (s)",              "30",                            False),
    ("LOG_LEVEL",                 "Nível de log",                  "INFO | DEBUG | WARNING",        False),
    ("LOG_DIR",                   "Pasta de logs",                 "./logs",                        False),
]


# ─────────────────────────────────────────────────────────────────────────── #
# Aplicação principal
# ─────────────────────────────────────────────────────────────────────────── #

class PhotoFlowApp(tk.Tk):

    MAX_LOG_TEXT = 240
    MAX_EXTRA_TEXT = 140

    def __init__(self) -> None:
        super().__init__()

        self.title("PhotoFlow Print Agent")
        self.geometry("900x720")
        self.minsize(760, 560)
        self.configure(bg=BG)

        # Comunicação thread → GUI
        self._log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=5000)

        # Estado do agente
        self._agent_thread: Optional[threading.Thread] = None
        self._running = False
        self._start_time: Optional[float] = None

        self._build_ui()
        self._install_log_bridge()
        self._poll_log_queue()
        self._update_uptime()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Construção da UI ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_header()
        self._build_controls()
        self._build_notebook()

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=HEADER_BG, padx=20, pady=12)
        hdr.pack(fill="x")

        tk.Label(
            hdr, text="PhotoFlow Print Agent",
            font=(FONT, 15, "bold"), fg="white", bg=HEADER_BG,
        ).pack(side="left")

        # Indicador de status (direita)
        sf = tk.Frame(hdr, bg=HEADER_BG)
        sf.pack(side="right", padx=(0, 4))

        self._dot_canvas = tk.Canvas(sf, width=14, height=14, bg=HEADER_BG,
                                     highlightthickness=0)
        self._dot_canvas.create_oval(1, 1, 13, 13, fill="#475569",
                                     outline="", tags="dot")
        self._dot_canvas.pack(side="left", padx=(0, 6))

        self._status_lbl = tk.Label(sf, text="Parado", font=(FONT, 10, "bold"),
                                    fg="white", bg=HEADER_BG)
        self._status_lbl.pack(side="left")

    def _build_controls(self) -> None:
        bar = tk.Frame(self, bg=BG, padx=20, pady=10)
        bar.pack(fill="x")

        self._btn_toggle = tk.Button(
            bar, text="▶  Iniciar Agente",
            font=(FONT, 10, "bold"), fg="white", bg=SUCCESS,
            activebackground="#15803d", relief="flat",
            padx=16, pady=7, cursor="hand2",
            command=self._toggle_agent,
        )
        self._btn_toggle.pack(side="left")

        sep = tk.Frame(bar, bg=BORDER, width=1, height=24)
        sep.pack(side="left", padx=16, pady=4)

        self._printer_lbl = tk.Label(bar, text="Impressora: —",
                                     font=(FONT, 10), fg=SUBTEXT, bg=BG)
        self._printer_lbl.pack(side="left")

        self._uptime_lbl = tk.Label(bar, text="", font=(FONT, 9),
                                    fg=SUBTEXT, bg=BG)
        self._uptime_lbl.pack(side="right")

    def _build_notebook(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=(FONT, 10), padding=[14, 7],
                        background=BG, foreground=SUBTEXT)
        style.map("TNotebook.Tab",
                  background=[("selected", PANEL_BG)],
                  foreground=[("selected", TEXT)])

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        tab_dash = tk.Frame(nb, bg=BG)
        tab_cfg  = tk.Frame(nb, bg=BG)
        nb.add(tab_dash, text="  Dashboard  ")
        nb.add(tab_cfg,  text="  Configurações  ")

        self._build_dashboard(tab_dash)
        self._build_settings(tab_cfg)

    # ── Dashboard ─────────────────────────────────────────────────────────

    def _build_dashboard(self, parent: tk.Frame) -> None:
        # Log em tempo real
        log_outer = tk.Frame(parent, bg=BG, padx=6, pady=6)
        log_outer.pack(fill="both", expand=True)

        tk.Label(log_outer, text="Log em tempo real",
                 font=(FONT, 10, "bold"), fg=TEXT, bg=BG).pack(anchor="w", pady=(0, 4))

        self._log_text = scrolledtext.ScrolledText(
            log_outer, font=(MONO, 9), bg=LOG_BG, fg="#94a3b8",
            insertbackground="white", relief="flat", height=9,
            wrap="word",
            state="disabled",
        )
        self._log_text.pack(fill="both", expand=True)
        self._log_text.tag_config("DEBUG",    foreground="#475569")
        self._log_text.tag_config("INFO",     foreground="#94a3b8")
        self._log_text.tag_config("WARNING",  foreground="#fbbf24")
        self._log_text.tag_config("ERROR",    foreground="#f87171")
        self._log_text.tag_config("CRITICAL", foreground="#ef4444")

    # ── Configurações ─────────────────────────────────────────────────────

    def _build_settings(self, parent: tk.Frame) -> None:
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=PANEL_BG,
                         highlightbackground=BORDER, highlightthickness=1)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize(e: tk.Event) -> None:
            canvas.itemconfig(win_id, width=e.width)
        canvas.bind("<Configure>", _resize)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        wrapper = tk.Frame(inner, bg=PANEL_BG, padx=28, pady=20)
        wrapper.pack(fill="both", expand=True)

        tk.Label(wrapper, text="Configurações (.env)",
                 font=(FONT, 13, "bold"), fg=TEXT, bg=PANEL_BG).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))

        env_vals = self._read_env(self._env_path())
        self._env_vars: Dict[str, tk.StringVar] = {}

        for i, (key, label, hint, secret) in enumerate(ENV_FIELDS, start=1):
            tk.Label(wrapper, text=label, font=(FONT, 9, "bold"),
                     fg=TEXT, bg=PANEL_BG, anchor="w").grid(
                row=i, column=0, sticky="w", pady=5, padx=(0, 12))

            var = tk.StringVar(value=env_vals.get(key, ""))
            self._env_vars[key] = var

            entry = tk.Entry(
                wrapper, textvariable=var,
                font=(FONT, 10), fg=TEXT, bg="#f8fafc",
                relief="solid", bd=1, width=44,
                show="●" if secret else "",
            )
            entry.grid(row=i, column=1, sticky="ew", pady=5)

            tk.Label(wrapper, text=hint, font=(FONT, 8),
                     fg=SUBTEXT, bg=PANEL_BG).grid(
                row=i, column=2, sticky="w", padx=(10, 0))

        wrapper.columnconfigure(1, weight=1)

        # Botões
        btn_row = tk.Frame(wrapper, bg=PANEL_BG)
        btn_row.grid(row=len(ENV_FIELDS) + 1, column=0,
                     columnspan=3, sticky="w", pady=(20, 0))

        tk.Button(
            btn_row, text="💾  Salvar",
            font=(FONT, 10, "bold"), fg="white", bg=ACCENT,
            activebackground="#1d4ed8", relief="flat",
            padx=16, pady=7, cursor="hand2",
            command=self._save_env,
        ).pack(side="left", padx=(0, 10))

        tk.Button(
            btn_row, text="🖨️  Listar Impressoras",
            font=(FONT, 10), fg=ACCENT, bg="#eff6ff",
            activebackground="#dbeafe", relief="flat",
            padx=14, pady=7, cursor="hand2",
            command=self._open_printer_dialog,
        ).pack(side="left")

        self._env_path_lbl = tk.Label(
            wrapper, text=f"📄  {self._env_path()}",
            font=(FONT, 8), fg=SUBTEXT, bg=PANEL_BG,
        )
        self._env_path_lbl.grid(row=len(ENV_FIELDS) + 2, column=0,
                                 columnspan=3, sticky="w", pady=(10, 0))

    # ── Log bridge ────────────────────────────────────────────────────────

    def _install_log_bridge(self) -> None:
        handler = _GuiLogHandler(self._log_queue)
        handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                record = self._log_queue.get_nowait()
                self._dispatch_record(record)
        except queue.Empty:
            pass
        finally:
            self.after(80, self._poll_log_queue)

    def _dispatch_record(self, record: logging.LogRecord) -> None:
        self._write_log_line(record)

        event = getattr(record, "event", None)
        if not event:
            return

        if event == "agent_start":
            printer = getattr(record, "printer", "—")
            self._printer_lbl.config(text=f"Impressora: {printer}")

        elif event == "printer_status_change":
            new = getattr(record, "new", "")
            self._printer_lbl.config(
                text=f"Impressora: {new or '—'}")

        elif event == "agent_stop":
            self._set_status(running=False)

    @staticmethod
    def _clip_text(value: object, max_len: int) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def _write_log_line(self, record: logging.LogRecord) -> None:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        # Campos extras relevantes
        skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName", "asctime",
        }
        extras = {k: v for k, v in record.__dict__.items()
                  if k not in skip and v is not None}

        msg = self._clip_text(record.getMessage(), self.MAX_LOG_TEXT)
        compact_extras = "  ".join(
            f"{k}={self._clip_text(v, self.MAX_EXTRA_TEXT)}"
            for k, v in extras.items()
        )
        extra_str = f"  {compact_extras}" if compact_extras else ""
        line = f"[{ts}] {record.levelname:<8} {msg}{extra_str}\n"

        self._log_text.config(state="normal")
        self._log_text.insert("end", line, record.levelname)
        self._log_text.see("end")
        # Mantém no máximo 3000 linhas
        total = int(self._log_text.index("end-1c").split(".")[0])
        if total > 3000:
            self._log_text.delete("1.0", f"{total - 3000}.0")
        self._log_text.config(state="disabled")

    # ── Controles do agente ───────────────────────────────────────────────

    def _toggle_agent(self) -> None:
        if self._running:
            self._stop_agent()
        else:
            self._start_agent()

    def _start_agent(self) -> None:
        try:
            import agent as _agent
            from config import Config
            _agent._stopping.clear()
            cfg = Config.load()
        except Exception as exc:
            messagebox.showerror("Erro de configuração", str(exc))
            return

        self._start_time = time.monotonic()
        self._set_status(running=True)

        def _run() -> None:
            try:
                import agent as _agent
                _agent._run_loop(cfg, dry_run=False)
            except Exception as exc:
                logging.getLogger("agent.gui").error(
                    "agent_crash",
                    extra={"event": "agent_crash", "error": str(exc)},
                )
            finally:
                self.after(0, lambda: self._set_status(running=False))

        self._agent_thread = threading.Thread(
            target=_run, name="agent-loop", daemon=True)
        self._agent_thread.start()

    def _stop_agent(self) -> None:
        try:
            import agent as _agent
            _agent._stopping.set()
        except Exception:
            pass
        self._btn_toggle.config(state="disabled")
        self.after(4000, lambda: (
            self._btn_toggle.config(state="normal"),
            self._set_status(running=False),
        ))

    def _set_status(self, *, running: bool) -> None:
        self._running = running
        if running:
            self._dot_canvas.itemconfig("dot", fill="#22c55e")
            self._status_lbl.config(text="Rodando")
            self._btn_toggle.config(
                text="■  Parar Agente", bg=ERROR, activebackground="#b91c1c",
                state="normal",
            )
        else:
            self._dot_canvas.itemconfig("dot", fill="#475569")
            self._status_lbl.config(text="Parado")
            self._btn_toggle.config(
                text="▶  Iniciar Agente", bg=SUCCESS,
                activebackground="#15803d", state="normal",
            )
            self._start_time = None
            self._uptime_lbl.config(text="")

    # ── Uptime ────────────────────────────────────────────────────────────

    def _update_uptime(self) -> None:
        if self._running and self._start_time is not None:
            secs = int(time.monotonic() - self._start_time)
            h, rem = divmod(secs, 3600)
            m, s   = divmod(rem, 60)
            self._uptime_lbl.config(text=f"Uptime: {h:02d}:{m:02d}:{s:02d}")
        self.after(1000, self._update_uptime)

    # ── .env helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _env_path() -> Path:
        # Quando empacotado pelo PyInstaller, .env fica ao lado do .exe
        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent / ".env"
        return Path(__file__).parent / ".env"

    @staticmethod
    def _read_env(path: Path) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if not path.exists():
            return result
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
        return result

    def _save_env(self) -> None:
        path = self._env_path()
        lines = [f"{key}={var.get().strip()}\n"
                 for key, var in self._env_vars.items()]
        try:
            path.write_text("".join(lines), encoding="utf-8")
            messagebox.showinfo(
                "Configurações salvas",
                f"Arquivo salvo em:\n{path}\n\n"
                "Reinicie o agente para aplicar as alterações.",
            )
        except OSError as exc:
            messagebox.showerror("Erro ao salvar", str(exc))

    # ── Diálogo de impressoras ─────────────────────────────────────────────

    def _open_printer_dialog(self) -> None:
        try:
            from printer import autodetect_citizen, list_installed_printers
            printers = list_installed_printers()
            citizens = set(autodetect_citizen(printers))
        except Exception as exc:
            messagebox.showerror("Erro", str(exc))
            return

        if not printers:
            messagebox.showinfo("Impressoras", "Nenhuma impressora instalada.")
            return

        win = tk.Toplevel(self)
        win.title("Impressoras instaladas")
        win.geometry("500x340")
        win.configure(bg=PANEL_BG)
        win.resizable(False, False)
        win.grab_set()
        win.transient(self)

        tk.Label(win, text="Selecione a impressora",
                 font=(FONT, 12, "bold"), fg=TEXT, bg=PANEL_BG,
                 pady=14).pack()

        lst = tk.Listbox(win, font=(FONT, 10), fg=TEXT, bg="#f8fafc",
                         selectbackground=ACCENT, selectforeground="white",
                         relief="flat", bd=0, activestyle="none")
        lst.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        for p in printers:
            suffix = "  ← Citizen (autodetect)" if p in citizens else ""
            lst.insert("end", p + suffix)

        def _use() -> None:
            sel = lst.curselection()
            if not sel:
                return
            name = printers[sel[0]]
            if "PRINTER_NAME" in self._env_vars:
                self._env_vars["PRINTER_NAME"].set(name)
            win.destroy()

        tk.Button(
            win, text="Usar selecionada",
            font=(FONT, 10, "bold"), fg="white", bg=ACCENT,
            activebackground="#1d4ed8", relief="flat",
            padx=14, pady=7, cursor="hand2",
            command=_use,
        ).pack(pady=(0, 18))

    # ── Fechar ────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                "Sair",
                "O agente está rodando.\nDeseja parar e fechar a janela?",
            ):
                return
            try:
                import agent as _agent
                _agent._stopping.set()
            except Exception:
                pass
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────── #

def main() -> None:
    app = PhotoFlowApp()
    app.mainloop()


if __name__ == "__main__":
    main()
