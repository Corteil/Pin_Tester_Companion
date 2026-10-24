"""Graphical companion for the Pin Tester badge app (tkinter, no extras).

One row per pin with its live level, a mode selector and the controls for
that mode (brightness / breathe time / PWM frequency and duty). Everything
the GUI does is also available from the command line, and the console at the
bottom accepts raw protocol lines.

All serial traffic happens on a worker thread; the UI thread only draws.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Optional

from . import __version__
from .link import (ALL_PINS, ANALOG_FULL_MV, BREATHE_MAX_S, BREATHE_MIN_S, HS_PINS, LS_PINS, MODES, PWM_FREQS,
                   Link, LinkError, Pin, Port, PortInfo, SerialLink, find_devices, modes_for, pin_kind)

SIMULATOR = "Simulator (demo)"

C = dict(
    bg="#12151a", panel="#1a1f27", row="#202733", row_alt="#1c222d", line="#2c3442",
    text="#e6edf3", muted="#8b949e", accent="#4cc2ff", green="#3ddc84", red="#ff5f56",
    amber="#ffb454", off="#3a424f", inspect="#7aa2c8",
)
FONT = ("DejaVu Sans", 10)
FONT_SMALL = ("DejaVu Sans", 9)
FONT_BOLD = ("DejaVu Sans", 10, "bold")
FONT_TITLE = ("DejaVu Sans", 17, "bold")
FONT_MONO = ("DejaVu Sans Mono", 9)

LINK_STATES = {
    "disconnected": (C["muted"], "Disconnected"),
    "connecting": (C["amber"], "Connecting..."),
    "remote-off": (C["amber"], "Badge not answering -- turn Remote ON (STATUS tile, CONFIRM)"),
    "ok": (C["green"], "Connected"),
    "error": (C["red"], "Error"),
}


def freq_label(hz: int) -> str:
    return "{} kHz".format(hz // 1000) if hz >= 1000 else "{} Hz".format(hz)


def mix(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colours (t=0 -> c1, t=1 -> c2)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join("{:02x}".format(round(x + (y - x) * t)) for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# worker thread: owns the link

class LoggingLink(Link):
    """Wraps a link and reports each exchange to the console."""

    def __init__(self, inner: Link, events: "queue.Queue"):
        self.inner = inner
        self.events = events
        self.name = inner.name
        self.quiet = False

    def request(self, cmd: str, timeout: float = 1.5) -> List[str]:
        try:
            lines = self.inner.request(cmd, timeout)
        except LinkError as exc:
            if not self.quiet:
                self.events.put(("log", cmd, [], str(exc)))
            raise
        if not self.quiet:
            self.events.put(("log", cmd, lines, None))
        return lines

    def close(self) -> None:
        self.inner.close()


class Worker(threading.Thread):
    def __init__(self, device: Optional[str], sim: bool, events: "queue.Queue", poll: float = 0.4):
        super().__init__(daemon=True)
        self.device, self.sim, self.events = device, sim, events
        self.poll = poll
        self.live = True
        self.jobs: "queue.Queue[Callable[[Link], None]]" = queue.Queue()
        self._halt = threading.Event()
        self.link: Optional[LoggingLink] = None
        self._cycle = 0
        self._last_key = None

    def stop(self) -> None:
        self._halt.set()

    def submit(self, job: Callable[[Link], None]) -> None:
        self.jobs.put(job)

    def _emit_link(self, state: str, msg: str = "") -> None:
        self.events.put(("link", state, msg))

    def _open(self) -> bool:
        self._emit_link("connecting")
        try:
            if self.sim:
                from .sim import SimLink
                inner = SimLink()
            else:
                inner = SerialLink(self.device)
        except LinkError as exc:
            self._emit_link("error", str(exc))
            return False
        self.link = LoggingLink(inner, self.events)
        return True

    def run(self) -> None:
        state = None

        def set_state(new, msg=""):
            nonlocal state
            if new != state:
                state = new
                self._emit_link(new, msg)

        while not self._halt.is_set() and not self._open():
            self._halt.wait(2.0)
        if self._halt.is_set():
            return
        link = self.link
        while not self._halt.is_set():
            try:
                job = self.jobs.get(timeout=self.poll)
            except queue.Empty:
                job = None
            try:
                if job is not None:
                    link.quiet = False
                    job(link)
                    set_state("ok")
                elif self.live or state != "ok":
                    link.quiet = True
                    if state != "ok":
                        link.ping(timeout=0.6)
                        set_state("ok")
                    port = link.dump()
                    self.events.put(("dump", port))
                    # EEPROM / I2C card: a fresh bus scan when the port or
                    # its presence changes (and on first connect), otherwise
                    # just the cheap cached view every couple of seconds.
                    key = (port.port, port.present)
                    if key != self._last_key:
                        self._last_key = key
                        self.events.put(("info", link.portinfo(scan=True)))
                    elif self._cycle % 5 == 0:
                        self.events.put(("info", link.portinfo(scan=False)))
                    self._cycle += 1
                    link.quiet = False
            except LinkError as exc:
                link.quiet = False
                if job is not None:
                    self.events.put(("error", str(exc)))
                set_state("remote-off", str(exc))
                self._halt.wait(0.8)
            except Exception as exc:  # keep the thread alive on parse errors etc.
                link.quiet = False
                self.events.put(("error", "{}: {}".format(type(exc).__name__, exc)))
        link.close()


# ---------------------------------------------------------------------------
# one pin = one row

class PinRow(ttk.Frame):
    HOLD_S = 1.2     # ignore polled values for this long after you touched a control

    def __init__(self, parent, name: str, shade: int, send_mode, send_values):
        super().__init__(parent, style="Row{}.TFrame".format(shade))
        self.name, self.kind = name, pin_kind(name)
        self.send_mode, self.send_values = send_mode, send_values
        self.pin: Optional[Pin] = None
        self._hold_until = 0.0
        self._after_id = None
        self._family = None
        self._scales = []
        self.editable = False
        bg = C["row"] if shade == 0 else C["row_alt"]
        self.bg = bg

        self.columnconfigure(4, weight=1)
        self.dot = tk.Canvas(self, width=22, height=22, bg=bg, highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(10, 6), pady=7)
        self.dot_id = self.dot.create_oval(3, 3, 19, 19, fill=C["off"], outline=C["line"])
        namebox = ttk.Frame(self, style="Row{}.TFrame".format(shade))
        namebox.grid(row=0, column=1, padx=(0, 8))
        ttk.Label(namebox, text=name, style="Name{}.TLabel".format(shade), width=6).pack(side="left")
        self.adc_chip = tk.Label(namebox, text="ADC", bg=mix(bg, C["accent"], 0.22), fg=C["accent"],
                                 font=("DejaVu Sans", 8, "bold"), padx=5, pady=1)

        self.mode_var = tk.StringVar()
        self.analog_ok = False            # offered only for pins that have an ADC channel
        self.words = {}
        self.mode_box = ttk.Combobox(self, textvariable=self.mode_var, state="readonly", width=17)
        self._set_mode_list(False)
        self.mode_box.grid(row=0, column=2, padx=(0, 10))
        self.mode_box.bind("<<ComboboxSelected>>", self._mode_chosen)

        self.info = ttk.Label(self, text="", style="Info{}.TLabel".format(shade), width=34, anchor="w")
        self.info.grid(row=0, column=3, padx=(0, 10), sticky="w")

        self.ctl = ttk.Frame(self, style="Row{}.TFrame".format(shade))
        self.ctl.grid(row=0, column=4, sticky="e", padx=(0, 12))
        self._build_controls(shade)
        self._build_analog(shade)
        self.set_editable(False)   # starts as inspect only

    def _set_mode_list(self, analog_ok: bool) -> None:
        words = [w for w in modes_for(self.kind) if w != "ANALOG" or analog_ok]
        self.analog_ok = analog_ok
        self.words = {MODES[w][0]: w for w in words}
        self.mode_box.configure(values=[MODES[w][0] for w in words])

    # -- controls ------------------------------------------------------
    def _scale(self, parent, lo, hi, var, command, width=150):
        s = ttk.Scale(parent, from_=lo, to=hi, variable=var, command=command, length=width)
        s.bind("<ButtonPress-1>", lambda e: self._touch())
        self._scales.append(s)
        return s

    def set_editable(self, editable: bool) -> None:
        """Lock or unlock this row's controls. Purely local: nothing is sent
        to the badge, so the pin itself is never touched by the switch."""
        self.editable = editable
        state = "readonly" if editable else "disabled"
        self.mode_box.configure(state=state)
        self.freq_box.configure(state=state)
        for scale in self._scales:
            scale.state(["!disabled"] if editable else ["disabled"])

    def _build_controls(self, shade):
        st = "Row{}.TFrame".format(shade)
        lbl = "Info{}.TLabel".format(shade)
        # LED family (LS pins)
        self.f_led = ttk.Frame(self.ctl, style=st)
        self.bright_var = tk.DoubleVar(value=50)
        self.period_var = tk.DoubleVar(value=2.0)
        ttk.Label(self.f_led, text="brightness", style=lbl).grid(row=0, column=0, padx=(0, 6))
        self._scale(self.f_led, 0, 100, self.bright_var, self._values_changed).grid(row=0, column=1)
        self.bright_txt = ttk.Label(self.f_led, text="50%", style=lbl, width=5)
        self.bright_txt.grid(row=0, column=2, padx=(4, 12))
        self.period_lbl = ttk.Label(self.f_led, text="cycle", style=lbl)
        self.period_lbl.grid(row=0, column=3, padx=(0, 6))
        self.period_scale = self._scale(self.f_led, BREATHE_MIN_S, BREATHE_MAX_S, self.period_var,
                                        self._values_changed, 130)
        self.period_scale.grid(row=0, column=4)
        self.period_txt = ttk.Label(self.f_led, text="2.0 s", style=lbl, width=6)
        self.period_txt.grid(row=0, column=5, padx=(4, 0))
        # PWM (HS pins)
        self.f_pwm = ttk.Frame(self.ctl, style=st)
        self.freq_var = tk.StringVar(value=freq_label(1000))
        self.duty_var = tk.DoubleVar(value=50)
        ttk.Label(self.f_pwm, text="freq", style=lbl).grid(row=0, column=0, padx=(0, 6))
        self.freq_box = ttk.Combobox(self.f_pwm, textvariable=self.freq_var, state="readonly", width=8,
                                     values=[freq_label(f) for f in PWM_FREQS])
        self.freq_box.grid(row=0, column=1)
        self.freq_box.bind("<<ComboboxSelected>>", lambda e: self._values_changed())
        ttk.Label(self.f_pwm, text="duty", style=lbl).grid(row=0, column=2, padx=(14, 6))
        self._scale(self.f_pwm, 0, 100, self.duty_var, self._values_changed).grid(row=0, column=3)
        self.duty_txt = ttk.Label(self.f_pwm, text="50%", style=lbl, width=5)
        self.duty_txt.grid(row=0, column=4, padx=(4, 0))

    def _build_analog(self, shade):
        bg = C["row"] if shade == 0 else C["row_alt"]
        self.f_analog = ttk.Frame(self.ctl, style="Row{}.TFrame".format(shade))
        self.volt_txt = ttk.Label(self.f_analog, text="-.--- V", style="Name{}.TLabel".format(shade), width=8)
        self.volt_txt.grid(row=0, column=0, padx=(0, 10))
        self.volt_bar = tk.Canvas(self.f_analog, width=230, height=14, bg=bg, highlightthickness=0)
        self.volt_bar.grid(row=0, column=1)
        self.volt_bar.create_rectangle(0, 3, 230, 11, fill=C["line"], outline="")
        self.volt_fill = self.volt_bar.create_rectangle(0, 3, 0, 11, fill=C["accent"], outline="")
        ttk.Label(self.f_analog, text="0 \u2013 3.1 V", style="Info{}.TLabel".format(shade)).grid(row=0, column=2, padx=(8, 0))

    def _show_family(self, family: Optional[str]) -> None:
        if family == self._family:
            return
        self._family = family
        for frame in (self.f_led, self.f_pwm, self.f_analog):
            frame.grid_remove()
        if family == "led":
            self.f_led.grid(row=0, column=0, sticky="w")
        elif family == "pwm":
            self.f_pwm.grid(row=0, column=0, sticky="w")
        elif family == "analog":
            self.f_analog.grid(row=0, column=0, sticky="w")

    # -- user input ----------------------------------------------------
    def _touch(self) -> None:
        self._hold_until = time.monotonic() + self.HOLD_S

    def _mode_chosen(self, _event=None) -> None:
        self._touch()
        word = self.words[self.mode_var.get()]
        self.mode_box.selection_clear()
        self.send_mode(self.name, word, self._current_args(word))

    def _current_args(self, word: str) -> tuple:
        if word in ("LEDON", "LEDOFF"):
            return (round(self.bright_var.get()),)
        if word == "BREATHE":
            return (round(self.period_var.get() * 10), round(self.bright_var.get()))
        if word == "PWM":
            hz = PWM_FREQS[[freq_label(f) for f in PWM_FREQS].index(self.freq_var.get())]
            return (hz, round(self.duty_var.get()))
        return ()

    def _values_changed(self, *_):
        self._touch()
        self.bright_txt.config(text="{}%".format(round(self.bright_var.get())))
        self.period_txt.config(text="{:.1f} s".format(round(self.period_var.get() * 10) / 10))
        self.duty_txt.config(text="{}%".format(round(self.duty_var.get())))
        if self._after_id is not None:
            self.after_cancel(self._after_id)
        self._after_id = self.after(70, self._flush_values)

    def _flush_values(self) -> None:
        self._after_id = None
        word = self.words.get(self.mode_var.get())
        if word in ("LEDON", "LEDOFF", "BREATHE", "PWM"):
            self.send_values(self.name, word, self._current_args(word))

    # -- state from the badge ------------------------------------------
    @staticmethod
    def _mode_text(pin: Pin) -> str:
        """What the dropdown shows. For an inspect-only pin that's what the
        pin turned out to be (output, input, PWM, peripheral...), so the box
        reflects the pin's real type; otherwise the mode you set."""
        if pin.mode != "INSPECT":
            return MODES.get(pin.mode, (pin.mode,))[0]
        func, direction = pin.fields.get("func"), pin.fields.get("dir")
        if func == "led":
            return "Inspect: LED mode"
        if func == "pwm":
            return "Inspect: PWM output"
        if func == "periph":
            return "Inspect: peripheral"
        if func == "gpio" and direction:
            return "Inspect: " + ("output" if direction == "out" else "input")
        return MODES["INSPECT"][0]

    def update_pin(self, pin: Pin) -> None:
        self.pin = pin
        f, lvl = pin.fields, pin.level
        # level dot
        if pin.mode in ("LEDOFF", "LEDON", "BREATHE"):
            color = mix(C["off"], C["amber"], (f.get("out") or 0) / 100.0)
        elif pin.mode == "PWM":
            color = mix(C["off"], C["amber"], (f.get("duty") or 0) / 100.0)
        elif pin.mode == "ANALOG":
            color = mix(C["off"], C["accent"], min(1.0, (f.get("mv") or 0) / ANALOG_FULL_MV))
        elif lvl is None or (pin.mode == "INSPECT" and f.get("func") in ("led", "pwm", "periph", None)):
            color = C["off"]
        else:
            color = C["green"] if lvl else C["red"]
        self.dot.itemconfig(self.dot_id, fill=color)
        self.info.config(text=pin.describe())
        # ADC capability: tag on the name, and ANALOG offered in the mode list
        adc = pin.adc
        if bool(adc) != self.analog_ok:
            self._set_mode_list(bool(adc))
            if adc:
                self.adc_chip.config(text=adc.replace("_CH", " ch"))
                self.adc_chip.pack(side="left")
            else:
                self.adc_chip.pack_forget()
        if pin.mode == "ANALOG":
            mv = f.get("mv")
            self.volt_txt.config(text="-.--- V" if mv is None else "{:.3f} V".format(mv / 1000))
            frac = 0.0 if mv is None else max(0.0, min(1.0, mv / ANALOG_FULL_MV))
            self.volt_bar.coords(self.volt_fill, 0, 3, int(230 * frac), 11)
        if time.monotonic() < self._hold_until:
            return  # you're mid-edit; don't fight the controls
        self.mode_var.set(self._mode_text(pin))
        if pin.mode in ("LEDOFF", "LEDON", "BREATHE"):
            self._show_family("led")
            self.bright_var.set(f.get("bright", 50))
            self.period_var.set(f.get("period", 2000) / 1000.0)
            show_period = pin.mode == "BREATHE"
            for w in (self.period_lbl, self.period_scale, self.period_txt):
                w.grid() if show_period else w.grid_remove()
        elif pin.mode == "PWM":
            self._show_family("pwm")
            self.freq_var.set(freq_label(f.get("freq", 1000)))
            self.duty_var.set(f.get("duty", 50))
        elif pin.mode == "ANALOG":
            self._show_family("analog")
        else:
            self._show_family(None)
        self.bright_txt.config(text="{}%".format(round(self.bright_var.get())))
        self.period_txt.config(text="{:.1f} s".format(self.period_var.get()))
        self.duty_txt.config(text="{}%".format(round(self.duty_var.get())))


