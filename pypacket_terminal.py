#!/usr/bin/env python3
"""
PyPacket Terminal v1.10.1
Classic packet-radio GUI inspired by UZ7HO EasyTerm.
v1.10.0: validate existing backend port configuration before attaching.

Backend:
    ax25_kiss_engine_v1_6_app_backend.py

Technology:
    Python 3.11+
    tkinter / ttk
    TCP JSON Lines
"""

import json
import queue
import socket
import threading
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText
from datetime import datetime
from pathlib import Path


APP_TITLE = "PyPacket Terminal v1.10.1"
API_HOST = "127.0.0.1"
API_PORT = 8010

DEFAULT_LOCAL = "N0CAL"
DEFAULT_REMOTE = ""
DEFAULT_VIA = ""

MAX_SESSION_SLOTS = 8
BACKEND_SCRIPT = "pypacket_backend.py"
CONFIG_FILE = "pypacket_terminal_config.json"


def norm_call(value: str) -> str:
    return value.strip().upper()


def callsign_base(value: str) -> str:
    return norm_call(value).split("-", 1)[0]


def choose_monospace_font(root, size=10):
    """Choose an installed fixed-width font for the current platform."""
    try:
        available = set(tkfont.families(root))
    except Exception:
        available = set()

    if sys.platform == "win32":
        candidates = (
            "Consolas",
            "Cascadia Mono",
            "Lucida Console",
            "Courier New",
        )
    elif sys.platform == "darwin":
        candidates = (
            "Menlo",
            "SF Mono",
            "Monaco",
            "Courier",
        )
    else:
        candidates = (
            "DejaVu Sans Mono",
            "Liberation Mono",
            "Noto Sans Mono",
            "Ubuntu Mono",
            "Courier New",
        )

    for family in candidates:
        if family in available:
            return tkfont.Font(
                root=root,
                family=family,
                size=size,
            )

    fallback = tkfont.nametofont("TkFixedFont").copy()
    fallback.configure(size=size)
    return fallback


def parse_via(value: str):
    value = value.strip()
    if not value:
        return []
    return [
        norm_call(x)
        for x in value.replace(";", ",").split(",")
        if norm_call(x)
    ]


