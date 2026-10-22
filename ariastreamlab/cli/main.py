"""The ``ariastreamlab`` command line.

Commands available so far: ``streams``, ``presets``, ``schema``, ``inspect-config``,
``run --mock``, ``replay``, ``inspect``. ``doctor`` and ``devices`` arrive with the Aria
adapter (Phase 4, see docs/ARCHITECTURE.md §12).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ariastreamlab import __version__
from ariastreamlab.api import Experiment
from ariastreamlab.config.loader import dump_resolved, load_config, load_preset, preset_names
from ariastreamlab.config.models import ExperimentConfig
from ariastreamlab.config.schema import export_json_schema_text
from ariastreamlab.core.errors import AriaStreamLabError
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.processing import list_processors, validate_processors

app = typer.Typer(
    name="ariastreamlab",
    help="Experiment framework for Meta Project Aria Gen 2 glasses.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
console = Console()
err_console = Console(stderr=True)


def _fail(exc: AriaStreamLabError) -> None:
    err_console.print(f"[bold red]error:[/] {escape(str(exc))}")
    raise typer.Exit(code=2)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ariastreamlab {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit",
        ),
    ] = None,
) -> None:
    """Experiment framework for Meta Project Aria Gen 2 glasses."""


@app.command()
def streams(
    live_only: Annotated[
        bool, typer.Option("--live-only", help="Only streams with a typed SDK callback")
    ] = False,
) -> None:
    """List the Aria Gen 2 streams AriaStreamLab knows about."""
    table = Table(title="Aria Gen 2 streams", show_lines=False)
    for col in ("id", "group", "category", "VRS label", "nominal Hz", "live", "parameters"):
        table.add_column(col)
    for d in REGISTRY:
        if live_only and not d.live_available:
            continue
        params = ", ".join(
            f"{n}={s.default}" + (f" ∈ {list(s.choices)}" if s.choices else "")
            for n, s in d.parameters.items()
        )
        table.add_row(
            d.id,
            d.group or "",
            d.category,
            d.native_label,
            "" if d.nominal_rate_hz is None else f"{d.nominal_rate_hz:g}",
            "yes" if d.live_available else "VRS only",
            params,
        )
    console.print(table)


@app.command()
def doctor(
    device: Annotated[
        bool, typer.Option("--device", help="Also talk to the glasses (device list, auth check)")
    ] = False,
    port: Annotated[int, typer.Option("--port", help="Streaming server port to check")] = 6768,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check this laptop: Python, packages, the SDK, the CLI, the streaming port, disk space."""
    from ariastreamlab.cli.doctor import run_checks

    checks = run_checks(server_port=port, with_device=device)
    if json_out:
        print(json.dumps([c.as_dict() for c in checks], indent=2))
    else:
        colours = {"ok": "green", "warn": "yellow", "fail": "red", "skip": "dim"}
        for c in checks:
            console.print(f"[{colours[c.status]}]{c.status:4}[/] {c.name}: {escape(c.detail)}")
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(code=1)


@app.command()
def devices(
    ble_scan: Annotated[
        bool, typer.Option("--ble-scan", help="Scan for paired BLE devices in range (slow)")
    ] = False,
) -> None:
    """List the glasses this laptop can reach (USB network link, configured IP, CLI view)."""
    from ariastreamlab.acquisition.aria_gen2 import sdk_available

    if not sdk_available():
        _fail(
            AriaStreamLabError(
                "the Project Aria client SDK is not installed",
                hint="pip install 'ariastreamlab[aria]' on the laptop that talks to the glasses",
            )
        )
        return
    from ariastreamlab.acquisition.aria_gen2.cli import AriaCli
    from ariastreamlab.acquisition.aria_gen2.device import AriaGen2DeviceManager
    from ariastreamlab.config.models import DeviceConfig

    try:
        mgr = AriaGen2DeviceManager(DeviceConfig())
        found = mgr.discover()
    except AriaStreamLabError as exc:
        _fail(exc)
        return
    if found:
        table = Table(title="Devices (SDK)")
        for col in ("serial", "transport", "ip"):
            table.add_column(col)
        for d in found:
            table.add_row(d.serial, d.transport, d.ip or "-")
        console.print(table)
    else:
        console.print(
            "[yellow]no USB network link to a device[/] (cable in? `aria_gen2 auth pair`?)"
        )
    try:
        res = AriaCli().device_list(ble_scan=ble_scan)
        console.print("[bold]aria_gen2 device list[/]")
        console.print(escape(res.stdout.strip() or res.stderr.strip() or "(no output)"))
    except AriaStreamLabError as exc:
        console.print(f"[yellow]{escape(str(exc))}[/]")


