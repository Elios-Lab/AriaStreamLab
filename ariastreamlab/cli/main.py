"""The ``ariastreamlab`` command line.

Commands available in this phase: ``streams``, ``presets``, ``schema``, ``inspect-config``,
``run --mock``. ``doctor``, ``devices``, ``replay`` and ``inspect`` are added by the phases
that implement the subsystems they need (see docs/ARCHITECTURE.md §12).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ariastreamlab import __version__
from ariastreamlab.api import Experiment
from ariastreamlab.config.loader import dump_resolved, load_config, load_preset, preset_names
from ariastreamlab.config.schema import export_json_schema_text
from ariastreamlab.core.errors import AriaStreamLabError
from ariastreamlab.core.registry import REGISTRY

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
    err_console.print(f"[bold red]error:[/] {exc}")
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
def presets() -> None:
    """List the bundled configuration presets (use with "extends": "preset:<name>")."""
    for name in preset_names():
        cfg = load_preset(name)
        console.print(
            f"[bold]{name}[/]: {cfg.experiment.description or '(no description)'}"
            f" — streams: {', '.join(cfg.enabled_streams()) or 'none'}"
        )


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
        table = Table(title="Processors")
        for col in ("id", "type", "inputs", "outputs", "policy", "save"):
            table.add_column(col)
        for p in cfg.processors:
            table.add_row(
                p.id,
                p.type,
                ", ".join(p.inputs),
                ", ".join(p.outputs),
                f"{p.execution.drop_policy.value}/{p.execution.queue_size}",
                "yes" if p.save_output else "no",
            )
        console.print(table)
    conn = cfg.device.connection
    console.print(
        f"device: interface={conn.streaming_interface}"
        f" batch_period_ms={conn.effective_batch_period_ms} profile.mode={cfg.device.profile.mode}"
    )
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
        typer.Option("--duration", "-d", help="Seconds to run (default: until Ctrl-C)"),
    ] = None,
    report_every: Annotated[
        float, typer.Option("--report-every", help="Seconds between rate reports (0 = off)")
    ] = 2.0,
    json_summary: Annotated[
        bool, typer.Option("--json", help="Print the final summary as JSON")
    ] = False,
) -> None:
    """Run an experiment."""
    try:
        exp = Experiment.from_json(path, mock=mock, duration_s=duration)
    except AriaStreamLabError as exc:
        _fail(exc)
        return
    try:
        with exp:
            assert exp.session is not None
            console.print(
                f"session [bold]{exp.session.session_id}[/] ({exp.session.origin})"
                " — press Ctrl-C to stop"
            )
            import threading

            stop_report = threading.Event()

            def _report() -> None:
                while not stop_report.wait(report_every):
                    _print_rates(exp)

            if report_every > 0:
                threading.Thread(target=_report, name="cli-report", daemon=True).start()
            try:
                exp.run()
            finally:
                stop_report.set()
            _print_rates(exp)
            if json_summary:
                console.print_json(json.dumps(exp.summary(), default=str))
    except AriaStreamLabError as exc:
        _fail(exc)


def _print_rates(exp: Experiment) -> None:
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
    console.print(f"[dim]{time.strftime('%H:%M:%S')}[/] " + "  ".join(parts))


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