# ---------------------------------------------------------------------------
# hexpansion EEPROM / I2C card

class InfoStrip(ttk.Frame):
    """Name + VID/PID of the hexpansion in the current port, with tags for
    its detect line, EEPROM, filesystem and the I2C addresses that answered."""

    def __init__(self, parent, on_rescan):
        super().__init__(parent, style="Panel.TFrame")
        self._key = None
        pad = ttk.Frame(self, style="Panel.TFrame")
        pad.pack(fill="x", padx=12, pady=9)
        pad.columnconfigure(1, weight=1)
        left = ttk.Frame(pad, style="Panel.TFrame")
        left.grid(row=0, column=0, columnspan=2, sticky="w", padx=(0, 18))
        self.title = ttk.Label(left, text="-", style="CardTitle.TLabel")
        self.title.pack(anchor="w")
        self.sub = ttk.Label(left, text="", style="CardSub.TLabel")
        self.sub.pack(anchor="w")
        self.tags = ttk.Frame(pad, style="Panel.TFrame")
        self.tags.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self._tag_widgets: List[tk.Label] = []
        self._flow_width = 0
        self.tags.bind("<Configure>", self._on_tags_resize)
        ttk.Button(pad, text="Rescan I2C", command=on_rescan).grid(row=0, column=2, sticky="e")

    def _tag(self, text: str, color: str) -> None:
        self._tag_widgets.append(tk.Label(self.tags, text=text, bg=mix(C["panel"], color, 0.22),
                                          fg=color, font=FONT_SMALL, padx=8, pady=3))

    def _reflow(self) -> None:
        """Lay the tags out left to right, wrapping onto new rows so none are
        ever hidden, however many there are (e.g. a bus full of I2C devices)."""
        width = self.tags.winfo_width()
        if width <= 1:      # not laid out yet; the <Configure> event will call us again
            width = 1000
        row = used = 0
        height = 24
        for tag in self._tag_widgets:
            w, height = tag.winfo_reqwidth(), tag.winfo_reqheight()
            if used and used + w > width:
                row, used = row + 1, 0
            tag.place(x=used, y=row * (height + 5))
            used += w + 6
        # placed children don't size their parent, so set the height ourselves
        self.tags.configure(height=(row + 1) * (height + 5))

    def _on_tags_resize(self, event) -> None:
        if event.width != self._flow_width:      # only width changes re-wrap (height changes are ours)
            self._flow_width = event.width
            self._reflow()

    def update_info(self, info: PortInfo) -> None:
        key = (info.port, info.present, info.detect, info.eeprom_mounted, info.files, info.has_app,
               info.scan, info.scan_error, info.name, info.vid, info.pid, info.uid, info.eeprom_addr,
               info.eeprom_size, info.page_size, tuple(info.i2c))
        if key == self._key:
            return
        self._key = key
        self.title.config(text=info.name or ("No EEPROM header" if info.present else "Empty port"))
        sub = ["Port {}".format(info.port)]
        if info.vid_pid:
            sub.append("VID/PID " + info.vid_pid)
        if info.uid is not None:
            sub.append("unique id {}".format(info.uid))
        if info.header_version:
            sub.append("header v{}".format(info.header_version))
        self.sub.config(text="   \u00b7   ".join(sub))
        for child in self.tags.winfo_children():
            child.destroy()
        self._tag_widgets = []
        if info.detect is None:
            self._tag("detect line n/a", C["muted"])
        elif info.detect:
            self._tag("detect: plugged in", C["amber"])
        else:
            self._tag("detect: empty", C["muted"])
        if info.eeprom_desc:
            self._tag("EEPROM " + info.eeprom_desc, C["accent"])
        if info.eeprom_mounted:
            self._tag("fs mounted \u00b7 {} file{}".format(info.files, "" if info.files == 1 else "s"), C["green"])
            self._tag("app \u2713" if info.has_app else "no app", C["green"] if info.has_app else C["muted"])
        elif info.eeprom_desc:
            self._tag("fs not mounted", C["muted"])
        if info.scan == "err":
            self._tag("I2C scan failed: " + (info.scan_error or "?"), C["red"])
        elif info.scan == "-":
            self._tag("I2C not scanned", C["muted"])
        elif not info.i2c:
            self._tag("I2C: no devices", C["muted"])
        else:
            if len(info.i2c) > 6:
                self._tag("I2C: {} devices".format(len(info.i2c)), C["muted"])
            for addr in info.i2c:
                self._tag("I2C 0x{:02X}".format(addr), C["accent"])
        self._reflow()


# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk, device: Optional[str], sim: bool):
        self.root = root
        self.events: "queue.Queue" = queue.Queue()
        self.worker: Optional[Worker] = None
        self.history: List[str] = []
        self.hist_pos = 0
        self.port_var = tk.IntVar(value=1)
        self.live_var = tk.BooleanVar(value=True)
        self.modify_var = tk.BooleanVar(value=False)
        self._shown_port = None
        self.dev_var = tk.StringVar()
        self.last_link_state = None

        root.title("Pin Tester companion")
        root.configure(bg=C["bg"])
        root.geometry("1090x900")   # wide enough for the widest row (LED breathe)
        root.minsize(1060, 840)
        self._theme()
        self._build_header()
        self._build_rows()
        self._build_console()
        self.status = ttk.Label(root, text="", style="Muted.TLabel")
        self.status.pack(fill="x", padx=16, pady=(0, 8))

        self.apply_editable()
        self.refresh_devices(select=SIMULATOR if sim else device)
        self.set_link_state("disconnected")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.drain)
        self.connect()

    # -- theme ---------------------------------------------------------
    def _theme(self) -> None:
        r = self.root
        style = ttk.Style(r)
        style.theme_use("clam")
        r.option_add("*TCombobox*Listbox.background", C["panel"])
        r.option_add("*TCombobox*Listbox.foreground", C["text"])
        r.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        r.option_add("*TCombobox*Listbox.selectForeground", C["bg"])
        r.option_add("*TCombobox*Listbox.font", FONT)
        style.configure(".", background=C["bg"], foreground=C["text"], font=FONT, borderwidth=0)
        style.configure("TFrame", background=C["bg"])
        style.configure("Panel.TFrame", background=C["panel"])
        style.configure("Row0.TFrame", background=C["row"])
        style.configure("Row1.TFrame", background=C["row_alt"])
        style.configure("TLabel", background=C["bg"], foreground=C["text"])
        style.configure("Title.TLabel", font=FONT_TITLE, foreground=C["text"])
        style.configure("Muted.TLabel", foreground=C["muted"], font=FONT_SMALL)
        style.configure("Panel.TLabel", background=C["panel"])
        style.configure("Chip.TLabel", background=C["panel"], font=FONT_BOLD)
        style.configure("Section.TLabel", foreground=C["accent"], font=FONT_BOLD)
        style.configure("CardTitle.TLabel", background=C["panel"], font=("DejaVu Sans", 12, "bold"))
        style.configure("CardSub.TLabel", background=C["panel"], foreground=C["muted"], font=FONT_SMALL)
        for shade, bg in ((0, C["row"]), (1, C["row_alt"])):
            style.configure("Name{}.TLabel".format(shade), background=bg, font=FONT_BOLD)
            style.configure("Info{}.TLabel".format(shade), background=bg, foreground=C["muted"], font=FONT_SMALL)
        style.configure("TButton", background=C["panel"], foreground=C["text"], padding=(12, 6),
                        bordercolor=C["line"], focusthickness=0, relief="flat")
        style.map("TButton", background=[("active", C["line"]), ("pressed", C["line"])])
        style.configure("Accent.TButton", background=C["accent"], foreground=C["bg"], font=FONT_BOLD)
        style.map("Accent.TButton", background=[("active", "#7ad4ff")])
        style.configure("Danger.TButton", foreground=C["red"])
        style.layout("Port.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})])
        style.configure("Port.TRadiobutton", background=C["panel"], foreground=C["text"],
                        padding=(12, 5), relief="flat", font=FONT_BOLD, anchor="center")
        style.map("Port.TRadiobutton", background=[("selected", C["accent"]), ("active", C["line"])],
                  foreground=[("selected", C["bg"])])
        style.layout("Modify.TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})])
        style.configure("Modify.TRadiobutton", background=C["panel"], foreground=C["text"],
                        padding=(12, 5), relief="flat", font=FONT_BOLD, anchor="center")
        style.map("Modify.TRadiobutton", background=[("selected", C["amber"]), ("active", C["line"])],
                  foreground=[("selected", C["bg"])])
        style.configure("TCheckbutton", background=C["panel"], foreground=C["text"])
        style.map("TCheckbutton", background=[("active", C["panel"])])
        style.configure("TCombobox", fieldbackground=C["panel"], background=C["panel"],
                        foreground=C["text"], arrowcolor=C["text"], bordercolor=C["line"],
                        lightcolor=C["line"], darkcolor=C["line"], padding=3)
        style.map("TCombobox", fieldbackground=[("readonly", C["panel"]), ("disabled", C["panel"])],
                  foreground=[("readonly", C["text"]), ("disabled", C["muted"])],
                  arrowcolor=[("disabled", C["off"])],
                  selectbackground=[("readonly", C["panel"])], selectforeground=[("readonly", C["text"])])
        style.configure("TEntry", fieldbackground=C["panel"], foreground=C["text"],
                        insertcolor=C["text"], bordercolor=C["line"], padding=5)
        style.configure("Horizontal.TScale", background=C["row"], troughcolor=C["line"],
                        bordercolor=C["line"], lightcolor=C["accent"], darkcolor=C["accent"])
        style.map("Horizontal.TScale", lightcolor=[("disabled", C["off"])],
                  darkcolor=[("disabled", C["off"])], background=[("disabled", C["row"])])
        style.configure("Vertical.TScrollbar", background=C["panel"], troughcolor=C["bg"],
                        arrowcolor=C["muted"], bordercolor=C["bg"])

    # -- layout --------------------------------------------------------
    def _build_header(self) -> None:
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=16, pady=(14, 8))
        ttk.Label(top, text="Pin Tester", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  companion  v" + __version__, style="Muted.TLabel").pack(side="left", pady=(7, 0))

        conn = ttk.Frame(self.root, style="Panel.TFrame")
        conn.pack(fill="x", padx=16)
        inner = ttk.Frame(conn, style="Panel.TFrame")
        inner.pack(fill="x", padx=12, pady=10)
        ttk.Label(inner, text="Device", style="Panel.TLabel").pack(side="left")
        self.dev_box = ttk.Combobox(inner, textvariable=self.dev_var, state="readonly", width=34)
        self.dev_box.pack(side="left", padx=(8, 4))
        ttk.Button(inner, text="↻", width=3, command=self.refresh_devices).pack(side="left", padx=(0, 8))
        self.conn_btn = ttk.Button(inner, text="Connect", style="Accent.TButton", command=self.toggle_connect)
        self.conn_btn.pack(side="left")
        self.chip_dot = tk.Canvas(inner, width=14, height=14, bg=C["panel"], highlightthickness=0)
        self.chip_dot.pack(side="left", padx=(16, 6))
        self.chip_id = self.chip_dot.create_oval(2, 2, 12, 12, fill=C["muted"], outline="")
        self.chip_text = ttk.Label(inner, text="", style="Chip.TLabel")
        self.chip_text.pack(side="left")

        ctrl = ttk.Frame(self.root, style="Panel.TFrame")
        ctrl.pack(fill="x", padx=16, pady=(8, 8))
        inner = ttk.Frame(ctrl, style="Panel.TFrame")
        inner.pack(fill="x", padx=12, pady=10)
        ttk.Label(inner, text="Hexpansion port", style="Panel.TLabel").pack(side="left", padx=(0, 8))
        for n in range(1, 7):
            ttk.Radiobutton(inner, text=str(n), value=n, variable=self.port_var, style="Port.TRadiobutton",
                            command=lambda n=n: self.change_port(n)).pack(side="left", padx=1)
        self.present_lbl = ttk.Label(inner, text="", style="Chip.TLabel")
        self.present_lbl.pack(side="left", padx=(14, 0))
        ttk.Checkbutton(inner, text="Live", variable=self.live_var, command=self.live_toggled).pack(side="right")
        # Inspect only (default) locks every pin control; Modify unlocks them.
        # Switching only changes this window -- nothing is sent to the badge.
        ttk.Radiobutton(inner, text="Modify", value=True, variable=self.modify_var, style="Modify.TRadiobutton",
                        command=self.modify_changed).pack(side="right", padx=(0, 8))
        ttk.Radiobutton(inner, text="Inspect only", value=False, variable=self.modify_var, style="Port.TRadiobutton",
                        command=self.modify_changed).pack(side="right", padx=(0, 1))
        self.reset_btn = ttk.Button(inner, text="Reset all pins", style="Danger.TButton", command=self.reset_all)
        self.reset_btn.pack(side="right", padx=(0, 14))
        self.reset_btn.state(["disabled"])

    def _build_rows(self) -> None:
        self.info = InfoStrip(self.root, self.rescan_i2c)
        self.info.pack(fill="x", padx=16)
        body = ttk.Frame(self.root)
        body.pack(fill="x", padx=16)
        self.rows = {}
        for title, names in (("LS pins  ·  AW9523B expander  ·  LED capable", LS_PINS),
                             ("HS pins  ·  ESP32 GPIO  ·  hardware PWM", HS_PINS)):
            ttk.Label(body, text=title, style="Section.TLabel").pack(anchor="w", pady=(8, 4))
            card = ttk.Frame(body, style="Panel.TFrame")
            card.pack(fill="x")
            for i, name in enumerate(names):
                row = PinRow(card, name, i % 2, self.send_mode, self.send_values)
                row.pack(fill="x", padx=1, pady=(1 if i == 0 else 0, 1 if i == len(names) - 1 else 0))
                self.rows[name] = row

    def _build_console(self) -> None:
        box = ttk.Frame(self.root)
        box.pack(fill="both", expand=True, padx=16, pady=(12, 6))
        ttk.Label(box, text="Console  ·  raw protocol, e.g.  SET HS_F PWM 1000 50", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        wrap = ttk.Frame(box, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True)
        self.log = tk.Text(wrap, height=7, bg=C["panel"], fg=C["text"], insertbackground=C["text"],
                           relief="flat", font=FONT_MONO, state="disabled", wrap="none",
                           padx=8, pady=6, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("cmd", C["accent"]), ("ok", C["green"]), ("err", C["red"]), ("dim", C["muted"])):
            self.log.tag_configure(tag, foreground=color)
        entry_row = ttk.Frame(box)
        entry_row.pack(fill="x", pady=(6, 0))
        self.entry = ttk.Entry(entry_row, font=FONT_MONO)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self.send_console)
        self.entry.bind("<Up>", lambda e: self.history_step(-1))
        self.entry.bind("<Down>", lambda e: self.history_step(1))
        ttk.Button(entry_row, text="Send", command=self.send_console).pack(side="left", padx=(8, 0))
        ttk.Button(entry_row, text="Clear", command=self.clear_log).pack(side="left", padx=(8, 0))

    # -- connection ----------------------------------------------------
    def refresh_devices(self, select: Optional[str] = None) -> None:
        devices = [d for d, _desc in find_devices()]
        values = devices + [SIMULATOR]
        self.dev_box.configure(values=values)
        if select in values:
            self.dev_var.set(select)
        elif self.dev_var.get() not in values:
            self.dev_var.set(values[0])

    def set_link_state(self, state: str, msg: str = "") -> None:
        color, text = LINK_STATES[state]
        if state == "error" and msg:
            text = msg
        self.chip_dot.itemconfig(self.chip_id, fill=color)
        self.chip_text.config(text=text, foreground=color)
        self.conn_btn.config(text="Disconnect" if state != "disconnected" else "Connect")

    def toggle_connect(self) -> None:
        if self.worker is None:
            self.connect()
        else:
            self.disconnect()

    def connect(self) -> None:
        self.disconnect(quiet=True)
        choice = self.dev_var.get()
        sim = choice == SIMULATOR
        self.worker = Worker(None if sim else choice, sim, self.events)
        self.worker.live = self.live_var.get()
        self.worker.start()
        self.set_link_state("connecting")

    def disconnect(self, quiet: bool = False) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.modify_var.set(False)
        self.apply_editable()
        if not quiet:
            self.set_link_state("disconnected")
            self.present_lbl.config(text="")

    def close(self) -> None:
        self.disconnect(quiet=True)
        self.root.destroy()

    def live_toggled(self) -> None:
        if self.worker:
            self.worker.live = self.live_var.get()

    # -- actions -------------------------------------------------------
    def _submit(self, job) -> None:
        if self.worker is None:
            self.status.config(text="Not connected.")
            return
        self.worker.submit(job)

    def send_mode(self, pin: str, word: str, args: tuple) -> None:
        self._submit(lambda link: link.set_mode(pin, word, *args))
        self._submit(lambda link: self.events.put(("dump", link.dump())))

    def send_values(self, pin: str, word: str, args: tuple) -> None:
        self._submit(lambda link: link.set_mode(pin, word, *args))

    def change_port(self, n: int) -> None:
        self._submit(lambda link: link.port(n))
        self._submit(lambda link: self.events.put(("dump", link.dump())))
        self.rescan_i2c()

    def rescan_i2c(self) -> None:
        self._submit(lambda link: self.events.put(("info", link.portinfo(scan=True))))

    def reset_all(self) -> None:
        if messagebox.askyesno("Reset all pins", "Hand every pin on this port back?\n\n"
                               "Anything the app took over is released to INPUT, then pins return to "
                               "their default (read-only inspect if a hexpansion is present).",
                               parent=self.root):
            self._submit(lambda link: link.reset())
            self._submit(lambda link: self.events.put(("dump", link.dump())))

    def modify_changed(self) -> None:
        self.apply_editable()
        if self.modify_var.get():
            self.status.config(text="Modify enabled: changes are sent to the pins you edit. "
                                    "Nothing has been changed yet.")
        else:
            self.status.config(text="Inspect only: controls locked. Pins keep whatever state they are in.")

    def apply_editable(self) -> None:
        editable = self.modify_var.get()
        for row in self.rows.values():
            row.set_editable(editable)
        self.reset_btn.state(["!disabled"] if editable else ["disabled"])

    # -- console -------------------------------------------------------
    def send_console(self, _event=None) -> None:
        cmd = self.entry.get().strip()
        if not cmd:
            return
        self.history.append(cmd)
        self.hist_pos = len(self.history)
        self.entry.delete(0, "end")
        self._submit(lambda link: link.request(cmd))

    def history_step(self, delta: int) -> str:
        if not self.history:
            return "break"
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + delta))
        self.entry.delete(0, "end")
        if self.hist_pos < len(self.history):
            self.entry.insert(0, self.history[self.hist_pos])
        return "break"

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def write_log(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- events from the worker ----------------------------------------
    def drain(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "link":
                    if ev[1] != self.last_link_state:
                        self.write_log("-- {}".format(LINK_STATES[ev[1]][1] if ev[1] != "error" else ev[2]), "dim")
                        self.last_link_state = ev[1]
                    self.set_link_state(ev[1], ev[2])
                    if ev[1] in ("remote-off", "error"):
                        self.status.config(text=ev[2])
                    elif ev[1] == "ok":
                        self.status.config(text="")
                elif kind == "dump":
                    self.apply_port(ev[1])
                elif kind == "info":
                    self.info.update_info(ev[1])
                elif kind == "log":
                    self.write_log("> " + ev[1], "cmd")
                    for ln in ev[2]:
                        self.write_log("  " + ln, "err" if ln.startswith("ERR") else "dim" if ln.startswith(("PIN", "DUMP")) else "ok")
                    if ev[3]:
                        self.write_log("  " + ev[3], "err")
                elif kind == "error":
                    self.status.config(text=ev[1])
        except queue.Empty:
            pass
        self.root.after(50, self.drain)

    def apply_port(self, port: Port) -> None:
        self.port_var.set(port.port)
        if self._shown_port is not None and port.port != self._shown_port and self.modify_var.get():
            # a different physical connector: make editing a fresh, deliberate choice
            self.modify_var.set(False)
            self.apply_editable()
            self.status.config(text="Port changed: back to Inspect only. Choose Modify again to edit.")
        self._shown_port = port.port
        if port.present:
            self.present_lbl.config(text="● hexpansion present", foreground=C["amber"])
        else:
            self.present_lbl.config(text="○ empty port", foreground=C["muted"])
        for pin in port.pins:
            row = self.rows.get(pin.name)
            if row:
                row.update_pin(pin)


def run_gui(device: Optional[str] = None, sim: bool = False) -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("error: cannot open a display ({}). Use the command-line tools instead: "
              "pintester -h".format(exc))
        return 1
    App(root, device, sim)
    root.mainloop()
    return 0
