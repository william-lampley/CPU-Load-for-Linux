#!/usr/bin/env python3
"""CPU temperature monitor for Linux (GTK 3). Reads sensors from /sys/class/hwmon."""

import glob
import os
import subprocess
import sys
import time
from collections import deque

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

# Kernel drivers that report CPU temperature (AMD, Intel, ARM).
CPU_DRIVERS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "cpu-thermal", "soc_thermal")
# Labels that represent the CPU as a whole, in order of preference.
PRIMARY_LABELS = ("Tctl", "Tdie", "Package id 0")

INTERVAL_MS = 1000
HISTORY = 120  # samples kept for the graph
WARN_C = 70
HOT_C = 85

# Stress test defaults.
STRESS_MINUTES = 5
STRESS_LIMIT_C = 95  # the test stops itself when the CPU reaches this
# Each worker loads one core and exits by itself if this program dies. Its
# argument is the share of time it stays busy: 1 pins the core at 100 %, lower
# values work in short uneven bursts, like a game's helper threads.
WORKER = """
import os, random, sys, time
parent = os.getppid()
duty = float(sys.argv[1])
frame = 0.05
x = 1.0001
while os.getppid() == parent:
    if duty >= 1:
        for _ in range(200000):
            x = (x * x) % 1000003.7
        continue
    busy = frame * min(0.95, max(0.05, random.gauss(duty, 0.15)))
    end = time.perf_counter() + busy
    while time.perf_counter() < end:
        for _ in range(2000):
            x = (x * x) % 1000003.7
    time.sleep(frame - busy)
"""
# Gaming simulation: a couple of saturated main threads plus partly busy helpers.
GAMING_DUTIES = (1, 1, 0.45, 0.45, 0.35, 0.35, 0.25, 0.25)
MODES = ("All-core (worst case)", "Gaming simulation")


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_sensors():
    """Return a list of (label, path) for every CPU temperature input."""
    sensors = []
    for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read(os.path.join(hwmon, "name")) not in CPU_DRIVERS:
            continue
        for path in sorted(glob.glob(os.path.join(hwmon, "temp*_input"))):
            label = read(path.replace("_input", "_label")) or os.path.basename(path)[:-6]
            sensors.append((label, path))
    if not sensors:
        # Fallback for machines without a dedicated CPU hwmon driver.
        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            kind = read(os.path.join(zone, "type")) or os.path.basename(zone)
            if kind in ("x86_pkg_temp", "acpitz") or "cpu" in kind.lower():
                sensors.append((kind, os.path.join(zone, "temp")))
    sensors.sort(key=lambda s: PRIMARY_LABELS.index(s[0]) if s[0] in PRIMARY_LABELS else 99)
    return sensors


def read_temp(path):
    raw = read(path)
    try:
        return int(raw) / 1000.0
    except (TypeError, ValueError):
        return None


def read_clock():
    """Return (average, fastest) core clock speed in GHz, or None."""
    speeds = []
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            speeds.append(int(read(path)) / 1e6)
        except (TypeError, ValueError):
            pass
    if not speeds:
        return None
    return sum(speeds) / len(speeds), max(speeds)


def colour(temp):
    if temp >= HOT_C:
        return (0.90, 0.25, 0.25)
    if temp >= WARN_C:
        return (0.95, 0.65, 0.15)
    return (0.25, 0.75, 0.45)


