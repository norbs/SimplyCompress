#!/usr/bin/env python3
"""SimplyConvert GUI — single-window front-end for simplyconvert.py.

One screen: source folder, output folder, mode, bitrate, auto-volume,
dry-run — and an Apply button that streams the engine's log live into the
window. Pure tkinter (Python stdlib): no extra dependency, identical on
Linux, macOS and Windows.

Run it with `python3 simplyconvert_gui.py` (or via run_gui.sh / run_gui.bat).
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simplyconvert.py")

BG = "#0D1220"        # SimplyPlay navy
PANEL = "#151D33"
TEXT = "#E8EEFA"
DIM = "#8A97B8"
ACCENT = "#4FC3F7"


class ApplyLog:
    """Thread-safe pipe between the engine subprocess and the Tk text widget."""

    def __init__(self, widget: tk.Text, q: queue.Queue):
        self.widget = widget
        self.q = q
        self.widget.tag_configure("dim", foreground=DIM)

    def pump(self):
        try:
            while True:
                line = self.q.get_nowait()
                tag = "dim" if line.startswith("  ") else None
                self.widget.insert(tk.END, line + "\n", tag)
                self.widget.see(tk.END)
        except queue.Empty:
            pass
        self.widget.after(120, self.pump)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SimplyConvert")
        self.configure(bg=BG)
        self.minsize(680, 560)
        self.proc = None

        pad = {"padx": 12, "pady": 6}
        frm = tk.Frame(self, bg=BG)
        frm.pack(fill="both", expand=True)

        # ------------------------------------------------------------------
        # Options panel
        # ------------------------------------------------------------------
        opts = tk.LabelFrame(frm, text=" Options ", bg=PANEL, fg=TEXT,
                             padx=10, pady=8)
        opts.pack(fill="x", **pad)

        def row(parent, r, label):
            tk.Label(parent, text=label, bg=PANEL, fg=DIM, anchor="w") \
                .grid(row=r, column=0, sticky="w", pady=3)
            return r

        row(opts, 0, "Dossier source")
        self.src_var = tk.StringVar()
        tk.Entry(opts, textvariable=self.src_var, bg=BG, fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=DIM) \
            .grid(row=0, column=1, sticky="ew", padx=6)
        tk.Button(opts, text="Parcourir…", command=self.pick_src,
                  bg=PANEL, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                  relief="flat") \
            .grid(row=0, column=2)

        row(opts, 1, "Dossier de sortie")
        self.out_var = tk.StringVar()
        tk.Entry(opts, textvariable=self.out_var, bg=BG, fg=TEXT,
                 insertbackground=TEXT, relief="flat",
                 highlightthickness=1, highlightbackground=DIM) \
            .grid(row=1, column=1, sticky="ew", padx=6)
        tk.Button(opts, text="Parcourir…", command=self.pick_out,
                  bg=PANEL, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                  relief="flat") \
            .grid(row=1, column=2)

        row(opts, 2, "Mode")
        self.mode_var = tk.StringVar(value="compressidentify")
        mode_box = ttk.Combobox(opts, textvariable=self.mode_var, state="readonly",
                                values=["compressidentify", "compress", "identify"],
                                width=22)
        mode_box.grid(row=2, column=1, sticky="w", padx=6)

        row(opts, 3, "Débit Ogg/Opus")
        self.br_var = tk.IntVar(value=160)
        br_box = ttk.Combobox(opts, textvariable=self.br_var, state="readonly",
                              values=[160, 180, 320], width=22)
        br_box.grid(row=3, column=1, sticky="w", padx=6)

        self.auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text="Volume automatique (ReplayGain, égalise la "
                       "loudness ; les originaux ne sont jamais modifiés)",
                       variable=self.auto_var, bg=PANEL, fg=TEXT,
                       activebackground=PANEL, activeforeground=TEXT,
                       selectcolor=BG, relief="flat", highlightthickness=0) \
            .grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.dry_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opts, text="Simulation (--dry-run : rien n'est écrit)",
                       variable=self.dry_var, bg=PANEL, fg=TEXT,
                       activebackground=PANEL, activeforeground=TEXT,
                       selectcolor=BG, relief="flat", highlightthickness=0) \
            .grid(row=5, column=0, columnspan=3, sticky="w")

        opts.columnconfigure(1, weight=1)

        # ------------------------------------------------------------------
        # Apply button
        # ------------------------------------------------------------------
        self.apply_btn = tk.Button(frm, text="APPLIQUER", command=self.apply,
                                   bg=ACCENT, fg="#062A3A",
                                   activebackground="#7AD4F9",
                                   activeforeground="#062A3A",
                                   relief="flat", font=("TkDefaultFont", 12, "bold"),
                                   padx=24, pady=8, cursor="hand2")
        self.apply_btn.pack(pady=(2, 6))

        # ------------------------------------------------------------------
        # Log area
        # ------------------------------------------------------------------
        log_frame = tk.LabelFrame(frm, text=" Journal ", bg=PANEL, fg=TEXT,
                                  padx=6, pady=6)
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_widget = tk.Text(log_frame, bg=BG, fg=TEXT, relief="flat",
                                  insertbackground=TEXT, wrap="word",
                                  height=12, state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        self.log_widget.configure(yscrollcommand=scroll.set)
        self.log_widget.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.q = queue.Queue()
        self.pumper = ApplyLog(self.log_widget, self.q)
        self.log_widget.after(120, self.pumper.pump)

        # Keep the engine's dependency check where the user can see it.
        if not self.check_engine():
            self.apply_btn.configure(state="disabled")

    # ----------------------------------------------------------------------

    def check_engine(self) -> bool:
        if not os.path.isfile(SCRIPT):
            messagebox.showerror(
                "SimplyConvert", f"Moteur introuvable :\n{SCRIPT}")
            return False
        r = subprocess.run([sys.executable, SCRIPT, "--help"],
                           capture_output=True)
        if r.returncode != 0:
            messagebox.showerror(
                "SimplyConvert",
                "Le moteur n'a pas démarré (mutagen/numpy manquants ?)\n"
                + r.stderr.decode(errors="replace")[:400])
            return False
        return True

    def pick_src(self):
        d = filedialog.askdirectory(title="Dossier source (musique)")
        if d:
            self.src_var.set(d)
            if not self.out_var.get():
                self.out_var.set(os.path.join(d + "_ogg"))

    def pick_out(self):
        d = filedialog.askdirectory(title="Dossier de sortie (copies Ogg)")
        if d:
            self.out_var.set(d)

    def apply(self):
        src = self.src_var.get().strip()
        out = self.out_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showwarning("SimplyConvert", "Choisissez un dossier source valide.")
            return
        if not out:
            messagebox.showwarning("SimplyConvert", "Choisissez un dossier de sortie.")
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("SimplyConvert", "Un traitement est déjà en cours.")
            return

        cmd = [sys.executable, "-u", SCRIPT,
               self.mode_var.get(), src, "-o", out,
               "--bitrate", str(self.br_var.get())]
        if not self.auto_var.get():
            cmd.append("--no-auto-volume")
        if self.dry_var.get():
            cmd.append("--dry-run")

        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", tk.END)
        self.log_widget.configure(state="disabled")
        self.apply_btn.configure(state="disabled", text="EN COURS…")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self.drain, daemon=True).start()

    def drain(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.q.put(line.rstrip("\n"))
        rc = self.proc.wait()
        self.q.put("")
        self.q.put(f"— Terminé (code {rc}) —")
        self.after(0, lambda: self.apply_btn.configure(
            state="normal", text="APPLIQUER"))


if __name__ == "__main__":
    App().mainloop()