@app.command()
def presets() -> None:
    """List the bundled configuration presets (use with "extends": "preset:<name>")."""
    for name in preset_names():
        cfg = load_preset(name)
        console.print(
            f"[bold]{name}[/]: {cfg.experiment.description or '(no description)'}"
            f" — streams: {', '.join(cfg.enabled_streams()) or 'none'}"
        )


@app.command()
def processors(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Also list processor files next to this experiment"),
    ] = None,
) -> None:
    """List the processors you can use in "processors": [{"type": ...}]."""
    path = None
    if config is not None:
        try:
            path = _processors_path(load_config(config), config)
        except AriaStreamLabError as exc:
            _fail(exc)
            return
    table = Table(title="Available processors")
    for col in ("type", "version", "origin", "parameters", "description"):
        table.add_column(col)
    for info in list_processors(path):
        params = "-"
        if info.cls.Parameters is not None:
            params = ", ".join(
                f"{n}" + ("" if f.is_required() else f"={f.get_default()!r}")
                for n, f in info.cls.Parameters.model_fields.items()
            )
        table.add_row(info.name, info.version, info.origin, params, info.description)
    console.print(table)
    console.print(
        '[dim]custom: "type": "package.module:Class" or "my_file.py:Class"'
        " (looked up in processors_path, default: processors/ next to the JSON)[/]"
    )


def _processors_path(cfg: ExperimentConfig, config_file: Path | None) -> Path | None:
    """``processors_path`` as the loader anchored it (absolute, or ``None``)."""
    del config_file  # kept for call-site readability; the loader already anchored the path
    return cfg.processors_path


