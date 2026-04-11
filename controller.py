"""
controller.py — BLE GUI controller for mini_RC_OLED (ESP32)

Uses the Nordic UART Service (NUS) to send commands and receive responses.

Requirements:
    pip install bleak

Controls:
    Arrow keys / WASD : drive
    Space             : stop
    T                 : send STATUS
    Q / Escape        : quit
"""

import asyncio
import threading
import tkinter as tk
from tkinter import scrolledtext
from bleak import BleakClient, BleakScanner

DEVICE_NAME = "mini_RC_OLED"
RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # PC  → ESP32
TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # ESP32 → PC

KEY_CMD = {
    "Up":         "FORWARD",
    "w":          "FORWARD",
    "Down":       "BACK",
    "s":          "BACK",
    "Left":       "LEFT",
    "a":          "LEFT",
    "Right":      "RIGHT",
    "d":          "RIGHT",
    "space":      "STOP",
    "t":          "STATUS",
    "q":          "QUIT",
    "Escape":     "QUIT",
    "bracketleft":  "SPEED_DOWN",   # [
    "bracketright": "SPEED_UP",     # ]
}

# Highlight color for active d-pad buttons
ACTIVE_COLOR   = "#00C8FF"
INACTIVE_COLOR = "#2E2E2E"
CMD_BUTTON = {}   # maps command string → button widget


class ControllerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("mini_RC Controller")
        self.root.configure(bg="#1A1A1A")
        self.root.resizable(False, False)

        self.client: BleakClient | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self._build_ui()

        # Start the asyncio event loop in a background thread
        self.loop = asyncio.new_event_loop()
        self._bg_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._bg_thread.start()

        self.root.bind("<KeyPress>",   self._on_key)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        PAD = 10
        FONT_MONO = ("Consolas", 10)
        FONT_LABEL = ("Segoe UI", 9)
        FONT_STATUS = ("Segoe UI", 10, "bold")

        # ── top: status bar ───────────────────────────────────────────────────
        top = tk.Frame(self.root, bg="#1A1A1A")
        top.pack(fill=tk.X, padx=PAD, pady=(PAD, 0))

        self.status_var = tk.StringVar(value="Disconnected")
        tk.Label(top, text="Status:", bg="#1A1A1A", fg="#888888",
                 font=FONT_LABEL).pack(side=tk.LEFT)
        self.status_lbl = tk.Label(top, textvariable=self.status_var,
                                   bg="#1A1A1A", fg="#FF4444", font=FONT_STATUS)
        self.status_lbl.pack(side=tk.LEFT, padx=(4, 0))

        self.connect_btn = tk.Button(top, text="Connect",
                                     bg="#3A3A3A", fg="white",
                                     activebackground="#555555",
                                     activeforeground="white",
                                     relief=tk.FLAT, padx=10,
                                     font=FONT_LABEL,
                                     command=self._on_connect_click)
        self.connect_btn.pack(side=tk.RIGHT)

        # ── middle: log ───────────────────────────────────────────────────────
        self.log = scrolledtext.ScrolledText(
            self.root, height=14, width=52,
            bg="#111111", fg="#CCCCCC",
            insertbackground="white",
            font=FONT_MONO,
            state=tk.DISABLED,
            relief=tk.FLAT,
        )
        self.log.pack(padx=PAD, pady=PAD)
        self.log.tag_config("sent",  foreground="#00C8FF")
        self.log.tag_config("recv",  foreground="#88FF88")
        self.log.tag_config("info",  foreground="#AAAAAA")
        self.log.tag_config("error", foreground="#FF6666")

        # ── bottom: d-pad ─────────────────────────────────────────────────────
        dpad_frame = tk.Frame(self.root, bg="#1A1A1A")
        dpad_frame.pack(pady=(0, PAD))

        btn_cfg = dict(width=6, height=2, bg=INACTIVE_COLOR, fg="white",
                       activebackground=ACTIVE_COLOR, activeforeground="black",
                       relief=tk.FLAT, font=("Segoe UI", 9, "bold"))

        self._make_dpad_btn(dpad_frame, "▲\nFWD",  "FORWARD", 0, 1, btn_cfg)
        self._make_dpad_btn(dpad_frame, "◄\nLEFT", "LEFT",    1, 0, btn_cfg)
        self._make_dpad_btn(dpad_frame, "■\nSTOP", "STOP",    1, 1, btn_cfg)
        self._make_dpad_btn(dpad_frame, "►\nRIGHT","RIGHT",   1, 2, btn_cfg)
        self._make_dpad_btn(dpad_frame, "▼\nBACK", "BACK",    2, 1, btn_cfg)

        # STATUS button
        tk.Button(dpad_frame, text="STATUS", command=lambda: self._send("STATUS"),
                  bg="#3A3A3A", fg="white", activebackground="#555555",
                  activeforeground="white", relief=tk.FLAT,
                  font=("Segoe UI", 8), width=8,
                  ).grid(row=2, column=0, padx=4, pady=4)

        # ── speed slider ──────────────────────────────────────────────────────
        speed_frame = tk.Frame(self.root, bg="#1A1A1A")
        speed_frame.pack(fill=tk.X, padx=PAD, pady=(0, 4))

        tk.Label(speed_frame, text="Speed", bg="#1A1A1A", fg="#888888",
                 font=FONT_LABEL).pack(side=tk.LEFT)

        self.speed_var = tk.IntVar(value=60)
        self.speed_lbl = tk.Label(speed_frame, text="60%", width=5,
                                  bg="#1A1A1A", fg="#00C8FF", font=FONT_STATUS)
        self.speed_lbl.pack(side=tk.RIGHT)

        self.speed_slider = tk.Scale(
            speed_frame, from_=0, to=100,
            orient=tk.HORIZONTAL, variable=self.speed_var,
            bg="#1A1A1A", fg="#CCCCCC", troughcolor="#2E2E2E",
            activebackground=ACTIVE_COLOR, highlightthickness=0,
            showvalue=False, length=300,
            command=self._on_speed_change,
        )
        self.speed_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 4))

        # ── bottom hint ───────────────────────────────────────────────────────
        tk.Label(self.root, text="Arrow keys / WASD · Space=Stop · T=Status · [ ] Speed · Q/Esc=Quit",
                 bg="#1A1A1A", fg="#555555", font=("Segoe UI", 8)
                 ).pack(pady=(0, PAD))

    def _make_dpad_btn(self, parent, label, cmd, row, col, cfg):
        btn = tk.Button(parent, text=label, **cfg,
                        command=lambda c=cmd: self._send(c))
        btn.grid(row=row, column=col, padx=4, pady=4)
        CMD_BUTTON[cmd] = btn

    # ── logging ───────────────────────────────────────────────────────────────

    def _log(self, text: str, tag: str = "info"):
        def _insert():
            self.log.config(state=tk.NORMAL)
            self.log.insert(tk.END, text + "\n", tag)
            self.log.see(tk.END)
            self.log.config(state=tk.DISABLED)
        self.root.after(0, _insert)

    # ── BLE ───────────────────────────────────────────────────────────────────

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _notification_handler(self, _sender, data: bytearray):
        self._log(f"[ESP32] {data.decode(errors='replace')}", "recv")

    async def _connect(self):
        self._set_status("Scanning…", "#FFAA00")
        self._log(f"Scanning for '{DEVICE_NAME}' …", "info")
        try:
            device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10.0)
            if device is None:
                raise RuntimeError(f"'{DEVICE_NAME}' not found. Is it powered on?")
            self._log(f"Found: {device.name}  [{device.address}]", "info")
            self._set_status("Connecting…", "#FFAA00")
            self.client = BleakClient(device, disconnected_callback=self._on_ble_disconnect)
            await self.client.connect()
            await self.client.start_notify(TX_UUID, self._notification_handler)
            self._set_status("Connected", "#44FF88")
            self._log("Connected! Use keyboard or buttons to drive.", "info")
            # sync slider value to ESP32
            pct = self.speed_var.get()
            await self.client.write_gatt_char(RX_UUID, f"SPEED:{pct}\n".encode())
            self.root.after(0, lambda: self.connect_btn.config(text="Disconnect"))
            self.stop_event = asyncio.Event()
        except Exception as e:
            self._log(f"[ERROR] {e}", "error")
            self._set_status("Disconnected", "#FF4444")
            self.client = None

    async def _disconnect(self):
        if self.client and self.client.is_connected:
            try:
                await self.client.write_gatt_char(RX_UUID, b"STOP\n")
                await self.client.stop_notify(TX_UUID)
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self._set_status("Disconnected", "#FF4444")
        self._log("Disconnected.", "info")
        self.root.after(0, lambda: self.connect_btn.config(text="Connect"))

    def _on_ble_disconnect(self, __client):
        self._set_status("Disconnected", "#FF4444")
        self._log("BLE connection lost.", "error")
        self.root.after(0, lambda: self.connect_btn.config(text="Connect"))
        self.client = None

    def _on_speed_change(self, value):
        pct = int(value)
        self.speed_lbl.config(text=f"{pct}%")
        self._send(f"SPEED:{pct}")

    def _send(self, cmd: str):
        if self.client is None or not self.client.is_connected:
            self._log("Not connected.", "error")
            return
        self._log(f"[SENT] {cmd}", "sent")
        self._flash_button(cmd)
        asyncio.run_coroutine_threadsafe(
            self.client.write_gatt_char(RX_UUID, (cmd + "\n").encode()),
            self.loop,
        )

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _set_status(self, text: str, color: str):
        def _update():
            self.status_var.set(text)
            self.status_lbl.config(fg=color)
        self.root.after(0, _update)

    def _flash_button(self, cmd: str):
        btn = CMD_BUTTON.get(cmd)
        if btn is None:
            return
        btn.config(bg=ACTIVE_COLOR, fg="black")
        self.root.after(200, lambda: btn.config(bg=INACTIVE_COLOR, fg="white"))

    def _on_connect_click(self):
        if self.client and self.client.is_connected:
            asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
        else:
            asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    def _on_key(self, event: tk.Event):
        cmd = KEY_CMD.get(event.keysym) or KEY_CMD.get(event.char)
        if cmd is None:
            return
        if cmd == "QUIT":
            self._on_close()
            return
        if cmd == "SPEED_UP":
            self.speed_var.set(min(100, self.speed_var.get() + 5))
            self._on_speed_change(self.speed_var.get())
            return
        if cmd == "SPEED_DOWN":
            self.speed_var.set(max(0, self.speed_var.get() - 5))
            self._on_speed_change(self.speed_var.get())
            return
        self._send(cmd)

    def _on_close(self):
        if self.client and self.client.is_connected:
            asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)
        self.root.after(300, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    app = ControllerApp(root)
    root.mainloop()