class Window(Gtk.Window):
    def __init__(self):
        super().__init__(title="CPU Temperature")
        self.set_default_size(380, 600)
        self.set_border_width(16)

        self.sensors = find_sensors()
        self.history = deque(maxlen=HISTORY)
        self.low = self.high = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)

        self.big = Gtk.Label()
        box.pack_start(self.big, False, False, 0)

        self.stats = Gtk.Label()
        box.pack_start(self.stats, False, False, 0)

        self.clock = Gtk.Label()
        box.pack_start(self.clock, False, False, 0)

        self.graph = Gtk.DrawingArea()
        self.graph.set_size_request(-1, 140)
        self.graph.connect("draw", self.draw_graph)
        box.pack_start(self.graph, True, True, 0)

        grid = Gtk.Grid(column_spacing=24, row_spacing=4)
        box.pack_start(grid, False, False, 0)
        self.rows = []
        for i, (label, _) in enumerate(self.sensors):
            name = Gtk.Label(label=label, xalign=0, hexpand=True)
            value = Gtk.Label(xalign=1)
            grid.attach(name, 0, i, 1, 1)
            grid.attach(value, 1, i, 1, 1)
            self.rows.append(value)

        box.pack_start(Gtk.Separator(), False, False, 0)

        cores = os.cpu_count() or 1
        self.mode = Gtk.ComboBoxText()
        for name in MODES:
            self.mode.append_text(name)
        self.mode.set_active(0)
        self.mode.connect("changed", self.mode_changed)
        box.pack_start(self.mode, False, False, 0)

        controls = Gtk.Grid(column_spacing=12, row_spacing=6)
        box.pack_start(controls, False, False, 0)
        self.threads = self.spin(controls, 0, "Threads", 1, cores, cores)
        self.minutes = self.spin(controls, 1, "Minutes", 1, 240, STRESS_MINUTES)
        self.limit = self.spin(controls, 2, "Stop at °C", 50, 110, STRESS_LIMIT_C)

        self.workers = []
        self.stress_end = 0
        self.stress_button = Gtk.Button(label="Start stress test")
        self.stress_button.connect("clicked", self.toggle_stress)
        self.stress_button.set_sensitive(bool(self.sensors))
        box.pack_start(self.stress_button, False, False, 0)
        self.stress_status = Gtk.Label(label="Stress test idle")
        box.pack_start(self.stress_status, False, False, 0)
        self.connect("destroy", lambda *_: self.stop_stress())

        if self.sensors:
            self.update()
            GLib.timeout_add(INTERVAL_MS, self.update)
        else:
            self.big.set_markup(
                "<span size='large'>No CPU temperature sensor found</span>\n"
                "Try: <tt>sudo modprobe coretemp</tt> (Intel) or <tt>k10temp</tt> (AMD)"
            )

    def spin(self, grid, column, text, low, high, value):
        grid.attach(Gtk.Label(label=text, xalign=0), column, 0, 1, 1)
        button = Gtk.SpinButton.new_with_range(low, high, 1)
        button.set_value(value)
        button.set_hexpand(True)
        grid.attach(button, column, 1, 1, 1)
        return button

    def toggle_stress(self, _button):
        if self.workers:
            self.stop_stress("Stress test stopped")
        else:
            self.start_stress()

    def gaming(self):
        return self.mode.get_active() == 1

    def mode_changed(self, _combo):
        # The gaming simulation uses its own fixed mix of threads.
        self.threads.set_sensitive(not self.gaming() and not self.workers)

    def start_stress(self):
        if self.gaming():
            duties = GAMING_DUTIES[: os.cpu_count() or 1]
        else:
            duties = (1,) * self.threads.get_value_as_int()
        self.workers = [
            subprocess.Popen([sys.executable, "-c", WORKER, str(duty)]) for duty in duties
        ]
        self.stress_name = MODES[self.mode.get_active()]
        self.stress_end = time.monotonic() + self.minutes.get_value_as_int() * 60
        self.stress_peak = None
        self.stress_button.set_label("Stop stress test")
        for widget in (self.mode, self.threads, self.minutes):
            widget.set_sensitive(False)

    def stop_stress(self, message=None):
        for worker in self.workers:
            worker.kill()
        for worker in self.workers:
            worker.wait()
        self.workers = []
        self.stress_button.set_label("Start stress test")
        for widget in (self.mode, self.minutes):
            widget.set_sensitive(True)
        self.threads.set_sensitive(not self.gaming())
        if message:
            if self.stress_peak is not None:
                message += f" — peak {self.stress_peak:.1f} °C"
            self.stress_status.set_text(message)

    def check_stress(self, temp):
        """Called every tick while a stress test runs."""
        if temp is not None:
            self.stress_peak = temp if self.stress_peak is None else max(self.stress_peak, temp)
        left = self.stress_end - time.monotonic()
        if temp is None:
            self.stop_stress("Stopped: lost the temperature reading")
        elif temp >= self.limit.get_value_as_int():
            self.stop_stress(f"Stopped: reached the {self.limit.get_value_as_int()} °C limit")
        elif left <= 0:
            self.stop_stress("Stress test finished")
        else:
            self.stress_status.set_text(
                f"{self.stress_name} — {int(left) // 60}:{int(left) % 60:02d} left"
                f" — peak {self.stress_peak:.1f} °C"
            )

    def update(self):
        temps = [read_temp(path) for _, path in self.sensors]
        if self.workers:
            self.check_stress(temps[0])
        for value, temp in zip(self.rows, temps):
            value.set_text("—" if temp is None else f"{temp:.1f} °C")

        clock = read_clock()
        if clock:
            self.clock.set_text(f"clock  avg {clock[0]:.2f} GHz   fastest core {clock[1]:.2f} GHz")

        main = temps[0]
        if main is not None:
            self.history.append(main)
            self.low = main if self.low is None else min(self.low, main)
            self.high = main if self.high is None else max(self.high, main)
            r, g, b = (int(c * 255) for c in colour(main))
            self.big.set_markup(
                f"<span size='48000' weight='bold' foreground='#{r:02x}{g:02x}{b:02x}'>"
                f"{main:.1f} °C</span>"
            )
            avg = sum(self.history) / len(self.history)
            self.stats.set_text(f"min {self.low:.1f}   avg {avg:.1f}   max {self.high:.1f}")
            self.set_title(f"{main:.0f} °C — CPU Temperature")
        self.graph.queue_draw()
        return True

    def draw_graph(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.12)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if len(self.history) < 2:
            return

        lo = min(self.history) - 5
        hi = max(self.history) + 5

        def y(temp):
            return h - (temp - lo) / (hi - lo) * h

        # Horizontal guide lines every 10 °C.
        cr.set_line_width(1)
        cr.set_font_size(10)
        step = 10
        mark = int(lo // step + 1) * step
        while mark < hi:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
            cr.move_to(0, y(mark))
            cr.line_to(w, y(mark))
            cr.stroke()
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.9)
            cr.move_to(4, y(mark) - 3)
            cr.show_text(f"{mark}°")
            mark += step

        dx = w / (HISTORY - 1)
        x0 = w - (len(self.history) - 1) * dx
        cr.set_source_rgb(*colour(self.history[-1]))
        cr.set_line_width(2)
        for i, temp in enumerate(self.history):
            if i == 0:
                cr.move_to(x0, y(temp))
            else:
                cr.line_to(x0 + i * dx, y(temp))
        cr.stroke()


if __name__ == "__main__":
    win = Window()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