class ApiClient:
    def __init__(self, event_queue):
        self.event_queue = event_queue
        self.sock = None
        self.send_lock = threading.Lock()
        self.running = False

    @property
    def connected(self):
        return self.running and self.sock is not None

    def connect(self, host, port):
        self.close()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((host, port))
        sock.settimeout(None)

        self.sock = sock
        self.running = True

        threading.Thread(
            target=self._reader_loop,
            daemon=True,
        ).start()

    def close(self):
        self.running = False
        sock = self.sock
        self.sock = None

        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def send(self, obj):
        if not self.connected:
            raise RuntimeError("Backend API not connected")

        raw = (
            json.dumps(
                obj,
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
        ).encode("utf-8")

        with self.send_lock:
            self.sock.sendall(raw)

    def _reader_loop(self):
        buf = bytearray()

        try:
            while self.running:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Backend closed connection")

                buf.extend(chunk)

                while True:
                    pos = buf.find(b"\n")
                    if pos < 0:
                        break

                    raw = bytes(buf[:pos])
                    del buf[:pos + 1]

                    if not raw.strip():
                        continue

                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except Exception as exc:
                        self.event_queue.put({
                            "event": "_client_error",
                            "error": f"Invalid backend JSON: {exc}",
                        })
                        continue

                    self.event_queue.put(msg)

        except Exception as exc:
            if self.running:
                self.event_queue.put({
                    "event": "_client_error",
                    "error": str(exc),
                })

        finally:
            self.running = False
            sock = self.sock
            self.sock = None

            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

            self.event_queue.put({
                "event": "_client_disconnected",
            })


class TerminalSession:
    def __init__(self, local, remote, via=None, port_id="0", port_name=None):
        self.port_id = str(port_id)
        self.port_name = port_name or self.port_id
        self.local = norm_call(local)
        self.remote = norm_call(remote)
        self.via = list(via or [])
        self.state = "UNKNOWN"
        self.buffer = ""


class PacketTerminalApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x780")

        self.mono_font = choose_monospace_font(
            self.root,
            12,
        )
        self.mono_font_small = choose_monospace_font(
            self.root,
            11,
        )

        # Match session buttons to the application's normal UI typography,
        # only slightly larger for readability on macOS/HiDPI displays.
        self.session_font = tkfont.nametofont(
            "TkDefaultFont"
        ).copy()
        try:
            base_size = abs(
                int(self.session_font.actual("size"))
            )
        except Exception:
            base_size = 10
        self.session_font.configure(
            size=max(11, base_size + 1)
        )
        self.root.minsize(900, 580)

        self.events = queue.Queue()
        self.api = ApiClient(self.events)

        self.first_run = not self.config_path().exists()
        self.config = self.load_config()

        self.sessions = {}
        self.session_order = []
        self.active_key = None

        # call -> dict(last, direction, via, frame_type)
        self.mheard = {}
        self.restore_mheard_from_config()

        self.monitor_visible = True
        self.backend_process = None
        self.kiss_port_states = {}
        self.kiss_port_status = {}
        self.digi_enabled_var = tk.BooleanVar(
            value=bool(
                self.config.get("digi_enabled", False)
            )
        )

        # Beacon configuration. Menu edits these values;
        # toolbar button sends them immediately.
        self.beacon_dest = self.config.get("beacon_dest", "CQ")
        self.beacon_via = self.config.get("beacon_via", "")
        self.beacon_text = self.config.get("beacon_text", "")
        self.beacon_port_ids = list(
            self.config.get(
                "beacon_port_ids",
                []
            )
        )

        self._build_menu()
        self._build_ui()
        self._setup_text_tags()
        self.refresh_mheard()
        self._poll_events()

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.on_close,
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(
            label="Connect backend",
            command=self.connect_api,
        )
        file_menu.add_command(
            label="Disconnect backend",
            command=self.disconnect_api,
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Configure Ports...",
            command=self.configure_ports_dialog,
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Exit",
            command=self.on_close,
        )
        menubar.add_cascade(label="File", menu=file_menu)

        station_menu = tk.Menu(menubar, tearoff=False)
        station_menu.add_command(
            label="Configure...",
            command=self.station_config_dialog,
        )
        station_menu.add_separator()
        station_menu.add_command(
            label="Connect...",
            command=self.connect_dialog,
        )
        station_menu.add_command(
            label="Disconnect",
            command=self.disconnect_current,
        )
        station_menu.add_separator()
        station_menu.add_checkbutton(
            label="DIGI ON",
            variable=self.digi_enabled_var,
            command=self.toggle_digi,
        )
        menubar.add_cascade(
            label="Stations",
            menu=station_menu,
        )

        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_command(
            label="Clear terminal",
            command=self.clear_terminal,
        )
        view_menu.add_command(
            label="Clear monitor",
            command=self.clear_monitor,
        )
        view_menu.add_command(
            label="Clear MHeard",
            command=self.clear_mheard,
        )
        menubar.add_cascade(
            label="View",
            menu=view_menu,
        )

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(
            label="About",
            command=self.show_about,
        )
        menubar.add_cascade(
            label="About",
            menu=help_menu,
        )

        self.root.config(menu=menubar)

    def show_about(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("About")
        dlg.transient(self.root)
        dlg.resizable(False, False)

        ttk.Label(
            dlg,
            text=(
                "PyPacket Terminal v1.10.1\n"
                "SQ9MDD\n"
                "Python / Tkinter AX.25 terminal\n\n"
                f"Terminal font: {self.mono_font.actual('family')}"
            ),
            justify="center",
            padding=(28, 18),
        ).pack()

        ttk.Button(
            dlg,
            text="OK",
            command=dlg.destroy,
        ).pack(pady=(0, 14))

        self.place_child_window(
            dlg,
            x_offset=90,
            y_offset=70,
        )
        dlg.grab_set()
        dlg.focus_set()

    def _build_ui(self):
        # Use the same fixed-width font for text-heavy widgets so
        # ASCII-art and hand-aligned columns remain stable across OSes.
        style = ttk.Style(self.root)
        style.configure(
            "PyPacket.Treeview",
            font=self.mono_font_small,
        )
        style.configure(
            "PyPacket.Treeview.Heading",
            font=self.mono_font_small,
        )
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # --------------------------------------------------------------
        # Toolbar
        # --------------------------------------------------------------
        toolbar = ttk.Frame(self.root)
        toolbar.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=4,
            pady=(4, 2),
        )

        self.btn_connect = ttk.Button(
            toolbar,
            text="⚡ Connect",
            command=self.connect_dialog,
        )
        self.btn_connect.pack(side="left", padx=2)

        self.btn_disconnect = ttk.Button(
            toolbar,
            text="🔌 Disconnect",
            command=self.disconnect_current,
        )
        self.btn_disconnect.pack(side="left", padx=2)

        self.btn_clear = ttk.Button(
            toolbar,
            text="🧹 Clear",
            command=self.clear_current_session,
        )
        self.btn_clear.pack(side="left", padx=2)

        ttk.Separator(
            toolbar,
            orient="vertical",
        ).pack(
            side="left",
            fill="y",
            padx=5,
        )

        ttk.Button(
            toolbar,
            text="📡 Beacon",
            command=self.send_beacon,
        ).pack(side="left", padx=2)

        ttk.Button(
            toolbar,
            text="📻 Mheard",
            command=self.focus_mheard,
        ).pack(side="left", padx=2)

        ttk.Button(
            toolbar,
            text="🧹 Clear MH",
            command=self.clear_mheard,
        ).pack(side="left", padx=2)

        ttk.Button(
            toolbar,
            text="📬 Mailbox",
            command=self.mailbox_info,
        ).pack(side="left", padx=2)

        ttk.Separator(
            toolbar,
            orient="vertical",
        ).pack(
            side="left",
            fill="y",
            padx=5,
        )

        self.local_var = tk.StringVar(
            value=self.config.get("local", DEFAULT_LOCAL)
        )
        self.local_var.trace_add(
            "write",
            self._on_local_changed,
        )
        self.callsign_display_var = tk.StringVar(
            value=f"CALLSIGN: {self.local_var.get()}"
        )
        ttk.Label(
            toolbar,
            textvariable=self.callsign_display_var,
        ).pack(side="left", padx=(2, 8))

        ttk.Label(
            toolbar,
            text="API:"
        ).pack(side="left", padx=(4, 2))

        self.api_state_var = tk.StringVar(
            value="OFF"
        )
        self.api_state_label = ttk.Label(
            toolbar,
            textvariable=self.api_state_var,
        )
        self.api_state_label.pack(side="left")

        # --------------------------------------------------------------
        # Main split: terminal + fixed MH panel
        # --------------------------------------------------------------
        main = ttk.Panedwindow(
            self.root,
            orient="horizontal",
        )
        main.grid(
            row=2,
            column=0,
            sticky="nsew",
            padx=4,
            pady=2,
        )

        # LEFT area
        left = ttk.Frame(main)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        self.terminal = ScrolledText(
            left,
            wrap="word",
            font=self.mono_font,
            bg="#666666",
            fg="white",
            insertbackground="white",
            selectbackground="#888888",
            relief="sunken",
            borderwidth=1,
        )
        self.terminal.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(
            left,
            textvariable=self.input_var,
            font=self.mono_font,
        )
        self.input_entry.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(2, 2),
        )
        self.input_entry.bind(
            "<Return>",
            self._send_from_entry,
        )

        # session strip similar to EasyTerm slots
        session_strip = ttk.Frame(left)
        session_strip.grid(
            row=2,
            column=0,
            sticky="ew",
        )

        self.slot_buttons = []
        for idx in range(MAX_SESSION_SLOTS):
            btn = tk.Button(
                session_strip,
                text="...",
                font=self.session_font,
                width=12,
                relief="raised",
                borderwidth=1,
                bg="#e8e8e8",
                activebackground="#dcdcdc",
                command=lambda i=idx: self.select_slot(i),
            )
            btn.pack(
                side="left",
                fill="x",
                expand=True,
                padx=(0 if idx == 0 else 1, 1),
            )
            btn.bind(
                "<Double-Button-1>",
                lambda _e, i=idx: self.reconnect_slot(i),
            )
            self.slot_buttons.append(btn)

        self.monitor_toggle = ttk.Button(
            session_strip,
            text="Monitor",
            width=12,
            command=self.toggle_monitor,
        )
        self.monitor_toggle.pack(
            side="right",
            padx=(3, 0),
        )

        main.add(left, weight=4)

        # RIGHT MHEARD panel
        mh_frame = ttk.LabelFrame(
            main,
            text="MH / Heard stations",
        )
        mh_frame.columnconfigure(0, weight=1)
        mh_frame.rowconfigure(0, weight=1)

        self.mh_tree = ttk.Treeview(
            mh_frame,
            columns=("call", "time", "port", "via"),
            show="headings",
            selectmode="browse",
            style="PyPacket.Treeview",
        )
        self.mh_tree.heading("call", text="Call")
        self.mh_tree.heading("time", text="Last")
        self.mh_tree.heading("port", text="Port")
        self.mh_tree.heading("via", text="Via")

        self.mh_tree.column(
            "call",
            width=105,
            anchor="w",
        )
        self.mh_tree.column(
            "time",
            width=70,
            anchor="center",
        )
        self.mh_tree.column("port", width=110, anchor="center")
        self.mh_tree.column(
            "via",
            width=140,
            anchor="w",
        )

        mh_scroll = ttk.Scrollbar(
            mh_frame,
            orient="vertical",
            command=self.mh_tree.yview,
        )
        self.mh_tree.configure(
            yscrollcommand=mh_scroll.set
        )

        self.mh_tree.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        mh_scroll.grid(
            row=0,
            column=1,
            sticky="ns",
        )

        self.mh_tree.bind(
            "<Double-1>",
            self.connect_from_mheard,
        )

        main.add(mh_frame, weight=1)

        # --------------------------------------------------------------
        # Monitor pane
        # --------------------------------------------------------------
        self.monitor_frame = ttk.LabelFrame(
            self.root,
            text="Monitor",
        )
        self.monitor_frame.grid(
            row=3,
            column=0,
            sticky="nsew",
            padx=4,
            pady=(2, 2),
        )
        self.monitor_frame.columnconfigure(0, weight=1)
        self.monitor_frame.rowconfigure(0, weight=1)

        self.monitor = ScrolledText(
            self.monitor_frame,
            wrap="none",
            height=8,
            font=self.mono_font_small,
            bg="#909090",
            fg="white",
            insertbackground="white",
            relief="sunken",
            borderwidth=1,
        )
        self.monitor.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        # --------------------------------------------------------------
        # Status bar
        # --------------------------------------------------------------
        status = ttk.Frame(self.root)
        status.grid(
            row=4,
            column=0,
            sticky="ew",
            padx=4,
            pady=(2, 4),
        )

        self.talk_state_var = tk.StringVar(value="TALK")
        ttk.Label(
            status,
            textvariable=self.talk_state_var,
            relief="sunken",
            anchor="center",
            width=10,
        ).pack(side="left")

        self.session_state_var = tk.StringVar(
            value="Not connected"
        )
        ttk.Label(
            status,
            textvariable=self.session_state_var,
            relief="sunken",
            anchor="center",
            width=28,
        ).pack(side="left", padx=(2, 0))

        self.buffer_state_var = tk.StringVar(
            value="Buffer: 0"
        )
        ttk.Label(
            status,
            textvariable=self.buffer_state_var,
            relief="sunken",
            anchor="center",
            width=12,
        ).pack(side="left", padx=(2, 0))

        self.term_state_var = tk.StringVar(
            value="TERM ON"
        )
        ttk.Label(
            status,
            textvariable=self.term_state_var,
            relief="sunken",
            anchor="center",
            width=10,
        ).pack(side="left", padx=(2, 0))

        self.kiss_state_var = tk.StringVar(
            value="TNC: ?"
        )
        ttk.Label(
            status,
            textvariable=self.kiss_state_var,
            relief="sunken",
            anchor="center",
            width=10,
        ).pack(side="left", padx=(2, 0))

        self.port_status_frame = ttk.Frame(status)
        self.port_status_frame.pack(
            side="left",
            padx=(2, 0),
        )
        self.port_status_labels = {}
        self.refresh_port_statusbar()

        self.api_detail_var = tk.StringVar(
            value=f"{API_HOST}:{API_PORT}"
        )
        ttk.Label(
            status,
            textvariable=self.api_detail_var,
            relief="sunken",
            anchor="w",
        ).pack(
            side="left",
            fill="x",
            expand=True,
            padx=(2, 0),
        )

    def _setup_text_tags(self):
        self.terminal.tag_configure(
            "rx",
            foreground="white",
        )
        self.terminal.tag_configure(
            "tx",
            foreground="#ffffaa",
        )
        self.terminal.tag_configure(
            "sys",
            foreground="#aaffaa",
        )
        self.terminal.tag_configure(
            "err",
            foreground="#ffb0b0",
        )

        self.monitor.tag_configure(
            "rx",
            foreground="white",
        )
        self.monitor.tag_configure(
            "tx",
            foreground="#ffffaa",
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def config_path(self):
        return Path(__file__).resolve().with_name(CONFIG_FILE)

    def load_config(self):
        path = self.config_path()

        defaults = {
            "local": DEFAULT_LOCAL,
            "digi_callsign": DEFAULT_LOCAL,
            "station_info": "",
            "welcome_text": "",
            "bye_text": "",
            "beacon_dest": "CQ",
            "beacon_via": "",
            "beacon_text": "",
            "beacon_interval_min": 0,
            "ports": [],
            "default_port_id": "",
            "beacon_port_ids": [],
            "mheard": {},
            "digi_enabled": False,
            "digi_route_ttl": 3600,
        }

        if not path.exists():
            return defaults

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return defaults

            local = norm_call(
                str(data.get("local", DEFAULT_LOCAL))
            )
            if not local:
                local = DEFAULT_LOCAL

            defaults["local"] = local

            digi_callsign = norm_call(
                str(data.get("digi_callsign", local))
            )
            defaults["digi_callsign"] = (
                digi_callsign or local
            )
            defaults["station_info"] = str(
                data.get("station_info", "")
            )
            defaults["welcome_text"] = str(
                data.get("welcome_text", "")
            )
            defaults["bye_text"] = str(
                data.get("bye_text", "")
            )
            defaults["beacon_dest"] = norm_call(
                str(data.get("beacon_dest", "CQ"))
            ) or "CQ"
            defaults["beacon_via"] = str(
                data.get("beacon_via", "")
            )
            defaults["beacon_text"] = str(
                data.get("beacon_text", "")
            )
            try:
                defaults["beacon_interval_min"] = max(
                    0,
                    int(data.get("beacon_interval_min", 0))
                )
            except (TypeError, ValueError):
                defaults["beacon_interval_min"] = 0

            raw_ports = data.get("ports")
            if isinstance(raw_ports, list):
                clean=[]
                used=set()
                for idx, raw in enumerate(raw_ports):
                    if not isinstance(raw, dict):
                        continue
                    pid=str(raw.get("id",f"port{idx}")).strip() or f"port{idx}"
                    if pid in used:
                        continue
                    used.add(pid)
                    try:
                        tcp_port=int(raw.get("port",8001))
                    except (TypeError,ValueError):
                        tcp_port=8001
                    if not 1 <= tcp_port <= 65535:
                        tcp_port=8001
                    clean.append({"id":str(len(clean)),"name":str(raw.get("name",f"Port {len(clean)+1}")).strip() or f"Port {len(clean)+1}","type":"kiss_tcp","host":str(raw.get("host","127.0.0.1")).strip() or "127.0.0.1","port":tcp_port})
                defaults["ports"]=clean
            elif "kiss_host" in data or "kiss_port" in data:
                try:
                    op=int(data.get("kiss_port",8001))
                except (TypeError,ValueError):
                    op=8001
                defaults["ports"]=[{"id":"0","name":"Port 1","type":"kiss_tcp","host":str(data.get("kiss_host","127.0.0.1")).strip() or "127.0.0.1","port":op}]
            valid={p["id"] for p in defaults["ports"]}
            dp=str(data.get("default_port_id",""))

            if valid:
                defaults["default_port_id"] = (
                    dp
                    if dp in valid
                    else defaults["ports"][0]["id"]
                )
            else:
                defaults["default_port_id"] = ""

            raw_bp = data.get("beacon_port_ids")
            if isinstance(raw_bp, list):
                defaults["beacon_port_ids"] = [
                    str(x)
                    for x in raw_bp
                    if str(x) in valid
                ]
            else:
                old_bp = str(
                    data.get("beacon_port_id", "")
                )
                defaults["beacon_port_ids"] = (
                    [old_bp]
                    if old_bp in valid
                    else (
                        [defaults["default_port_id"]]
                        if defaults["default_port_id"]
                        else []
                    )
                )

            raw_mheard = data.get("mheard", {})
            if isinstance(raw_mheard, dict):
                defaults["mheard"] = raw_mheard

            defaults["digi_enabled"] = bool(
                data.get("digi_enabled", False)
            )

            try:
                ttl = int(data.get("digi_route_ttl", 3600))
                if ttl >= 60:
                    defaults["digi_route_ttl"] = ttl
            except (TypeError, ValueError):
                pass

            return defaults

        except Exception:
            return defaults

    def save_config(self):
        local = norm_call(
            self.local_var.get()
            if hasattr(self, "local_var")
            else self.config.get("local", DEFAULT_LOCAL)
        )
        if not local:
            local = DEFAULT_LOCAL

        self.config["local"] = local
        self.config["digi_callsign"] = norm_call(
            self.config.get("digi_callsign", local)
        ) or local
        self.config["station_info"] = str(
            self.config.get("station_info", "")
        )
        self.config["welcome_text"] = str(
            self.config.get("welcome_text", "")
        )
        self.config["bye_text"] = str(
            self.config.get("bye_text", "")
        )
        self.config["beacon_dest"] = getattr(
            self,
            "beacon_dest",
            self.config.get("beacon_dest", "CQ"),
        )
        self.config["beacon_via"] = getattr(
            self,
            "beacon_via",
            self.config.get("beacon_via", ""),
        )
        self.config["beacon_text"] = getattr(
            self,
            "beacon_text",
            self.config.get("beacon_text", ""),
        )
        # Port IDs are always consecutive logical numbers: 0, 1, 2...
        for idx, port in enumerate(self.config.get("ports", [])):
            port["id"] = str(idx)

        valid = {
            p["id"]
            for p in self.config.get("ports", [])
        }

        if valid:
            if self.config.get("default_port_id") not in valid:
                self.config["default_port_id"] = self.config["ports"][0]["id"]
        else:
            self.config["default_port_id"] = ""

        beacon_ids = getattr(
            self,
            "beacon_port_ids",
            self.config.get("beacon_port_ids", [])
        )
        beacon_ids = [
            str(x)
            for x in beacon_ids
            if str(x) in valid
        ]
        if not beacon_ids and self.config.get("default_port_id"):
            beacon_ids = [self.config["default_port_id"]]

        self.beacon_port_ids = beacon_ids
        self.config["beacon_port_ids"] = beacon_ids
        self.config.pop("beacon_port_id", None)
        self.config["mheard"] = self.serialize_mheard()
        self.config["digi_enabled"] = bool(
            self.digi_enabled_var.get()
            if hasattr(self, "digi_enabled_var")
            else self.config.get("digi_enabled", False)
        )

        try:
            self.config_path().write_text(
                json.dumps(
                    self.config,
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            # Configuration persistence must never kill the terminal.
            print(f"Config save failed: {exc}")

    def _on_local_changed(self, *_args):
        value = norm_call(self.local_var.get())
        if not value:
            return

        self.config["local"] = value
        if hasattr(self, "callsign_display_var"):
            self.callsign_display_var.set(
                f"CALLSIGN: {value}"
            )
        self.save_config()

    def place_child_window(self, window, x_offset=70, y_offset=70):
        """
        Position a separate dialog relative to the top-left corner of the
        main terminal window, not relative to the desktop/screen.
        """
        self.root.update_idletasks()
        window.update_idletasks()

        x = self.root.winfo_rootx() + x_offset
        y = self.root.winfo_rooty() + y_offset

        window.geometry(f"+{x}+{y}")

    def port_by_id(self, port_id):
        for p in self.config.get("ports", []):
            if p.get("id") == port_id:
                return p
        return None

    def port_name(self, port_id):
        p = self.port_by_id(port_id)
        return p.get("name", port_id) if p else port_id

    def refresh_port_statusbar(self):
        if not hasattr(self, "port_status_frame"):
            return

        for widget in self.port_status_frame.winfo_children():
            widget.destroy()

        self.port_status_labels = {}

        for idx, port in enumerate(
            self.config.get("ports", [])
        ):
            port_id = str(idx)
            state = self.kiss_port_status.get(
                port_id,
                (
                    "connected"
                    if self.kiss_port_states.get(port_id)
                    else "disconnected"
                )
            )

            if state == "connected":
                symbol = "●"
                fg = "#008000"
                bg = "#dff4df"
                suffix = ""
                weight = "bold"
            elif state == "aborted":
                symbol = "●"
                fg = "#b00020"
                bg = "#f7d7dc"
                suffix = " ABORT"
                weight = "bold"
            elif state in ("connecting", "retrying"):
                symbol = "●"
                fg = "#b36b00"
                bg = "#fff0d2"
                suffix = ""
                weight = "normal"
            else:
                symbol = "○"
                fg = "#777777"
                bg = "#e3e3e3"
                suffix = ""
                weight = "normal"

            label = tk.Label(
                self.port_status_frame,
                text=(
                    f"{symbol} P{idx} "
                    f"{port.get('name', f'Port {idx + 1}')}"
                    f"{suffix}"
                ),
                relief="sunken",
                borderwidth=1,
                padx=5,
                fg=fg,
                bg=bg,
                font=(
                    "TkDefaultFont",
                    9,
                    weight,
                ),
            )
            label.pack(
                side="left",
                padx=(0, 2)
            )
            self.port_status_labels[port_id] = label

    def configure_ports_dialog(self, first_run=False):
        dlg=tk.Toplevel(self.root)
        dlg.title(
            "First run - Configure KISS Ports"
            if first_run
            else "Configure KISS Ports"
        )
        dlg.transient(self.root); dlg.grab_set(); dlg.resizable(False,False)
        self.place_child_window(dlg,70,70)
        working=[dict(p) for p in self.config.get("ports",[])]
        tree=ttk.Treeview(dlg,columns=("id","name","host","port"),show="headings",height=8)
        for c,t,w in (("id","Port",55),("name","Name",120),("host","Host",180),("port","TCP",70)):
            tree.heading(c,text=t); tree.column(c,width=w,anchor="w")
        tree.grid(row=0,column=0,columnspan=4,padx=8,pady=8)
        def redraw():
            tree.delete(*tree.get_children())
            for i,p in enumerate(working):
                tree.insert("","end",iid=str(i),values=(i,p["name"],p["host"],p["port"]))
        def editor(index=None):
            old=working[index] if index is not None else None
            w=tk.Toplevel(dlg); w.title("Edit Port" if old else "Add Port"); w.transient(dlg); w.grab_set(); w.resizable(False,False)
            self.place_child_window(w,110,110)
            vals=dict(old or {})
            nv=tk.StringVar(value=vals.get("name",f"Port {len(working)+1}")); hv=tk.StringVar(value=vals.get("host","127.0.0.1")); pv=tk.StringVar(value=str(vals.get("port",8001)))
            for r,(lab,var) in enumerate((("Name:",nv),("Host:",hv),("TCP port:",pv))):
                ttk.Label(w,text=lab).grid(row=r,column=0,sticky="e",padx=8,pady=4)
                ttk.Entry(w,textvariable=var,width=28).grid(row=r,column=1,padx=8,pady=4)
            def commit():
                name=nv.get().strip(); host=hv.get().strip()
                try: tp=int(pv.get().strip())
                except ValueError:
                    messagebox.showwarning("Port","TCP port must be numeric.",parent=w); return
                if not name or not host or not 1 <= tp <= 65535:
                    messagebox.showwarning("Port","Check ID, name, host and port.",parent=w); return
                np={"id":str(index if index is not None else len(working)),"name":name,"type":"kiss_tcp","host":host,"port":tp}
                if index is None: working.append(np)
                else: working[index]=np
                redraw(); w.destroy()
            ttk.Button(w,text="Save",command=commit).grid(row=3,column=0,pady=10)
            ttk.Button(w,text="Cancel",command=w.destroy).grid(row=3,column=1,pady=10)
        def selidx():
            sel=tree.selection()
            return int(sel[0]) if sel else None
        ttk.Button(dlg,text="Add",command=lambda:editor(None)).grid(row=1,column=0,pady=4)
        ttk.Button(dlg,text="Edit",command=lambda:editor(selidx()) if selidx() is not None else None).grid(row=1,column=1,pady=4)
        def delete():
            i=selidx()
            if i is None:return
            working.pop(i)
            redraw()
        ttk.Button(dlg,text="Delete",command=delete).grid(row=1,column=2,pady=4)
        def saveall():
            if first_run and not working:
                messagebox.showwarning(
                    "Ports",
                    "Add at least one KISS port for first setup.",
                    parent=dlg,
                )
                return

            for idx, port in enumerate(working):
                port["id"] = str(idx)

            self.config["ports"] = working
            valid = {p["id"] for p in working}

            if valid:
                if (
                    self.config.get("default_port_id")
                    not in valid
                ):
                    self.config["default_port_id"] = (
                        working[0]["id"]
                    )
            else:
                self.config["default_port_id"] = ""

            self.beacon_port_ids = [
                x
                for x in self.beacon_port_ids
                if x in valid
            ]

            if (
                not self.beacon_port_ids
                and self.config["default_port_id"]
            ):
                self.beacon_port_ids = [
                    self.config["default_port_id"]
                ]

            self.config["beacon_port_ids"] = list(
                self.beacon_port_ids
            )
            self.save_config()

            # Drop stale visual state for logical ports; the restarted
            # backend will immediately repopulate it with the new list.
            self.kiss_port_states = {
                str(i): False
                for i, _p in enumerate(working)
            }
            self.kiss_port_status = {
                str(i): "disconnected"
                for i, _p in enumerate(working)
            }
            self.refresh_port_statusbar()
            dlg.destroy()

            if first_run:
                self.root.after(
                    100,
                    self.connect_api,
                )
            else:
                self.root.after(
                    100,
                    self.restart_owned_backend_after_port_change,
                )
        ttk.Label(
            dlg,
            text="Saving ports restarts a backend started by this GUI."
        ).grid(row=2,column=0,columnspan=4,pady=4)
        ttk.Button(dlg,text="Save configuration",command=saveall).grid(row=3,column=0,columnspan=2,pady=8)
        ttk.Button(dlg,text="Cancel",command=dlg.destroy).grid(row=3,column=2,columnspan=2,pady=8)
        tree.bind("<Double-1>",lambda e: editor(selidx()) if selidx() is not None else None)
        redraw()

    # ------------------------------------------------------------------
    # Backend/API
    # ------------------------------------------------------------------

    def backend_path(self):
        return Path(__file__).resolve().with_name(BACKEND_SCRIPT)

    def start_backend_if_needed(self):
        """
        Start the backend from the same directory as this GUI if the API
        is not already reachable.
        """
        backend = self.backend_path()

        if not backend.exists():
            raise FileNotFoundError(
                f"Backend not found:\n{backend}\n\n"
                f"Place {BACKEND_SCRIPT} next to this GUI script."
            )

        if self.backend_process is not None:
            if self.backend_process.poll() is None:
                return
            self.backend_process = None

        creationflags = 0
        startupinfo = None

        # On Windows do not open an additional console window.
        if sys.platform.startswith("win"):
            creationflags = getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0,
            )

        self.backend_process = subprocess.Popen(
            [
                sys.executable,
                str(backend),
            ],
            cwd=str(backend.parent),
            creationflags=creationflags,
            startupinfo=startupinfo,
        )

    @staticmethod
    def probe_backend_status(host, port, timeout=1.0):
        """
        Query an already running native backend without attaching the GUI.
        Returns the backend status dict or None.
        """
        sock = None
        try:
            sock = socket.create_connection(
                (host, port),
                timeout=timeout,
            )
            sock.settimeout(timeout)
            raw = (
                json.dumps(
                    {"cmd": "status", "id": "gui-probe"},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            sock.sendall(raw)

            buf = bytearray()
            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)

                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    if not line.strip():
                        continue

                    msg = json.loads(
                        line.decode("utf-8")
                    )
                    if (
                        msg.get("event") == "reply"
                        and msg.get("id") == "gui-probe"
                        and msg.get("ok")
                    ):
                        status = msg.get("status")
                        return (
                            status
                            if isinstance(status, dict)
                            else None
                        )
        except Exception:
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        return None

    def backend_ports_match_config(self, status):
        backend_ports = status.get("ports", []) or []
        gui_ports = self.config.get("ports", []) or []

        def normalized(items):
            out = []
            for idx, p in enumerate(items):
                try:
                    tcp_port = int(p.get("port", 0))
                except (TypeError, ValueError):
                    tcp_port = 0

                out.append((
                    str(p.get("id", idx)),
                    str(p.get("name", "")),
                    str(p.get("host", "")).strip(),
                    tcp_port,
                ))
            return out

        return normalized(backend_ports) == normalized(gui_ports)

    def format_backend_port_mismatch(self, status):
        backend_ports = status.get("ports", []) or []
        gui_ports = self.config.get("ports", []) or []

        def lines(items):
            if not items:
                return ["  (none)"]
            result = []
            for idx, p in enumerate(items):
                result.append(
                    "  P{} {} {}:{}".format(
                        p.get("id", idx),
                        p.get("name", ""),
                        p.get("host", ""),
                        p.get("port", ""),
                    )
                )
            return result

        return (
            "A backend is already running on "
            f"{API_HOST}:{API_PORT}, but it was started with "
            "a different KISS port configuration.\n\n"
            "Running backend:\n"
            + "\n".join(lines(backend_ports))
            + "\n\nGUI configuration:\n"
            + "\n".join(lines(gui_ports))
            + "\n\nStop the old backend process and connect again."
        )

    @staticmethod
    def api_port_open(host, port, timeout=0.25):
        try:
            with socket.create_connection(
                (host, port),
                timeout=timeout,
            ):
                return True
        except OSError:
            return False

    def restart_owned_backend_after_port_change(self):
        """
        Port definitions are loaded by the backend at process startup.
        If this GUI owns the backend process, restart it automatically after
        saving port changes so PORTS/PORTS_BY_ID and KISS worker threads are
        rebuilt from the new JSON configuration.
        """
        owned_running = (
            self.backend_process is not None
            and self.backend_process.poll() is None
        )

        if not owned_running:
            # An API may still be connected to an externally started backend.
            # Never terminate a process we do not own.
            if self.api.connected or self.api_port_open(
                API_HOST,
                API_PORT,
            ):
                messagebox.showinfo(
                    "Ports saved",
                    "Port configuration was saved.\n\n"
                    "The currently running backend was not started by this "
                    "GUI, so it was left untouched. Restart that backend "
                    "manually to load the new port list.",
                    parent=self.root,
                )
                return

            # No backend is running: normal connect will start one with the
            # newly saved configuration.
            self.root.after(
                100,
                self.connect_api,
            )
            return

        self.api_state_var.set("RESTARTING BACKEND")
        self.root.update_idletasks()

        # Close the GUI API connection first.
        self.api.close()
        self.kiss_port_states.clear()
        self.kiss_port_status.clear()
        self.refresh_port_statusbar()

        proc = self.backend_process
        self.backend_process = None

        try:
            proc.terminate()
            proc.wait(timeout=3.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass

        # Give the old API listener a moment to release 127.0.0.1:8010.
        def reconnect():
            self.api_state_var.set("OFF")
            self.connect_api()

        self.root.after(
            350,
            reconnect,
        )

    def connect_api(self):
        if self.api.connected:
            return

        try:
            self.api_state_var.set("CONNECTING")
            self.root.update_idletasks()

            # If something already owns the API port, verify that it is
            # actually a backend started with the same KISS configuration.
            # Never silently attach to a stale process with another port list.
            api_already_open = self.api_port_open(
                API_HOST,
                API_PORT,
            )

            if api_already_open:
                status = self.probe_backend_status(
                    API_HOST,
                    API_PORT,
                )
                if status is None:
                    raise ConnectionError(
                        f"Port {API_HOST}:{API_PORT} is occupied, "
                        "but the process did not answer as a PyPacket backend."
                    )

                if not self.backend_ports_match_config(status):
                    raise ConnectionError(
                        self.format_backend_port_mismatch(status)
                    )

            else:
                self.api_state_var.set("STARTING BACKEND")
                self.root.update_idletasks()

                self.start_backend_if_needed()

                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    if self.api_port_open(API_HOST, API_PORT):
                        break
                    self.root.update_idletasks()
                    time.sleep(0.10)
                else:
                    raise ConnectionError(
                        "Backend was started but API port "
                        f"{API_HOST}:{API_PORT} did not become ready."
                    )

                # The listener appeared: verify that the process loaded
                # exactly the ports saved by this GUI.
                status = self.probe_backend_status(
                    API_HOST,
                    API_PORT,
                )
                if (
                    status is None
                    or not self.backend_ports_match_config(status)
                ):
                    raise ConnectionError(
                        "Backend started, but its loaded KISS port list "
                        "does not match the GUI configuration."
                    )

            self.api.connect(
                API_HOST,
                API_PORT,
            )

            self.api_state_var.set("ON")

            local = norm_call(
                self.local_var.get()
            )
            if local:
                self.api.send({
                    "cmd": "register",
                    "callsigns": [local],
                })

            self.api.send({"cmd": "status"})
            self.api.send({"cmd": "sessions"})
            self.api.send({"cmd": "monitor_on"})
            self.api.send({
                "cmd": "station_config",
                "station_callsign": local,
                "digi_callsign": norm_call(
                    self.config.get("digi_callsign", local)
                ),
                "station_info": self.config.get("station_info", ""),
                "welcome_text": self.config.get("welcome_text", ""),
                "bye_text": self.config.get("bye_text", ""),
                "beacon_dest": self.config.get("beacon_dest", "CQ"),
                "beacon_via": self.config.get("beacon_via", ""),
                "beacon_text": self.config.get("beacon_text", ""),
                "beacon_port_ids": list(
                    self.config.get("beacon_port_ids", [])
                ),
                "beacon_interval_min": int(
                    self.config.get("beacon_interval_min", 0)
                ),
            })
            self.api.send({
                "cmd": "digi_set",
                "enabled": bool(self.digi_enabled_var.get()),
                "station_callsign": local,
                "digi_callsign": norm_call(
                    self.config.get("digi_callsign", local)
                ),
            })

        except Exception as exc:
            self.api_state_var.set("OFF")
            messagebox.showerror(
                "Backend",
                str(exc),
            )

    def disconnect_api(self):
        """
        Disconnect GUI from the native API and stop the backend process
        when it was started by this terminal.

        An externally started backend is left running because this GUI does
        not own that process.
        """
        self.api.close()
        self.api_state_var.set("OFF")
        self.kiss_state_var.set("TNC: ?")

        if self.backend_process is not None:
            if self.backend_process.poll() is None:
                try:
                    self.backend_process.terminate()
                    self.backend_process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        self.backend_process.kill()
                    except OSError:
                        pass
                except OSError:
                    pass

            self.backend_process = None

    def ensure_api(self):
        if self.api.connected:
            return True

        self.connect_api()
        return self.api.connected

    def toggle_digi(self):
        enabled = bool(self.digi_enabled_var.get())
        self.config["digi_enabled"] = enabled
        self.save_config()

        if not self.ensure_api():
            return

        try:
            self.api.send({
                "cmd": "digi_set",
                "enabled": enabled,
                "station_callsign": norm_call(self.local_var.get()),
                "digi_callsign": norm_call(self.config.get("digi_callsign", self.local_var.get())),
            })
            self.append_monitor(
                f"[DIGI] {'ON' if enabled else 'OFF'} "
                f"{norm_call(self.config.get('digi_callsign', self.local_var.get()))}\n"
            )
        except Exception as exc:
            messagebox.showerror("DIGI", str(exc))

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def session_key(self, local, remote, port_id=None):
        return (str(port_id or self.config.get("default_port_id","0")), norm_call(local), norm_call(remote))

    def get_session(self, local, remote, create=False, via=None, port_id=None, port_name=None):
        port_id=str(port_id or self.config.get("default_port_id","0"))
        key = self.session_key(local, remote, port_id)

        session = self.sessions.get(key)

        if session is None and create:
            session = TerminalSession(local, remote, via=via, port_id=port_id, port_name=port_name or self.port_name(port_id))
            self.sessions[key] = session

            if key not in self.session_order:
                self.session_order.append(key)

            self.refresh_slots()

        return session

    def connect_dialog(self):
        if not self.ensure_api(): return
        dlg=tk.Toplevel(self.root); dlg.title("Connect"); dlg.transient(self.root); dlg.grab_set(); dlg.resizable(False,False); self.place_child_window(dlg,70,70)
        ports=self.config.get("ports",[])
        labels={f"{p['name']} [{p['id']}]":p["id"] for p in ports}; rev={v:k for k,v in labels.items()}
        lv=tk.StringVar(value=norm_call(self.local_var.get())); rv=tk.StringVar(); vv=tk.StringVar(); pv=tk.StringVar(value=rev.get(self.config.get("default_port_id"),next(iter(labels))))
        for r,(lab,var) in enumerate((("Local callsign:",lv),("Remote:",rv),("Via:",vv))):
            ttk.Label(dlg,text=lab).grid(row=r,column=0,sticky="e",padx=8,pady=4); ent=ttk.Entry(dlg,textvariable=var,width=24); ent.grid(row=r,column=1,padx=8,pady=4)
            if r==1: remote_entry=ent
        ttk.Label(dlg,text="Port:").grid(row=3,column=0,sticky="e",padx=8,pady=4)
        ttk.Combobox(dlg,textvariable=pv,values=list(labels),state="readonly",width=22).grid(row=3,column=1,padx=8,pady=4)
        def go():
            local=norm_call(lv.get()); remote=norm_call(rv.get()); via=parse_via(vv.get()); pid=labels.get(pv.get())
            if not local or not remote or not pid:return
            self.local_var.set(local); self.config["default_port_id"]=pid; self.save_config()
            self.api.send({"cmd":"register","callsigns":[local]})
            self.api.send({"cmd":"connect","port_id":pid,"local":local,"remote":remote,"via":via})
            ses=self.get_session(local,remote,True,via,pid,self.port_name(pid)); ses.state="AWAIT_UA"; self.active_key=self.session_key(local,remote,pid)
            self.append_terminal(f"*** Connecting to station {remote} on {ses.port_name}"+(f" via {','.join(via)}" if via else "")+"\n","sys")
            self.refresh_slots(); self.refresh_active_session(); dlg.destroy()
        ttk.Button(dlg,text="Connect",command=go).grid(row=4,column=0,columnspan=2,pady=10); remote_entry.focus_set(); dlg.bind("<Return>",lambda e:go())

    def disconnect_current(self):
        if not self.active_key:
            return

        session = self.sessions.get(
            self.active_key
        )
        if not session:
            return

        if not self.ensure_api():
            return

        try:
            self.api.send({
                "cmd": "disconnect",
                "port_id": session.port_id,
                "local": session.local,
                "remote": session.remote,
            })
        except Exception as exc:
            messagebox.showerror(
                "Disconnect",
                str(exc),
            )

    def select_slot(self, idx):
        if idx >= len(self.session_order):
            return

        key = self.session_order[idx]
        if key not in self.sessions:
            return

        self.active_key = key
        self.refresh_slots()
        self.refresh_active_session()
        self.input_entry.focus_set()

    def reconnect_slot(self, idx):
        """
        Double-clicking a disconnected session slot reconnects using the
        callsigns and VIA route remembered in that session.
        """
        if idx >= len(self.session_order):
            return

        key = self.session_order[idx]
        session = self.sessions.get(key)
        if session is None:
            return

        self.active_key = key

        if session.state == "CONNECTED":
            self.refresh_slots()
            self.refresh_active_session()
            return

        if session.state in (
            "AWAIT_UA",
            "AWAIT_DISC_UA",
            "CONNECTING",
        ):
            return

        if not self.ensure_api():
            return

        try:
            self.api.send({
                "cmd": "register",
                "callsigns": [session.local],
            })
            self.api.send({
                "cmd": "connect",
                "port_id": session.port_id,
                "local": session.local,
                "remote": session.remote,
                "via": list(session.via),
            })

            session.state = "AWAIT_UA"

            self._append_to_session(
                session,
                f"*** Reconnecting to station {session.remote}"
                + (
                    f" via {','.join(session.via)}"
                    if session.via
                    else ""
                )
                + "\n",
                "sys",
            )

            self.refresh_slots()
            self.refresh_active_session()

        except Exception as exc:
            messagebox.showerror(
                "Reconnect",
                str(exc),
            )

    @staticmethod
    def slot_state_colors(state):
        if state == "CONNECTED":
            return "#b7e4b8", "#8fd492"

        if state in (
            "AWAIT_UA",
            "AWAIT_DISC_UA",
            "CONNECTING",
        ):
            return "#ffe69a", "#ffd45c"

        if state == "DISCONNECTED":
            return "#d9d9d9", "#c8c8c8"

        return "#e8e8e8", "#dcdcdc"

    def refresh_slots(self):
        sessions = [
            self.sessions[key]
            for key in self.session_order
            if key in self.sessions
        ]

        for idx, btn in enumerate(self.slot_buttons):
            if idx >= len(sessions):
                btn.configure(
                    text="...",
                    bg="#ececec",
                    fg="#303030",
                    activebackground="#e2e2e2",
                    activeforeground="#202020",
                    relief="raised",
                    bd=1,
                    font=self.session_font,
                    highlightthickness=0,
                )
                continue

            session = sessions[idx]
            key = self.session_key(
                session.local,
                session.remote,
                session.port_id,
            )
            active = key == self.active_key
            state = str(session.state).upper()

            if state == "CONNECTED":
                state_icon = "●"
            elif state in (
                "AWAIT_UA",
                "CONNECTING",
                "AWAIT_DISC_UA",
                "DISCONNECTING",
            ):
                state_icon = "◐"
            else:
                state_icon = "○"

            focus_icon = "▶ " if active else ""
            text = (
                f"{focus_icon}{state_icon} "
                f"P{session.port_id} {session.remote}"
            )

            btn.configure(
                text=text,
                bg="#ececec",
                fg="#202020",
                activebackground="#e2e2e2",
                activeforeground="#202020",
                relief="sunken" if active else "raised",
                bd=1,
                font=self.session_font,
                highlightthickness=0,
            )

    def refresh_active_session(self):
        self.terminal.delete("1.0", "end")

        if not self.active_key:
            self.session_state_var.set(
                "Not connected"
            )
            return

        session = self.sessions.get(
            self.active_key
        )
        if not session:
            return

        if session.buffer:
            self.terminal.insert(
                "end",
                session.buffer,
                "rx",
            )
            self.terminal.see("end")

        via_text = (
            f" via {','.join(session.via)}"
            if session.via
            else ""
        )

        self.session_state_var.set(
            f"{session.remote} {session.state} [{session.port_name}]{via_text}"
        )

    # ------------------------------------------------------------------
    # Terminal I/O
    # ------------------------------------------------------------------

    def _send_from_entry(self, _event=None):
        self.send_current()
        return "break"

    def send_current(self):
        text = self.input_var.get()

        if not text:
            return

        if not self.active_key:
            messagebox.showwarning(
                "Terminal",
                "No active session."
            )
            return

        session = self.sessions.get(
            self.active_key
        )
        if not session:
            return

        if not self.ensure_api():
            return

        payload = text + "\r"

        try:
            self.api.send({
                "cmd": "send",
                "port_id": session.port_id,
                "local": session.local,
                "remote": session.remote,
                "data": payload,
            })

            display = f":>{text}\n"
            self._append_to_session(
                session,
                display,
                "tx",
            )

            self.input_var.set("")

        except Exception as exc:
            messagebox.showerror(
                "Send",
                str(exc),
            )

    def _append_to_session(
        self,
        session,
        text,
        tag,
    ):
        session.buffer += text

        if len(session.buffer) > 250000:
            session.buffer = session.buffer[-200000:]

        key = self.session_key(
            session.local,
            session.remote,
            session.port_id,
        )

        if key == self.active_key:
            self.append_terminal(
                text,
                tag,
            )

    def append_terminal(self, text, tag=None):
        self.terminal.insert(
            "end",
            text,
            tag or "rx",
        )
        self.terminal.see("end")

    def clear_terminal(self):
        self.terminal.delete(
            "1.0",
            "end",
        )

        if self.active_key in self.sessions:
            self.sessions[
                self.active_key
            ].buffer = ""

    def clear_current_session(self):
        """
        Clear the visible terminal and its saved scrollback.
        If the selected session is not connected anymore, also remove the
        remembered session from its numbered slot.
        """
        self.clear_terminal()

        if not self.active_key:
            return

        key = self.active_key
        session = self.sessions.get(key)

        if session is None:
            return

        if session.state == "CONNECTED":
            return

        # A session that is still negotiating is not considered removable.
        if session.state in (
            "AWAIT_UA",
            "AWAIT_DISC_UA",
            "CONNECTING",
        ):
            return

        self.sessions.pop(key, None)

        if key in self.session_order:
            self.session_order.remove(key)

        if self.session_order:
            self.active_key = self.session_order[0]
        else:
            self.active_key = None

        self.refresh_slots()
        self.refresh_active_session()

    def own_callsigns(self):
        values = {
            norm_call(
                self.config.get(
                    "local",
                    DEFAULT_LOCAL
                )
            ),
            norm_call(
                self.config.get(
                    "digi_callsign",
                    self.config.get(
                        "local",
                        DEFAULT_LOCAL
                    )
                )
            ),
        }
        return {x for x in values if x}

    def is_own_callsign(self, call):
        return norm_call(call) in self.own_callsigns()

    def sanitize_mheard_via(self, via):
        own = self.own_callsigns()
        result = []

        if isinstance(via, str):
            values = [
                x.strip()
                for x in via.split(",")
                if x.strip()
            ]
        else:
            values = list(via or [])

        for item in values:
            item = norm_call(str(item))
            if not item:
                continue
            if item in own:
                continue
            result.append(item)

        return result

    # ------------------------------------------------------------------
    # MHEARD
    # ------------------------------------------------------------------

    def serialize_mheard(self):
        data = {}

        for key, info in self.mheard.items():
            last_dt = info.get("last_dt")

            if isinstance(last_dt, datetime):
                last_iso = last_dt.isoformat(timespec="seconds")
            else:
                last_iso = ""

            route_last_dt = info.get("route_last_dt")
            if isinstance(route_last_dt, datetime):
                route_last_iso = route_last_dt.isoformat(
                    timespec="seconds"
                )
            else:
                route_last_iso = ""

            data[key] = {
                "call": info.get("call", str(key).split("|")[-1]),
                "last": info.get("last", ""),
                "last_iso": last_iso,
                "direction": info.get("direction", ""),
                "port_id": info.get("port_id", ""),
                "port_name": info.get("port_name", ""),
                "via": info.get("via", ""),
                "route_kind": info.get(
                    "route_kind",
                    "via" if info.get("via") else "direct",
                ),
                "route_last_iso": route_last_iso,
                "type": info.get("type", ""),
            }

        return data

    def restore_mheard_from_config(self):
        raw = self.config.get("mheard", {})
        if not isinstance(raw, dict):
            return

        for key, info in raw.items():
            if not isinstance(info, dict):
                continue

            call = norm_call(
                str(
                    info.get(
                        "call",
                        str(key).split("|")[-1]
                    )
                )
            )
            if not call:
                continue

            # Purge our own callsign and historical TX records from MH.
            if self.is_own_callsign(call):
                continue

            if str(info.get("direction", "")).upper() != "RX":
                continue

            port_id=str(info.get("port_id",str(key).split("|")[0] if "|" in str(key) else self.config.get("default_port_id","0")))
            mh_key=f"{port_id}|{call}"

            last_iso = str(info.get("last_iso", "")).strip()

            try:
                last_dt = (
                    datetime.fromisoformat(last_iso)
                    if last_iso
                    else datetime.min
                )
            except ValueError:
                last_dt = datetime.min

            clean_via = ",".join(
                self.sanitize_mheard_via(
                    info.get("via", "")
                )
            )
            route_kind = str(
                info.get(
                    "route_kind",
                    "via" if clean_via else "direct"
                )
            ).lower()

            route_last_iso = str(
                info.get("route_last_iso", "")
            ).strip()
            try:
                route_last_dt = (
                    datetime.fromisoformat(route_last_iso)
                    if route_last_iso
                    else last_dt
                )
            except ValueError:
                route_last_dt = last_dt

            self.mheard[mh_key] = {
                "call": call,
                "port_id": port_id,
                "port_name": str(
                    info.get(
                        "port_name",
                        self.port_name(port_id)
                    )
                ),
                "last_dt": last_dt,
                "last": str(info.get("last", "")),
                "direction": str(info.get("direction", "")),
                "via": clean_via,
                "route_kind": route_kind,
                "route_last_dt": route_last_dt,
                "type": str(info.get("type", "")),
            }

    def update_mheard(
        self,
        call,
        direction,
        via,
        frame_type,
        port_id,
        port_name,
    ):
        call = norm_call(call)
        if not call:
            return

        if str(direction).upper() != "RX":
            return

        if self.is_own_callsign(call):
            return

        clean_via = self.sanitize_mheard_via(via)
        incoming_direct = not bool(clean_via)

        key = f"{port_id}|{call}"
        now = datetime.now()
        existing = self.mheard.get(key)

        route_ttl = int(
            self.config.get("digi_route_ttl", 3600)
        )

        # "Last" describes when we heard the station at all.
        # Route data is maintained separately so an indirect copy cannot
        # refresh/poison a previously learned direct path.
        if existing is None:
            self.mheard[key] = {
                "call": call,
                "port_id": port_id,
                "port_name": port_name,
                "last_dt": now,
                "last": now.strftime("%H:%M:%S"),
                "direction": direction,
                "via": "" if incoming_direct else ",".join(clean_via),
                "route_kind": (
                    "direct"
                    if incoming_direct
                    else "via"
                ),
                "route_last_dt": now,
                "type": frame_type,
            }
        else:
            existing["last_dt"] = now
            existing["last"] = now.strftime("%H:%M:%S")
            existing["direction"] = direction
            existing["type"] = frame_type

            if incoming_direct:
                # Direct always wins immediately.
                existing["port_id"] = port_id
                existing["port_name"] = port_name
                existing["via"] = ""
                existing["route_kind"] = "direct"
                existing["route_last_dt"] = now
            else:
                route_kind = existing.get(
                    "route_kind",
                    "via" if existing.get("via") else "direct",
                )
                route_last_dt = existing.get(
                    "route_last_dt",
                    existing.get("last_dt", datetime.min),
                )

                try:
                    direct_age = (
                        now - route_last_dt
                    ).total_seconds()
                except Exception:
                    direct_age = route_ttl + 1

                # A fresh direct route is authoritative. The station may also
                # be heard in a repeated copy through our/local digi; that
                # must not change the displayed route or CONNECT VIA choice.
                if (
                    route_kind == "direct"
                    and direct_age <= route_ttl
                ):
                    pass
                else:
                    existing["port_id"] = port_id
                    existing["port_name"] = port_name
                    existing["via"] = ",".join(clean_via)
                    existing["route_kind"] = "via"
                    existing["route_last_dt"] = now

        self.refresh_mheard()
        self.save_config()

    def refresh_mheard(self):
        for item in self.mh_tree.get_children():
            self.mh_tree.delete(item)

        rows = sorted(
            self.mheard.items(),
            key=lambda kv: kv[1]["last_dt"],
            reverse=True,
        )

        for key, info in rows:
            port_id = str(info.get("port_id", ""))
            port_name = info.get(
                "port_name",
                self.port_name(port_id)
            )
            self.mh_tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    info.get("call", str(key).split("|")[-1]),
                    info["last"],
                    f"{port_id} {port_name}",
                    info["via"],
                ),
            )

    def clear_mheard(self):
        self.mheard.clear()
        self.refresh_mheard()
        self.save_config()

    def focus_mheard(self):
        self.mh_tree.focus_set()

    def connect_from_mheard(self, _event=None):
        selected = self.mh_tree.selection()
        if not selected and _event is not None:
            item = self.mh_tree.identify_row(_event.y)
            if item:
                selected = (item,)

        if not selected:
            return

        info = self.mheard.get(selected[0])
        if not info:
            return

        local = norm_call(self.local_var.get())
        remote = norm_call(
            info.get(
                "call",
                selected[0].split("|")[-1],
            )
        )
        if not local or not remote:
            return

        port_id = str(
            info.get(
                "port_id",
                self.config.get("default_port_id", "0"),
            )
        )
        port_name = info.get(
            "port_name",
            self.port_name(port_id),
        )

        stored_via = info.get("via", "")
        if isinstance(stored_via, (list, tuple)):
            via = [
                norm_call(str(x))
                for x in stored_via
                if norm_call(str(x))
            ]
        else:
            via = parse_via(str(stored_via))

        # Remove only our exact local/DIGI identities if old MH data
        # contains them.
        own = self.own_callsigns()
        via = [
            x
            for x in via
            if x not in own
        ]

        key = self.session_key(
            local,
            remote,
            port_id,
        )
        session = self.get_session(
            local,
            remote,
            True,
            via,
            port_id,
            port_name,
        )

        # Update route even for an already existing disconnected slot.
        session.via = list(via)
        session.port_id = port_id
        session.port_name = port_name

        self.active_key = key
        self.refresh_slots()
        self.refresh_active_session()

        if session.state == "CONNECTED":
            return

        if session.state in (
            "AWAIT_UA",
            "AWAIT_DISC_UA",
        ):
            return

        if not self.ensure_api():
            return

        self.config["default_port_id"] = port_id
        self.save_config()

        self.api.send({
            "cmd": "register",
            "callsigns": [local],
        })
        self.api.send({
            "cmd": "connect",
            "port_id": port_id,
            "local": local,
            "remote": remote,
            "via": via,
        })

        session.state = "AWAIT_UA"

        self.append_terminal(
            f"*** Connecting to station {remote} "
            f"on {port_name}"
            + (
                f" via {','.join(via)}"
                if via
                else ""
            )
            + "\n",
            "sys",
        )
        self.refresh_slots()
        self.refresh_active_session()


    def append_monitor(self, text, tag=None):
        self.monitor.insert(
            "end",
            text,
            tag or "rx",
        )
        self.monitor.see("end")

    def clear_monitor(self):
        self.monitor.delete(
            "1.0",
            "end",
        )

    def toggle_monitor(self):
        if self.monitor_visible:
            self.monitor_frame.grid_remove()
            self.monitor_visible = False
        else:
            self.monitor_frame.grid()
            self.monitor_visible = True

    # ------------------------------------------------------------------
    # Beacon/mailbox placeholders
    # ------------------------------------------------------------------

    def station_config_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Station configuration")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        self.place_child_window(
            dlg,
            x_offset=90,
            y_offset=70,
        )

        callsign_var = tk.StringVar(
            value=norm_call(
                self.config.get("local", DEFAULT_LOCAL)
            )
        )
        digi_var = tk.StringVar(
            value=norm_call(
                self.config.get(
                    "digi_callsign",
                    callsign_var.get()
                )
            )
        )
        beacon_dest_var = tk.StringVar(
            value=self.beacon_dest
        )
        beacon_via_var = tk.StringVar(
            value=self.beacon_via
        )
        beacon_text_var = tk.StringVar(
            value=self.beacon_text
        )
        beacon_interval_var = tk.StringVar(
            value=str(
                self.config.get("beacon_interval_min", 0)
            )
        )
        welcome_var = tk.StringVar(
            value=self.config.get("welcome_text", "")
        )
        bye_var = tk.StringVar(
            value=self.config.get("bye_text", "")
        )

        row = 0

        ttk.Label(
            dlg,
            text="Callsign:"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=(10, 4),
        )
        callsign_entry = ttk.Entry(
            dlg,
            textvariable=callsign_var,
            width=38,
        )
        callsign_entry.grid(
            row=row,
            column=1,
            padx=8,
            pady=(10, 4),
        )
        row += 1

        ttk.Label(
            dlg,
            text="DIGI callsign:"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=4,
        )
        ttk.Entry(
            dlg,
            textvariable=digi_var,
            width=38,
        ).grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        row += 1

        ttk.Label(
            dlg,
            text="INFO:"
        ).grid(
            row=row,
            column=0,
            sticky="ne",
            padx=8,
            pady=4,
        )
        info_text = tk.Text(
            dlg,
            width=40,
            height=5,
            wrap="word",
        )
        info_text.grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        info_text.insert(
            "1.0",
            self.config.get("station_info", "")
        )
        row += 1

        ttk.Separator(
            dlg,
            orient="horizontal"
        ).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=8,
            pady=7,
        )
        row += 1

        ttk.Label(
            dlg,
            text="Beacon destination:"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=4,
        )
        ttk.Entry(
            dlg,
            textvariable=beacon_dest_var,
            width=38,
        ).grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        row += 1

        ttk.Label(
            dlg,
            text="Beacon VIA:"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=4,
        )
        ttk.Entry(
            dlg,
            textvariable=beacon_via_var,
            width=38,
        ).grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        row += 1

        ttk.Label(
            dlg,
            text="Beacon text:"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=4,
        )
        ttk.Entry(
            dlg,
            textvariable=beacon_text_var,
            width=38,
        ).grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        row += 1

        ttk.Label(
            dlg,
            text="Beacon interval (min):"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=4,
        )
        ttk.Entry(
            dlg,
            textvariable=beacon_interval_var,
            width=38,
        ).grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        row += 1

        ttk.Label(
            dlg,
            text="Beacon ports:"
        ).grid(
            row=row,
            column=0,
            sticky="ne",
            padx=8,
            pady=4,
        )

        port_list = tk.Listbox(
            dlg,
            selectmode="multiple",
            exportselection=False,
            height=max(
                3,
                min(
                    7,
                    len(self.config.get("ports", []))
                )
            ),
            width=38,
        )
        port_list.grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
            sticky="ew",
        )

        selected_ids = set(self.beacon_port_ids)
        for idx, port in enumerate(
            self.config.get("ports", [])
        ):
            port_list.insert(
                "end",
                f"P{idx}  "
                f"{port.get('name', f'Port {idx+1}')}  "
                f"{port.get('host')}:{port.get('port')}"
            )
            if str(idx) in selected_ids:
                port_list.selection_set(idx)

        row += 1

        ttk.Separator(
            dlg,
            orient="horizontal"
        ).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=8,
            pady=7,
        )
        row += 1

        ttk.Label(
            dlg,
            text="Welcome text:"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=4,
        )
        ttk.Entry(
            dlg,
            textvariable=welcome_var,
            width=38,
        ).grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        row += 1

        ttk.Label(
            dlg,
            text="Bye text:"
        ).grid(
            row=row,
            column=0,
            sticky="e",
            padx=8,
            pady=4,
        )
        ttk.Entry(
            dlg,
            textvariable=bye_var,
            width=38,
        ).grid(
            row=row,
            column=1,
            padx=8,
            pady=4,
        )
        row += 1

        def save():
            callsign = norm_call(callsign_var.get())
            digi_callsign = norm_call(digi_var.get())

            if not callsign:
                messagebox.showwarning(
                    "Station configuration",
                    "Callsign is required.",
                    parent=dlg,
                )
                return

            if not digi_callsign:
                digi_callsign = callsign

            destination = norm_call(
                beacon_dest_var.get()
            )
            if not destination:
                destination = "CQ"

            try:
                beacon_interval = int(
                    beacon_interval_var.get().strip() or "0"
                )
            except ValueError:
                messagebox.showwarning(
                    "Station configuration",
                    "Beacon interval must be a whole number of minutes.",
                    parent=dlg,
                )
                return

            if beacon_interval < 0:
                messagebox.showwarning(
                    "Station configuration",
                    "Beacon interval cannot be negative. Use 0 to disable periodic beacon.",
                    parent=dlg,
                )
                return

            indices = list(port_list.curselection())
            if not indices:
                messagebox.showwarning(
                    "Station configuration",
                    "Select at least one beacon port.",
                    parent=dlg,
                )
                return

            self.config["local"] = callsign
            self.config["digi_callsign"] = digi_callsign
            self.config["station_info"] = (
                info_text.get("1.0", "end-1c")
            )
            self.config["welcome_text"] = welcome_var.get()
            self.config["bye_text"] = bye_var.get()

            self.beacon_dest = destination
            self.beacon_via = beacon_via_var.get().strip()
            self.beacon_text = beacon_text_var.get()
            self.beacon_port_ids = [
                str(i)
                for i in indices
            ]

            self.config["beacon_dest"] = self.beacon_dest
            self.config["beacon_via"] = self.beacon_via
            self.config["beacon_text"] = self.beacon_text
            self.config["beacon_interval_min"] = beacon_interval
            self.config["beacon_port_ids"] = list(
                self.beacon_port_ids
            )

            self.local_var.set(callsign)
            self.save_config()

            # Remove any old own-call entries immediately.
            for key in list(self.mheard):
                info = self.mheard.get(key, {})
                if self.is_own_callsign(
                    info.get("call", "")
                ):
                    self.mheard.pop(key, None)

            self.refresh_mheard()
            self.save_config()

            if self.api.connected:
                try:
                    self.api.send({
                        "cmd": "station_config",
                        "station_callsign": callsign,
                        "digi_callsign": digi_callsign,
                        "station_info": self.config["station_info"],
                        "welcome_text": self.config["welcome_text"],
                        "bye_text": self.config["bye_text"],
                        "beacon_dest": self.beacon_dest,
                        "beacon_via": self.beacon_via,
                        "beacon_text": self.beacon_text,
                        "beacon_port_ids": list(self.beacon_port_ids),
                        "beacon_interval_min": beacon_interval,
                    })
                    self.api.send({
                        "cmd": "digi_set",
                        "enabled": bool(
                            self.digi_enabled_var.get()
                        ),
                        "station_callsign": callsign,
                        "digi_callsign": digi_callsign,
                    })
                except Exception:
                    pass

            dlg.destroy()

        buttons = ttk.Frame(dlg)
        buttons.grid(
            row=row,
            column=0,
            columnspan=2,
            pady=10,
        )

        ttk.Button(
            buttons,
            text="Save",
            command=save,
        ).pack(
            side="left",
            padx=4,
        )

        ttk.Button(
            buttons,
            text="Cancel",
            command=dlg.destroy,
        ).pack(
            side="left",
            padx=4,
        )

        callsign_entry.focus_set()

    def send_beacon(self):
        if not self.ensure_api():
            return

        if not self.beacon_dest:
            messagebox.showwarning(
                "Beacon",
                "Configure beacon first in Stations -> Configure..."
            )
            return

        local = norm_call(self.local_var.get())
        if not local:
            messagebox.showwarning(
                "Beacon",
                "Local callsign is empty."
            )
            return

        valid = {
            str(idx): port
            for idx, port in enumerate(
                self.config.get("ports", [])
            )
        }
        port_ids = [
            pid for pid in self.beacon_port_ids
            if pid in valid
        ]

        if not port_ids:
            messagebox.showwarning(
                "Beacon",
                "No beacon ports selected."
            )
            return

        try:
            for port_id in port_ids:
                self.api.send({
                    "cmd": "beacon",
                    "port_id": port_id,
                    "local": local,
                    "to": self.beacon_dest,
                    "via": parse_via(self.beacon_via),
                    "data": self.beacon_text,
                })

                self.append_monitor(
                    f"TX BEACON [P{port_id} "
                    f"{self.port_name(port_id)}] "
                    f"{local} -> {self.beacon_dest}"
                    + (
                        f" via {self.beacon_via}"
                        if self.beacon_via
                        else ""
                    )
                    + f" : {self.beacon_text}\n",
                    "tx",
                )

        except Exception as exc:
            messagebox.showerror(
                "Beacon",
                str(exc),
            )

    def mailbox_info(self):
        messagebox.showinfo(
            "Mailbox",
            "Mailbox GUI will be added in a later version.\n"
            "Incoming mailbox sessions are already handled by the backend."
        )

    # ------------------------------------------------------------------
    # Backend events
    # ------------------------------------------------------------------

    def _poll_events(self):
        while True:
            try:
                msg = self.events.get_nowait()
            except queue.Empty:
                break

            try:
                self.handle_event(msg)
            except Exception as exc:
                self.append_monitor(
                    f"[GUI ERROR] {exc}\n",
                    "tx",
                )

        self.root.after(
            50,
            self._poll_events,
        )

    def handle_event(self, msg):
        event = msg.get("event", "")

        if event == "_client_error":
            self.api_state_var.set("ERROR")
            self.append_monitor(
                f"[API ERROR] {msg.get('error', '')}\n",
                "tx",
            )
            return

        if event == "_client_disconnected":
            self.api_state_var.set("OFF")
            self.kiss_state_var.set("TNC: ?")
            self.kiss_port_states.clear()
            self.refresh_port_statusbar()
            return

        if event == "hello":
            self.api_state_var.set("ON")

            for p in msg.get("ports", []) or []:
                pid = str(p.get("id", ""))
                if not pid:
                    continue
                self.kiss_port_states[pid] = bool(
                    p.get("connected")
                )
                self.kiss_port_status[pid] = str(
                    p.get(
                        "state",
                        "connected"
                        if p.get("connected")
                        else "disconnected"
                    )
                )
            self.refresh_port_statusbar()

            if "digi_enabled" in msg:
                self.digi_enabled_var.set(
                    bool(msg.get("digi_enabled"))
                )

            self.kiss_state_var.set(
                "TNC: ON"
                if msg.get("kiss_connected")
                else "TNC: OFF"
            )

            for s in msg.get(
                "sessions",
                [],
            ):
                self.restore_session(s)

            return

        if event == "kiss_state":
            port_id = str(
                msg.get("port_id", "0")
            )
            connected_now = bool(
                msg.get("connected")
            )
            state = str(
                msg.get(
                    "state",
                    "connected"
                    if connected_now
                    else "disconnected"
                )
            )

            self.kiss_port_states[port_id] = connected_now
            self.kiss_port_status[port_id] = state

            connected = sum(
                1
                for v in self.kiss_port_states.values()
                if v
            )
            total = max(
                len(self.config.get("ports", [])),
                len(self.kiss_port_states),
            )
            self.kiss_state_var.set(
                f"TNC: {connected}/{total}"
            )
            self.refresh_port_statusbar()
            return

        if event == "connected":
            s = msg.get("session", {})

            local = s.get("local", "")
            remote = s.get("remote", "")
            via=s.get("via",[]); port_id=s.get("port_id",self.config.get("default_port_id","0"))
            session=self.get_session(local,remote,True,via,port_id,s.get("port_name",self.port_name(port_id)))
            session.state = "CONNECTED"
            session.via = list(via)

            self.active_key = self.session_key(local, remote, port_id)

            connection_label = (
                "*** Attached to existing station connection "
                if msg.get("resumed")
                else "*** Connected to station "
            )
            self._append_to_session(
                session,
                f"{connection_label}{remote}"
                + (
                    f" via {','.join(via)}"
                    if via
                    else ""
                )
                + "\n",
                "sys",
            )

            self.refresh_slots()
            self.refresh_slots()
            self.refresh_active_session()
            return

        if event == "disconnected":
            s = msg.get("session", {})
            local = s.get("local", "")
            remote = s.get("remote", "")
            via=s.get("via",[]); port_id=s.get("port_id",self.config.get("default_port_id","0"))
            session=self.get_session(local,remote,True,via,port_id,s.get("port_name",self.port_name(port_id)))
            session.state = "DISCONNECTED"

            self._append_to_session(
                session,
                f"*** Disconnected from station {remote}: "
                f"{msg.get('reason', '')}\n",
                "sys",
            )

            self.refresh_slots()
            self.refresh_active_session()
            return

        if event == "service_tx_data":
            local = msg.get("local", "")
            remote = msg.get("remote", "")
            via = msg.get("via", [])
            port_id = msg.get(
                "port_id",
                self.config.get("default_port_id", "0")
            )
            session = self.get_session(
                local,
                remote,
                True,
                via,
                port_id,
                msg.get(
                    "port_name",
                    self.port_name(port_id)
                ),
            )

            text = msg.get("data", "")
            text = (
                text
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )

            # Service output is TX over radio, but is shown locally without
            # the operator ':>' prefix because it is an automatic response.
            self._append_to_session(
                session,
                text,
                "tx",
            )
            return

        if event == "rx_data":
            local = msg.get("local", "")
            remote = msg.get("remote", "")
            via=msg.get("via",[]); port_id=msg.get("port_id",self.config.get("default_port_id","0"))
            session=self.get_session(local,remote,True,via,port_id,msg.get("port_name",self.port_name(port_id)))

            text = msg.get("data", "")
            text = (
                text
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )

            self._append_to_session(
                session,
                text,
                "rx",
            )
            return

        if event == "session_state":
            s = msg.get("session", {})
            local = s.get("local", "")
            remote = s.get("remote", "")

            if not local or not remote:
                return

            port_id=s.get("port_id",self.config.get("default_port_id","0"))
            session=self.get_session(local,remote,True,s.get("via",[]),port_id,s.get("port_name",self.port_name(port_id)))
            session.state = s.get(
                "state",
                session.state,
            )
            session.via = list(
                s.get("via", session.via)
            )

            self.refresh_slots()

            if self.session_key(local, remote, port_id) == self.active_key:
                self.refresh_active_session()
            return

        if event == "monitor":
            direction = msg.get(
                "direction",
                "?"
            ).upper()

            src = msg.get("src", "")
            dst = msg.get("dst", "")
            via = msg.get("via", [])
            via_repeated = msg.get(
                "via_repeated",
                via,
            )
            frame_type=msg.get("type","")
            port_id=msg.get("port_id",self.config.get("default_port_id","0")); port_name=msg.get("port_name",self.port_name(port_id))

            repeated_set = {
                norm_call(x)
                for x in via_repeated
            }
            via_display = [
                (
                    f"{norm_call(x)}*"
                    if norm_call(x) in repeated_set
                    else norm_call(x)
                )
                for x in via
            ]
            via_text = (
                f" via {','.join(via_display)}"
                if via_display
                else ""
            )

            extra = []

            if msg.get("ns") is not None:
                extra.append(
                    f"NS={msg['ns']}"
                )

            if msg.get("nr") is not None:
                extra.append(
                    f"NR={msg['nr']}"
                )

            if msg.get("pid") is not None:
                extra.append(
                    f"PID={msg['pid']:02X}"
                )

            extra_text = (
                " " + " ".join(extra)
                if extra
                else ""
            )

            line = (
                f"{direction:2} [{port_name}] "
                f"{src} -> {dst}"
                f"{via_text} "
                f"{frame_type}"
                f"{extra_text}\n"
            )

            self.append_monitor(
                line,
                "tx"
                if direction == "TX"
                else "rx",
            )

            if src:
                self.update_mheard(
                    src,
                    direction,
                    via_repeated,
                    frame_type,
                    port_id,
                    port_name,
                )

            return

        if event == "beacon_tx":
            port_id = str(msg.get("port_id", ""))
            if msg.get("status") == "sent":
                self.append_monitor(
                    f"TX BEACON AUTO [P{port_id} "
                    f"{msg.get('port_name', self.port_name(port_id))}] "
                    f"{msg.get('local', '')} -> "
                    f"{msg.get('to', 'CQ')} : "
                    f"{msg.get('data', '')}\n",
                    "tx",
                )
            else:
                self.append_monitor(
                    f"BEACON AUTO FAIL [P{port_id}] "
                    f"{msg.get('error', 'unknown error')}\n",
                    "err",
                )
            return

        if event == "digi_state":
            enabled = bool(msg.get("enabled", False))
            self.digi_enabled_var.set(enabled)
            self.config["digi_enabled"] = enabled
            self.save_config()
            return

        if event == "digi":
            action = msg.get("action", "")
            mode = msg.get("mode", "")

            if action == "repeat":
                prefix = (
                    "DIGI UI"
                    if mode == "ui_fanout"
                    else "DIGI"
                )
                self.append_monitor(
                    f"{prefix} P{msg.get('ingress_port')} -> "
                    f"P{msg.get('egress_port')}  "
                    f"{msg.get('src')} -> {msg.get('dst')}\n"
                )

            elif action == "drop":
                # Duplicate UI copies are expected with multi-port/proxy
                # reception, so keep them concise.
                prefix = (
                    "DIGI UI"
                    if mode == "ui_fanout"
                    or msg.get("reason") == "ui_duplicate"
                    else "DIGI"
                )
                self.append_monitor(
                    f"{prefix} DROP P{msg.get('ingress_port')}  "
                    f"{msg.get('src')} -> {msg.get('dst')}  "
                    f"({msg.get('reason')})\n"
                )
            return

        if event == "reply":
            if not msg.get("ok", False):
                self.append_monitor(
                    "[API ERROR] "
                    + msg.get(
                        "error",
                        "request failed",
                    )
                    + "\n",
                    "tx",
                )
            return

        if event == "error":
            self.append_monitor(
                "[BACKEND ERROR] "
                + msg.get("error", "")
                + "\n",
                "tx",
            )
            return

    def restore_session(self, s):
        local = s.get("local", "")
        remote = s.get("remote", "")

        if not local or not remote:
            return

        port_id=s.get("port_id",self.config.get("default_port_id","0"))
        session=self.get_session(local,remote,True,s.get("via",[]),port_id,s.get("port_name",self.port_name(port_id)))
        session.state = s.get(
            "state",
            "UNKNOWN",
        )

        if self.active_key is None:
            self.active_key = self.session_key(local, remote, port_id)

        self.refresh_slots()
        self.refresh_active_session()

    # ------------------------------------------------------------------

    def on_close(self):
        self.save_config()
        self.disconnect_api()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = PacketTerminalApp(root)

    if app.first_run or not app.config.get("ports"):
        # Do not start a backend which has nowhere to connect.
        root.after(
            250,
            lambda: app.configure_ports_dialog(
                first_run=True
            ),
        )
    else:
        # Existing configuration: attach/start backend normally.
        root.after(
            250,
            app.connect_api,
        )

    root.mainloop()


if __name__ == "__main__":
    main()
