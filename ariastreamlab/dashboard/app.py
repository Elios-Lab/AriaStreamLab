"""The NiceGUI dashboard (docs/ARCHITECTURE.md §10): one page, one background server.

NiceGUI keeps one application per process, so :func:`get_dashboard` returns a process-wide
singleton. An experiment *attaches* its :class:`DashboardState` (and marker / replay hooks)
when it opens and detaches when it closes; the server keeps running so a notebook can run
several sessions against the same browser tab. The UI only ever reads the state and calls
the two hooks — it never touches the source or the recorders.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ariastreamlab.acquisition.base import ReplayControl
from ariastreamlab.config.models import DashboardConfig
from ariastreamlab.core.logging import get_logger
from ariastreamlab.dashboard.state import DashboardState
from ariastreamlab.monitoring.quality import format_bytes

__all__ = ["Dashboard", "dashboard_available", "get_dashboard"]

log = get_logger("ariastreamlab.dashboard")

Launcher = Callable[..., Any]
"""``launcher(config, *, mock, duration_s, prompt) -> Experiment`` (an object with ``run``,
``stop``, ``config``, ``session_dir`` and the context-manager protocol)."""

MarkHook = Callable[[str, Mapping[str, Any] | None], Any]
IntervalHook = Callable[[str, Mapping[str, Any] | None], Any]
"""``begin(name, payload)`` / ``end(name, payload)`` of the running experiment."""


def dashboard_available() -> bool:
    """True when NiceGUI is installed."""
    import importlib.util

    return importlib.util.find_spec("nicegui") is not None


def native_available() -> bool:
    """True when pywebview is installed (the desktop window of the ``ariastreamlab`` app)."""
    import importlib.util

    return importlib.util.find_spec("webview") is not None


class Dashboard:
    """Process-wide dashboard server; sessions attach and detach their state."""

    def __init__(self, config: DashboardConfig) -> None:
        self.config = config
        self.state: DashboardState | None = None
        self.on_mark: MarkHook | None = None
        self.on_begin: IntervalHook | None = None
        self.on_end: IntervalHook | None = None
        self.on_set_parameters: Callable[[str, Mapping[str, Any]], Any] | None = None
        self.parameter_forms: Callable[[], Mapping[str, tuple[Any, Mapping[str, Any]]]] | None = (
            None
        )
        """``processor id → (Parameters model or None, current values)`` of the running graph."""
        self.replay: ReplayControl | None = None
        self.replay_range_s: tuple[float, float] | None = None
        self.replay_t0_ns: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failed = threading.Event()  # set by the server thread when ui.run() raised
        self._lock = threading.Lock()
        self._pages_built = False
        self._generation = 0
        # the experiment designer (/design) and sessions started from it
        self._designer: Any = None
        self.last_saved: Path | None = None
        self._run: _BrowserRun | None = None
        self.launcher: Launcher | None = None
        """Builds an experiment for :meth:`launch`; set by ``api`` (keeps this layer below it)."""
        self.replay_launcher: Callable[..., Any] | None = None
        """``replay_launcher(path, speed=…) -> Experiment`` for the Sessions page (set by api)."""
        self.setup_run: Any = None  # the Setup page's current command (one at a time)
        self.setup_checks: Any = None  # the last doctor result
        self._setup_checks_shown: Any = None
        self._prompt_lock = threading.Lock()
        self._prompt: tuple[int, str, threading.Event] | None = None
        self._prompt_seq = 0

    # -- lifecycle --------------------------------------------------------------------
    @property
    def url(self) -> str:  # noqa: D102
        return f"http://{self.config.host}:{self.config.port}/"

    @property
    def running(self) -> bool:  # noqa: D102
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 15.0, *, native: bool = False) -> bool:
        """Start the server thread once; returns True when it answers within ``timeout``.

        ``native`` opens a desktop window instead of relying on a browser (best effort from a
        background thread; the ``ariastreamlab`` app runs the window on the main thread).
        """
        if self.running:
            return self._ready.wait(timeout) and not self._failed.is_set()
        self._build_pages()
        self._failed.clear()
        thread = threading.Thread(
            target=lambda: self.serve(native=native), name="dashboard-server", daemon=True
        )
        self._thread = thread
        thread.start()
        deadline = time.monotonic() + timeout
        ok = self._ready.wait(timeout) and not self._failed.is_set()
        # uvicorn runs the startup hooks *before* binding the socket, so a busy port only
        # shows up afterwards: make sure it is *our* server that answers on host:port
        while ok and not self._accepting():
            if self._failed.is_set() or not thread.is_alive() or time.monotonic() > deadline:
                ok = False
                break
            time.sleep(0.05)
        if ok:
            log.info("dashboard.ready", url=self.url)
        else:
            self._failed.set()
            if thread.is_alive():
                log.warning("dashboard.not_ready", url=self.url, timeout_s=timeout)
            else:
                log.error("dashboard.server_failed", url=self.url, hint="is the port in use?")
        return ok

    def _accepting(self) -> bool:
        """True when *this process's* server answers on the configured port."""
        import json
        import urllib.request

        host = "127.0.0.1" if self.config.host in ("0.0.0.0", "") else self.config.host
        try:
            with urllib.request.urlopen(
                f"http://{host}:{self.config.port}/api/ping", timeout=0.5
            ) as r:
                return bool(json.load(r).get("pid") == os.getpid())
        except Exception:
            return False

    def stop(self) -> None:
        """Shut the server down (normally only at process exit)."""
        if not self.running:
            return
        from nicegui import app

        with contextlib.suppress(Exception):
            app.shutdown()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        self._ready.clear()

    def attach(
        self,
        state: DashboardState,
        *,
        on_mark: MarkHook | None = None,
        on_begin: IntervalHook | None = None,
        on_end: IntervalHook | None = None,
        on_set_parameters: Callable[[str, Mapping[str, Any]], Any] | None = None,
        parameter_forms: Callable[[], Mapping[str, tuple[Any, Mapping[str, Any]]]] | None = None,
        replay: ReplayControl | None = None,
        replay_range_s: tuple[float, float] | None = None,
        replay_t0_ns: int | None = None,
    ) -> None:
        """Show this session."""
        with self._lock:
            self.state = state
            self.on_mark = on_mark
            self.on_begin = on_begin
            self.on_end = on_end
            self.on_set_parameters = on_set_parameters
            self.parameter_forms = parameter_forms
            self.replay = replay
            self.replay_range_s = replay_range_s
            self.replay_t0_ns = replay_t0_ns
            self._generation += 1

    def detach(self, state: DashboardState | None = None) -> None:
        """Keep showing the last state, but drop the hooks (the session is over)."""
        with self._lock:
            if state is not None and self.state is not state:
                return
            self.on_mark = None
            self.on_begin = None
            self.on_end = None
            self.on_set_parameters = None
            self.parameter_forms = None
            self.replay = None

    # -- server -------------------------------------------------------------------------
    def serve(self, *, native: bool = False, show: bool = False) -> None:
        """Run the server in the calling thread (blocks).

        ``native=True`` opens the interface in a desktop window (pywebview) instead of a
        browser tab — the ``ariastreamlab`` app; it needs the main thread on macOS. When
        pywebview is not available the page is opened in the browser instead.
        """
        from nicegui import app, ui

        @app.on_startup
        def _started() -> None:
            self._ready.set()

        if native and not native_available():
            log.warning(
                "app.no_native_window",
                note="pywebview is not installed (pip install pywebview); opening the browser",
            )
            native, show = False, True
        self._build_pages()
        # under pytest NiceGUI replaces the port with this variable (for its screen tests)
        os.environ.setdefault("NICEGUI_SCREEN_TEST_PORT", str(self.config.port))
        try:
            ui.run(
                host=self.config.host,
                port=self.config.port,
                title="AriaStreamLab",
                dark=True,
                reload=False,
                show=show,
                native=native,
                window_size=(1440, 960) if native else None,
                show_welcome_message=False,
                uvicorn_logging_level="warning",
                favicon="🕶️",
            )
        except Exception:
            log.exception("dashboard.server_failed")
            self._failed.set()
            self._ready.set()

    def _build_pages(self) -> None:
        if self._pages_built:
            return
        self._pages_built = True
        from nicegui import app, ui

        dash = self

        @app.get("/api/ping")
        def api_ping() -> dict[str, Any]:
            return {"app": "ariastreamlab", "pid": os.getpid()}

        @app.get("/api/state")
        def api_state() -> dict[str, Any]:
            st = dash.state
            data = st.snapshot() if st is not None else {"session": {}, "samples_seen": 0}
            data["replay"] = dash._replay_view()
            return data

        @ui.page("/")
        def home() -> None:
            from ariastreamlab.dashboard.pages import build_home_page

            build_home_page(dash, ui)

        @ui.page("/monitor")
        def monitor() -> None:
            _build_index(dash, ui)

        @ui.page("/design")
        def design() -> None:
            from ariastreamlab.dashboard.designer_ui import build_design_page

            build_design_page(dash, ui)

        @ui.page("/sessions")
        def sessions() -> None:
            from ariastreamlab.dashboard.pages import build_sessions_page

            build_sessions_page(dash, ui)

        @ui.page("/setup")
        def setup() -> None:
            from ariastreamlab.dashboard.setup_ui import build_setup_page

            build_setup_page(dash, ui)

    # -- the designer and sessions started from the browser ---------------------------------
    @property
    def designer(self) -> Any:
        """The :class:`~ariastreamlab.dashboard.designer.Designer` behind ``/design``."""
        if self._designer is None:
            from ariastreamlab.dashboard.designer import Designer

            self._designer = Designer.from_preset("base", base_dir=Path.cwd())
        return self._designer

    @designer.setter
    def designer(self, value: Any) -> None:
        self._designer = value

    @staticmethod
    def sdk_available() -> bool:  # noqa: D102
        from ariastreamlab.acquisition.aria_gen2 import sdk_available

        return sdk_available()

    def launch(self, designer: Any, *, mock: bool, duration_s: float | None) -> None:
        """Start a session from the designer's document in a background thread.

        Raises ``RuntimeError`` when one started from here is still running.
        """
        from ariastreamlab.config.loader import load_dict

        with self._lock:
            if self.launcher is None:
                raise RuntimeError("no launcher registered (start with `ariastreamlab design`)")
            if self._run is not None and self._run.running:
                raise RuntimeError("a session started from the browser is still running")
            cfg = load_dict(designer.to_dict(compact=False), base_dir=designer.base_dir)
            # the server is this one: never open another tab, keep the port we are on
            cfg = cfg.model_copy(
                update={
                    "dashboard": cfg.dashboard.model_copy(
                        update={
                            "enabled": True,
                            "open_browser": False,
                            "host": self.config.host,
                            "port": self.config.port,
                        }
                    ),
                    "storage": cfg.storage.model_copy(
                        update={"root": (designer.base_dir / cfg.storage.root).resolve()}
                    ),
                }
            )
            exp = self.launcher(cfg, mock=mock, duration_s=duration_s, prompt=self._ask)
            run = _BrowserRun(exp, origin="mock" if mock else "live")
            self._run = run
            run.start()

    def launch_replay(self, path: Path | str, *, speed: float = 1.0) -> None:
        """Replay a session directory or VRS file from the Sessions page (background thread)."""
        with self._lock:
            if self.replay_launcher is None:
                raise RuntimeError("no replay launcher registered (start with `ariastreamlab app`)")
            if self._run is not None and self._run.running:
                raise RuntimeError("a session started from the browser is still running")
            exp = self.replay_launcher(Path(path), speed=speed)
            run = _BrowserRun(exp, origin="replay")
            self._run = run
            run.start()

    @property
    def sessions_root(self) -> Path:
        """Where the Sessions page looks: the designer's ``storage.root`` (relative to its dir)."""
        root = Path(str(self.designer.doc.get("storage", {}).get("root") or "sessions"))
        return root if root.is_absolute() else (self.designer.base_dir / root).resolve()

    def run_state(self) -> dict[str, Any] | None:
        """State of the session started from the browser (``None`` when there was none)."""
        run = self._run
        if run is None:
            return None
        return {
            "state": run.state,
            "running": run.running,
            "experiment": run.exp.config.experiment.name,
            "origin": run.origin,
            "error": run.error,
            "session_dir": None if run.exp.session_dir is None else str(run.exp.session_dir),
        }

    def stop_run(self) -> None:
        """Ask the browser-started session to stop (no-op otherwise)."""
        run = self._run
        if run is not None and run.running:
            run.exp.stop()
            self.answer_prompt()  # a session waiting at the hotspot prompt must not hang

    def _ask(self, message: str) -> None:
        """``Experiment.prompt`` for browser-started sessions: a dialog, answered by a click."""
        with self._prompt_lock:
            self._prompt_seq += 1
            done = threading.Event()
            self._prompt = (self._prompt_seq, message, done)
        log.info("dashboard.prompt", message=message.splitlines()[0])
        done.wait(timeout=600)
        with self._prompt_lock:
            if self._prompt is not None and self._prompt[2] is done:
                self._prompt = None

    def pending_prompt(self) -> tuple[int, str] | None:
        """``(id, message)`` of the question a live session is waiting on, if any."""
        with self._prompt_lock:
            return None if self._prompt is None else (self._prompt[0], self._prompt[1])

    def answer_prompt(self) -> None:
        """Release a session waiting on :meth:`pending_prompt`."""
        with self._prompt_lock:
            if self._prompt is not None:
                self._prompt[2].set()

    # -- helpers used by the page -------------------------------------------------------
    def _replay_view(self) -> dict[str, Any] | None:
        rc = self.replay
        if rc is None:
            return None
        try:
            pos = getattr(rc, "position_ns", None)
            t0 = self.replay_t0_ns or 0
            return {
                "paused": bool(rc.paused),
                "speed": float(getattr(rc, "speed", 1.0)),
                "position_s": None if pos is None else round((pos - t0) / 1e9, 2),
                "range_s": list(self.replay_range_s) if self.replay_range_s else None,
            }
        except Exception:
            return None