@app.command()
def schema(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to this file instead of stdout")
    ] = None,
) -> None:
    """Export the JSON Schema of the experiment configuration."""
    text = export_json_schema_text()
    if output is None:
        console.print_json(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
        console.print(f"schema written to {output}")


@app.command("inspect-config")
def inspect_config(
    path: Annotated[Path, typer.Argument(help="experiment.json")],
    resolved: Annotated[
        bool,
        typer.Option(
            "--resolved", help="Print the fully resolved JSON (presets merged, defaults applied)"
        ),
    ] = False,
) -> None:
    """Validate an experiment file and show what it will do."""
    try:
        cfg = load_config(path)
    except AriaStreamLabError as exc:
        _fail(exc)
        return
    if resolved:
        console.print_json(dump_resolved(cfg))
        return
    console.print(
        f"[green]OK[/] {path} — experiment [bold]{cfg.experiment.name}[/],"
        f" participant {cfg.experiment.participant}"
    )
    table = Table(title="Enabled streams")
    table.add_column("stream")
    table.add_column("parameters")
    table.add_column("nominal Hz")
    table.add_column("live")
    for sid, params in cfg.enabled_streams().items():
        d = REGISTRY.get(sid)
        table.add_row(
            sid,
            ", ".join(f"{k}={v}" for k, v in params.items()) or "-",
            "" if d.nominal_rate_hz is None else f"{d.nominal_rate_hz:g}",
            "yes" if d.live_available else "VRS only",
        )
    console.print(table)
    if cfg.processors:
        # resolve the classes and validate the parameters now, not at run time
        try:
            procs = {r.cfg.id: r for r in validate_processors(cfg, _processors_path(cfg, path))}
        except AriaStreamLabError as exc:
            _fail(exc)
            return
        table = Table(title="Processors")
        for col in ("id", "type", "version", "inputs", "outputs", "join", "policy", "save"):
            table.add_column(col)
        for p in cfg.processors:
            r = procs.get(p.id)
            table.add_row(
                p.id,
                p.type,
                "-" if r is None else r.cls.version,
                ", ".join(p.inputs),
                ", ".join(p.outputs),
                f"{p.execution.join}"
                + (f"/{p.execution.tolerance_ms:g}ms" if len(p.inputs) > 1 else ""),
                f"{p.execution.drop_policy.value}/{p.execution.queue_size}",
                "yes" if p.save_output else ("off" if not p.enabled else "no"),
            )
        console.print(table)
    conn = cfg.device.connection
    from ariastreamlab.acquisition.aria_gen2.profiles import build_plan

    try:
        plan = build_plan(cfg, None)
    except AriaStreamLabError as exc:
        _fail(exc)
        return
    hs = conn.hotspot
    hotspot = f"start {hs.band}/{hs.bandwidth}" if hs.start else "reuse"
    console.print(
        f"device: interface={plan.interface} batch_period_ms={plan.batch_period_ms}"
        f" profile={plan.profile_name or 'custom JSON'} ({cfg.device.profile.mode})"
        f" hotspot={hotspot}"
    )
    for w in plan.warnings:
        console.print(f"[yellow]live:[/] {escape(w)}")
    console.print(
        f"storage: root={cfg.storage.root} save_raw_vrs={cfg.storage.save_raw_vrs}"
        f" | dashboard: {'on' if cfg.dashboard.enabled else 'off'}"
    )


@app.command()
def run(
    path: Annotated[Path, typer.Argument(help="experiment.json")],
    mock: Annotated[
        bool, typer.Option("--mock", help="Use the synthetic source (no glasses)")
    ] = False,
    duration: Annotated[
        float | None,
        typer.Option(
            "--duration",
            "-d",
            help="Seconds to run (default: until Ctrl-C); with a non-realtime mock, data seconds",
        ),
    ] = None,
    report_every: Annotated[
        float, typer.Option("--report-every", help="Seconds between rate reports (0 = off)")
    ] = 2.0,
    json_summary: Annotated[
        bool, typer.Option("--json", help="Print the final summary as JSON")
    ] = False,
    no_storage: Annotated[
        bool, typer.Option("--no-storage", help="Do not write a session directory")
    ] = False,
    no_dashboard: Annotated[
        bool, typer.Option("--no-dashboard", help="Do not start the browser dashboard")
    ] = False,
    dashboard_port: Annotated[
        int | None, typer.Option("--dashboard-port", help="Override dashboard.port")
    ] = None,
) -> None:
    """Run an experiment."""
    try:
        exp = Experiment.from_json(
            path,
            mock=mock,
            duration_s=duration,
            persist=not no_storage,
            dashboard=False if no_dashboard else None,
        )
        _apply_dashboard_port(exp, dashboard_port)
    except AriaStreamLabError as exc:
        _fail(exc)
        return
    _drive(exp, report_every, json_summary)


@app.command()
def replay(
    path: Annotated[Path, typer.Argument(help="session directory or .vrs file")],
    speed: Annotated[
        float | None, typer.Option("--speed", "-s", help="1 = realtime, 2 = 2x, 0 = max speed")
    ] = None,
    start: Annotated[
        float | None, typer.Option("--start", help="Seconds into the recording")
    ] = None,
    end: Annotated[float | None, typer.Option("--end", help="Seconds into the recording")] = None,
    backend: Annotated[
        str | None, typer.Option("--backend", help="'tools' (pause/seek) or 'sdk' (Meta's engine)")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", "-c", help="experiment.json to apply (default: the session's own)"
        ),
    ] = None,
    streams: Annotated[
        str | None, typer.Option("--streams", help="Comma-separated subset of streams")
    ] = None,
    duration: Annotated[
        float | None,
        typer.Option(
            "--duration",
            "-d",
            help="Seconds to run; at --speed 0 this is recorded time (a window end)",
        ),
    ] = None,
    report_every: Annotated[float, typer.Option("--report-every")] = 2.0,
    json_summary: Annotated[bool, typer.Option("--json")] = False,
    no_storage: Annotated[
        bool, typer.Option("--no-storage", help="Do not write a new session directory")
    ] = False,
    no_dashboard: Annotated[
        bool, typer.Option("--no-dashboard", help="Do not start the browser dashboard")
    ] = False,
    dashboard_port: Annotated[
        int | None, typer.Option("--dashboard-port", help="Override dashboard.port")
    ] = None,
) -> None:
    """Replay a recorded session through the same pipeline."""
    try:
        exp = Experiment.replay(
            path,
            config=config,
            speed=speed,
            start_s=start,
            end_s=end,
            backend=backend,
            streams=None
            if streams is None
            else [s.strip() for s in streams.split(",") if s.strip()],
            duration_s=duration,
            persist=not no_storage,
            dashboard=False if no_dashboard else None,
        )
        _apply_dashboard_port(exp, dashboard_port)
    except AriaStreamLabError as exc:
        _fail(exc)
        return
    _drive(exp, report_every, json_summary)


@app.command()
def inspect(
    path: Annotated[Path, typer.Argument(help="session directory")],
    repair: Annotated[
        bool, typer.Option("--repair", help="Rebuild the timeline and mark a crashed session")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    """Show what a session directory contains (works on crashed sessions too)."""
    from ariastreamlab.storage.inspect import inspect_session, summary_as_dict

    try:
        s = inspect_session(path, repair=repair)
    except AriaStreamLabError as exc:
        _fail(exc)
        return
    if json_out:
        console.print_json(json.dumps(summary_as_dict(s), default=str))
        return
    m = s.manifest
    colour = {"completed": "green", "running": "yellow", "crashed": "red"}.get(m.status, "white")
    console.print(
        f"[bold]{s.root.name}[/]  status=[{colour}]{m.status}[/]  origin={m.origin}"
        f"  experiment={m.experiment}  participant={m.participant}"
    )
    console.print(
        f"started {m.started_utc}  duration {m.duration_s if m.duration_s is not None else '?'} s"
        f"  events {s.events}  timeline rows {s.timeline_rows}  total {s.total_bytes / 1e6:.1f} MB"
    )
    if m.raw.kind != "none":
        console.print(
            f"raw: {m.raw.kind} {', '.join(f for f, _ in s.raw_files) or m.raw.source or ''}"
        )
    table = Table(title="Stored streams")
    for col in ("stream", "kind", "format", "payload", "samples", "files", "MB", "span s"):
        table.add_column(col)
    for x in s.streams:
        table.add_row(
            x.stream_id,
            x.kind,
            x.fmt,
            x.payload,
            str(x.samples),
            str(x.files),
            f"{x.bytes / 1e6:.2f}",
            "" if x.duration_s is None else f"{x.duration_s:.1f}",
        )
    console.print(table)
    for pb in s.problems:
        console.print(f"[yellow]problem:[/] {pb}")
    if s.repaired:
        console.print("[green]repaired[/]")


def _apply_dashboard_port(exp: Experiment, port: int | None) -> None:
    if port is not None:
        exp.config = exp.config.model_copy(
            update={"dashboard": exp.config.dashboard.model_copy(update={"port": port})}
        )


def _drive(exp: Experiment, report_every: float, json_summary: bool) -> None:
    # with --json only the summary goes to stdout; progress goes to stderr so that
    # `ariastreamlab run … --json | jq` works
    out = err_console if json_summary else console
    try:
        with exp:
            assert exp.session is not None
            out.print(
                f"session [bold]{exp.session.session_id}[/] ({exp.session.origin})"
                + (f" → {exp.session_dir}" if exp.session_dir else "")
                + " — press Ctrl-C to stop"
            )
            if exp.dashboard_url:
                out.print(f"dashboard [link={exp.dashboard_url}]{exp.dashboard_url}[/]")
            import threading

            stop_report = threading.Event()

            def _report() -> None:
                while not stop_report.wait(report_every):
                    _print_rates(exp, out)

            if report_every > 0:
                threading.Thread(target=_report, name="cli-report", daemon=True).start()
            try:
                exp.run()
            finally:
                stop_report.set()
            _print_rates(exp, out)
            if json_summary:
                print(json.dumps(exp.summary(), default=str, indent=2))
    except AriaStreamLabError as exc:
        _fail(exc)


def _print_rates(exp: Experiment, out: Console = console) -> None:
    parts = []
    for sid, s in sorted(exp.stream_stats().items()):
        rate = "-" if s.effective_rate_hz is None else f"{s.effective_rate_hz:.1f}"
        nominal = "" if s.nominal_rate_hz is None else f"/{s.nominal_rate_hz:g}"
        extra = ""
        if s.transport_dropped:
            extra += f" drop={s.transport_dropped}"
        if s.out_of_order:
            extra += f" ooo={s.out_of_order}"
        parts.append(f"{sid}={s.received} ({rate}{nominal} Hz{extra})")
    out.print(f"[dim]{time.strftime('%H:%M:%S')}[/] " + "  ".join(parts))
    procs = exp.processor_stats()
    if procs:
        bits = []
        for pid, p in sorted(procs.items()):
            ms = "-" if p.exec_ms_mean is None else f"{p.exec_ms_mean:.2f}ms"
            dropped = sum(p.queue_dropped.values())
            flag = ""
            if p.errors:
                flag += f" [red]err={p.errors}[/]"
            if dropped:
                flag += f" drop={dropped}"
            if p.join_misses:
                flag += f" miss={p.join_misses}"
            bits.append(f"{pid}: {p.fired}→{p.outputs} ({ms}{flag})")
        out.print("[dim]   pipeline[/] " + "  ".join(bits))


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