class _BrowserRun:
    """One session started from the designer page, on its own thread."""

    def __init__(self, exp: Any, *, origin: str) -> None:
        self.exp = exp
        self.origin = origin
        self.state = "starting"
        self.error: str | None = None
        self._thread = threading.Thread(target=self._main, name="browser-run", daemon=True)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def _main(self) -> None:
        try:
            with self.exp:
                self.state = "running"
                self.exp.run(install_signal_handler=False)
            self.state = "finished"
        except Exception as exc:
            self.state = "failed"
            self.error = str(exc)
            log.exception("dashboard.run_failed")


_SINGLETON: Dashboard | None = None
_SINGLETON_LOCK = threading.Lock()


def get_dashboard(config: DashboardConfig) -> Dashboard:
    """The process-wide dashboard (created with the first config seen)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = Dashboard(config)
        elif (_SINGLETON.config.host, _SINGLETON.config.port) != (config.host, config.port):
            log.warning(
                "dashboard.port_fixed",
                note="the dashboard server is already running on another port",
                url=_SINGLETON.url,
            )
        return _SINGLETON


# ----------------------------------------------------------------------------- the page
def _fmt(v: Any, digits: int = 1) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _phase(payload: Any) -> str:
    if isinstance(payload, dict) and payload.get("phase") in ("start", "end"):
        extra = {k: v for k, v in payload.items() if k not in ("phase", "interval", "source")}
        return f" ({payload['phase']}{', ' + json.dumps(extra) if extra else ''})"
    return ""


def _fmt_t(t_s: float | None) -> str:
    return "host time" if t_s is None else f"{t_s:.2f} s"


NAV = (
    ("home", "/"),
    ("setup", "/setup"),
    ("design", "/design"),
    ("monitor", "/monitor"),
    ("sessions", "/sessions"),
)


def nav_header(ui: Any, current: str) -> Any:
    """The application header: title, the four pages, and a slot for page status on the right."""
    ui.colors(primary="#5b8def")
    with ui.header().classes("items-center justify-between px-4"):
        with ui.row().classes("items-center gap-5"):
            ui.label("AriaStreamLab").classes("text-xl font-bold")
            for name, path in NAV:
                link = ui.link(name, path).classes("text-white text-sm")
                if name == current:
                    link.classes("font-bold underline")
                else:
                    link.classes("opacity-75")
        right = ui.row().classes("items-center gap-4")
    return right


def _build_index(dash: Dashboard, ui: Any) -> None:
    cfg = dash.config
    with nav_header(ui, "monitor"):
        session_label = ui.label("no session").classes("text-sm opacity-80")
        status_label = ui.label("").classes("text-sm")

    # ---- images --------------------------------------------------------------------
    image_widgets: dict[str, Any] = {}
    image_shown_ts: dict[str, int] = {}
    with ui.row().classes("w-full gap-2 px-2") as image_row:
        pass

    def refresh_images() -> None:
        st = dash.state
        if st is None:
            return
        for sid in st.image_streams():
            frame = st.image(sid)
            if frame is None:
                continue
            w = image_widgets.get(sid)
            if w is None:
                with image_row, ui.card().classes("p-2"):
                    ui.label(sid).classes("text-xs opacity-70")
                    w = ui.interactive_image(frame.data_url).classes("w-[320px]")
                    image_widgets[sid] = w
            if image_shown_ts.get(sid) != frame.device_timestamp_ns:
                w.set_source(frame.data_url)
                image_shown_ts[sid] = frame.device_timestamp_ns
                overlay = ""
                for gsid, g in st.gaze_points.items():
                    if sid == "rgb" and g.get("valid"):
                        cx, cy = g["x_norm"] * frame.width, g["y_norm"] * frame.height
                        r = max(8.0, frame.width * 0.012)
                        colour = "#4ade80" if g.get("in_image") else "#f87171"
                        overlay += (
                            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" stroke="{colour}" '
                            f'stroke-width="{max(2.0, r / 4):.1f}" fill="none" />'
                            f'<text x="{cx + r + 4:.1f}" y="{cy:.1f}" fill="{colour}" '
                            f'font-size="{r * 1.5:.0f}">{gsid}</text>'
                        )
                w.content = overlay

    ui.timer(1.0 / cfg.image_fps, refresh_images)

    # ---- charts ----------------------------------------------------------------------
    with ui.card().classes("w-full mx-2 p-2"):
        with ui.row().classes("items-center gap-4"):
            ui.label("Signals").classes("font-bold")
            channel_select = ui.select([], multiple=True, label="channels").classes("w-[600px]")
            window = ui.slider(min=2, max=60, value=10).props("label-always").classes("w-[200px]")
            ui.label("s window").classes("text-xs opacity-70")
        chart = ui.echart(
            {
                "animation": False,
                "backgroundColor": "transparent",
                "tooltip": {"trigger": "axis"},
                "legend": {"textStyle": {"color": "#ccc"}},
                "xAxis": {"type": "value", "name": "s", "axisLabel": {"color": "#aaa"}},
                "yAxis": {"type": "value", "scale": True, "axisLabel": {"color": "#aaa"}},
                "series": [],
            }
        ).classes("w-full h-[320px]")

    known_keys: set[str] = set()

    def refresh_chart() -> None:
        st = dash.state
        if st is None:
            return
        keys = st.channel_keys()
        new = [k for k in keys if k not in known_keys]
        if new:
            known_keys.update(new)
            channel_select.options = sorted(known_keys)
            if not channel_select.value:
                default = [k for k in keys if k.endswith(("/accel_z", "/value", "/yaw"))][:4]
                channel_select.value = default
            channel_select.update()
        selected = list(channel_select.value or [])
        last_s = float(window.value or 10)
        data = st.series(selected, last_s=last_s)
        lo, hi = st.window(last_s)
        chart.options["xAxis"]["min"] = round(lo, 2)
        chart.options["xAxis"]["max"] = round(hi, 2)
        chart.options["series"] = [
            {"name": k, "type": "line", "showSymbol": False, "data": pts, "sampling": "lttb"}
            for k, pts in data.items()
        ]
        chart.update()

    ui.timer(0.5, refresh_chart)

    # ---- trajectory (poses) --------------------------------------------------------------
    with ui.card().classes("w-full mx-2 p-2") as traj_card:
        with ui.row().classes("items-center gap-4"):
            ui.label("Trajectory").classes("font-bold")
            traj_label = ui.label("").classes("text-xs opacity-70")
        traj_chart = ui.echart(
            {
                "animation": False,
                "backgroundColor": "transparent",
                "tooltip": {"trigger": "item"},
                "xAxis": {"type": "value", "name": "x (m)", "axisLabel": {"color": "#aaa"}},
                "yAxis": {"type": "value", "name": "y (m)", "axisLabel": {"color": "#aaa"}},
                "series": [],
            }
        ).classes("w-full h-[300px]")
        traj_card.visible = False

    def refresh_trajectory() -> None:
        st = dash.state
        if st is None:
            return
        poses = st.pose_streams()
        if not poses:
            traj_card.visible = False
            return
        traj_card.visible = True
        series = []
        for sid in poses:
            pts = st.trajectory(sid, last_s=120.0)
            series.append({"name": sid, "type": "line", "showSymbol": False, "data": pts})
            if pts:
                series.append(
                    {
                        "name": f"{sid} now",
                        "type": "scatter",
                        "symbolSize": 10,
                        "data": [pts[-1]],
                    }
                )
        traj_label.text = f"{', '.join(poses)} · last 120 s, odometry frame (x, y)"
        traj_chart.options["series"] = series
        traj_chart.update()

    ui.timer(1.0, refresh_trajectory)

    # ---- health + streams --------------------------------------------------------------
    with ui.row().classes("w-full gap-2 px-2"):
        with ui.card().classes("p-2 min-w-[260px]"):
            ui.label("Device").classes("font-bold")
            device_md = ui.markdown("")
        with ui.card().classes("p-2 min-w-[260px]"):
            ui.label("Link").classes("font-bold")
            link_md = ui.markdown("")
        with ui.card().classes("p-2 grow"):
            ui.label("Streams").classes("font-bold")
            stream_table = (
                ui.table(
                    columns=[
                        {"name": "stream", "label": "stream", "field": "stream", "align": "left"},
                        {"name": "received", "label": "received", "field": "received"},
                        {"name": "rate", "label": "Hz", "field": "rate"},
                        {"name": "nominal", "label": "nominal", "field": "nominal"},
                        {"name": "ooo", "label": "out of order", "field": "ooo"},
                        {"name": "drops", "label": "transport drops", "field": "drops"},
                    ],
                    rows=[],
                    row_key="stream",
                )
                .classes("w-full")
                .props("dense flat")
            )
    with ui.row().classes("w-full gap-2 px-2"):
        with ui.card().classes("p-2 grow"):
            with ui.row().classes("items-center gap-2"):
                ui.label("Pipeline").classes("font-bold")
                ui.button("parameters", icon="tune", on_click=lambda: _open_parameters()).props(
                    "flat dense"
                ).tooltip("Change parameters while running (recorded as a config_change event)")
            proc_table = (
                ui.table(
                    columns=[
                        {"name": "id", "label": "processor", "field": "id", "align": "left"},
                        {"name": "fired", "label": "fired", "field": "fired"},
                        {"name": "outputs", "label": "outputs", "field": "outputs"},
                        {"name": "exec", "label": "exec ms", "field": "exec"},
                        {"name": "latency", "label": "latency ms", "field": "latency"},
                        {"name": "drops", "label": "queue drops", "field": "drops"},
                        {"name": "errors", "label": "errors", "field": "errors"},
                    ],
                    rows=[],
                    row_key="id",
                )
                .classes("w-full")
                .props("dense flat")
            )
        with ui.card().classes("p-2 grow"):
            ui.label("Device quality").classes("font-bold")
            quality_table = (
                ui.table(
                    columns=[
                        {"name": "sensor", "label": "sensor", "field": "sensor", "align": "left"},
                        {"name": "actual", "label": "actual", "field": "actual"},
                        {"name": "dropped", "label": "device drops", "field": "dropped"},
                        {"name": "seq", "label": "sequential", "field": "seq"},
                        {"name": "score", "label": "score", "field": "score"},
                    ],
                    rows=[],
                    row_key="sensor",
                )
                .classes("w-full")
                .props("dense flat")
            )

    def refresh_health() -> None:
        st = dash.state
        if st is None:
            session_label.text = "no session attached"
            return
        snap = st.snapshot()
        sess = snap["session"]
        session_label.text = (
            f"{sess.get('experiment', '?')} / {sess.get('participant', '?')} · "
            f"{sess.get('origin', '?')} · {sess.get('session_id', '')[:8]}"
        )
        elapsed = snap.get("elapsed_s")
        status_label.text = (
            f"{snap['samples_seen']} samples · {_fmt(elapsed)} s of data · {sess.get('state', '')}"
        )
        dev = snap["health"].get("device") or {}
        link = snap["health"].get("link") or {}
        if dev:
            temp = dev.get("skin_temperature_c")
            device_md.content = (
                f"battery **{_fmt(dev.get('battery_percent'), 0)} %**"
                f"{' (charging)' if dev.get('charging') else ''}  \n"
                f"skin temperature **{_fmt(temp)} °C**"
                f"{' ⚠️ thermal mitigation' if dev.get('thermal_mitigation') else ''}  \n"
                f"Wi-Fi {'on' if dev.get('wifi_connected') else 'off'} "
                f"{dev.get('wifi_ssid') or ''}  \nmode {dev.get('device_mode') or '–'}"
            )
        else:
            device_md.content = "_no device status (mock / replay)_"
        if link:
            ts = link.get("ticsync") or {}
            link_md.content = (
                f"data path **{link.get('data_path')}** "
                f"from {link.get('stream_source_ip') or '–'}  \n"
                f"control over **{link.get('control_transport') or '–'}**  \n"
                f"connections {link.get('connections')} · samples {link.get('samples')}  \n"
                f"TicSync {'valid' if ts.get('valid') else 'not valid'}  \n"
                f"SDK queues {link.get('sdk_queues') or {}}"
                + (
                    f"  \n**⚠ stream stalled: no sample for {link['stalled_s']:.0f} s**"
                    if link.get("stalled_s") is not None
                    else ""
                )
            )
        else:
            link_md.content = (
                f"origin **{sess.get('origin', '?')}**  \nsamples {snap['samples_seen']}"
            )
        queues = snap.get("app_queues") or {}
        if queues:
            dropped = {k: v.get("dropped", 0) for k, v in queues.items() if v.get("dropped")}
            link_md.content += "  \napp queues: " + (
                ", ".join(f"`{k}` dropped {v}" for k, v in dropped.items())
                if dropped
                else "no drops"
            )
        storage = snap["health"].get("storage") or {}
        if storage:
            link_md.content += (
                f"  \nsession on disk **{format_bytes(storage.get('bytes'))}**"
                f" · writing {format_bytes(storage.get('write_rate_bps'))}/s"
                f" · free {format_bytes(storage.get('free_bytes'))}"
            )
        rows = []
        for sid, s in sorted(snap.get("streams", {}).items()):
            rows.append(
                {
                    "stream": sid,
                    "received": s.get("received"),
                    "rate": _fmt(s.get("effective_rate_hz")),
                    "nominal": _fmt(s.get("nominal_rate_hz"), 0),
                    "ooo": s.get("out_of_order"),
                    "drops": s.get("transport_dropped"),
                }
            )
        stream_table.rows = rows
        stream_table.update()
        prow = []
        for pid, p in sorted(snap.get("processors", {}).items()):
            prow.append(
                {
                    "id": pid,
                    "fired": p.get("fired"),
                    "outputs": p.get("outputs"),
                    "exec": _fmt(p.get("exec_ms_mean"), 2),
                    "latency": _fmt(p.get("latency_ms_mean")),
                    "drops": sum((p.get("queue_dropped") or {}).values()),
                    "errors": p.get("errors"),
                }
            )
        proc_table.rows = prow
        proc_table.update()
        q = snap["health"].get("quality") or {}
        qrows = []
        for sdata in q.get("sensors", []):
            qrows.append(
                {
                    "sensor": sdata.get("sensor_type"),
                    "actual": sdata.get("actual"),
                    "dropped": sdata.get("dropped"),
                    "seq": sdata.get("sequential_drops"),
                    "score": _fmt(sdata.get("score")),
                }
            )
        quality_table.rows = qrows
        quality_table.update()
        ev_rows = [
            f"{_fmt_t(e['t_s']):>10}  {e['kind']:<12} {e['name']}{_phase(e['payload'])}"
            for e in snap["events"][-15:]
        ]
        events_md.content = "```\n" + ("\n".join(ev_rows) or "(no events yet)") + "\n```"
        rv = dash._replay_view()
        if rv is not None:
            replay_card.visible = True
            pos = rv.get("position_s")
            replay_label.text = (
                f"{'paused' if rv['paused'] else 'playing'} · {rv['speed']:g}× · {_fmt(pos, 2)} s"
            )
            if rv.get("range_s"):
                seek.max = rv["range_s"][1] - rv["range_s"][0]
                if pos is not None and not _seek_active[0]:
                    seek.value = pos
            if abs(float(speed.value or 1.0) - float(rv["speed"])) > 1e-6:
                speed.value = float(rv["speed"])  # changed from the API / CLI
        else:
            replay_card.visible = False

    # ---- live parameter changes ----------------------------------------------------------
    with ui.dialog() as params_dialog, ui.card().classes("min-w-[420px]"):
        ui.label("Processor parameters").classes("font-bold")
        params_body = ui.column().classes("w-full gap-2")

    def _open_parameters() -> None:
        from ariastreamlab.dashboard.designer import fields_of
        from ariastreamlab.dashboard.designer_ui import field_widget

        forms = dash.parameter_forms() if dash.parameter_forms is not None else {}
        setter = dash.on_set_parameters
        params_body.clear()
        with params_body:
            if not forms or setter is None:
                ui.label("no running processors").classes("opacity-70")
                forms = {}
            apply_fn = setter or (lambda *_: {})
            for pid, (model, current) in forms.items():
                with ui.expansion(pid, value=len(forms) <= 3).classes("w-full"):
                    if model is None or not fields_of(model):
                        ui.label("no declared parameters").classes("text-xs opacity-70")
                        continue
                    draft: dict[str, Any] = dict(current)
                    with ui.row().classes("items-end gap-3 flex-wrap"):
                        for spec in fields_of(model):
                            field_widget(ui, spec, draft, lambda: None)

                    def _apply(
                        pid: str = pid, draft: dict[str, Any] = draft, current: Any = current
                    ) -> None:
                        changes = {k: v for k, v in draft.items() if current.get(k) != v}
                        if not changes:
                            ui.notify("nothing changed")
                            return
                        try:
                            new = apply_fn(pid, changes)
                        except Exception as exc:
                            ui.notify(str(exc), type="negative")
                            return
                        current.clear()
                        current.update(new)
                        ui.notify(f"{pid}: {changes} applied and recorded", type="positive")

                    ui.button("apply", on_click=_apply, icon="check").props("dense")
        params_dialog.open()

    # ---- markers + replay --------------------------------------------------------------
    with ui.row().classes("w-full gap-2 px-2 pb-4"):
        with ui.card().classes("p-2 grow"):
            ui.label("Markers").classes("font-bold")
            with ui.row().classes("items-end gap-2 flex-wrap"):
                marker_name = ui.input("name", value="trial").classes("w-[160px]")
                marker_payload = ui.input("payload (JSON)", placeholder='{"trial": 3}').classes(
                    "w-[220px]"
                )

                def _payload() -> dict[str, Any] | None:
                    text = (marker_payload.value or "").strip()
                    if not text:
                        return {"source": "dashboard"}
                    try:
                        data = json.loads(text)
                    except ValueError:
                        ui.notify("payload must be a JSON object", type="warning")
                        return None
                    if not isinstance(data, dict):
                        ui.notify("payload must be a JSON object", type="warning")
                        return None
                    return {**data, "source": "dashboard"}

                def _fire(kind: str) -> None:
                    hook = {"mark": dash.on_mark, "begin": dash.on_begin, "end": dash.on_end}[kind]
                    if hook is None:
                        ui.notify("no running session", type="warning")
                        return
                    payload = _payload()
                    if payload is None:
                        return
                    name = (marker_name.value or "marker").strip()
                    try:
                        hook(name, payload)
                    except ValueError as exc:  # end() without a begin()
                        ui.notify(str(exc), type="warning")
                        return
                    ui.notify(f"{kind} {name!r}")

                ui.button("mark", on_click=lambda: _fire("mark"), icon="flag")
                ui.button("begin", on_click=lambda: _fire("begin"), icon="play_arrow").props(
                    "flat dense"
                ).tooltip("Open an interval with this name (a trial, a condition…)")
                ui.button("end", on_click=lambda: _fire("end"), icon="stop").props(
                    "flat dense"
                ).tooltip("Close the most recent open interval with this name")
                ui.button(
                    "note",
                    on_click=lambda: (dash.on_mark or (lambda *_: None))(
                        "note", {"source": "dashboard"}
                    ),
                ).props("flat dense")
            events_md = ui.markdown("")
        with ui.card().classes("p-2 min-w-[360px]") as replay_card:
            ui.label("Replay").classes("font-bold")
            replay_label = ui.label("")
            _seek_active = [False]
            with ui.row().classes("items-center gap-2"):

                def _toggle() -> None:
                    rc = dash.replay
                    if rc is None:
                        return
                    rc.resume() if rc.paused else rc.pause()

                ui.button("pause / resume", on_click=_toggle, icon="pause")
                speed = (
                    ui.slider(min=0.25, max=4.0, step=0.25, value=1.0)
                    .props("label-always")
                    .classes("w-[160px]")
                )

                def _speed(_e: Any) -> None:
                    rc = dash.replay
                    if rc is not None:
                        with contextlib.suppress(Exception):
                            rc.set_speed(float(speed.value))

                speed.on("change", _speed)
            seek = (
                ui.slider(min=0.0, max=1.0, step=0.1, value=0.0)
                .props("label-always")
                .classes("w-full")
            )

            def _seek_start(_e: Any) -> None:
                _seek_active[0] = True

            def _seek(_e: Any) -> None:
                _seek_active[0] = False
                rc = dash.replay
                if rc is None or dash.replay_range_s is None:
                    return
                t0 = dash.replay_t0_ns or 0
                target = int(t0 + float(seek.value) * 1e9)
                with contextlib.suppress(Exception):
                    rc.seek(target)

            seek.on("pan", _seek_start)
            seek.on("change", _seek)
            replay_card.visible = False

    ui.timer(1.0, refresh_health)
    ui.timer(0.1, refresh_health, once=True)
