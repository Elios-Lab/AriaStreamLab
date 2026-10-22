"""The *Setup* page: environment, packages, checks, and the glasses' one-time steps."""

from __future__ import annotations

from typing import Any

from ariastreamlab.dashboard import setup as model
from ariastreamlab.dashboard.app import nav_header

__all__ = ["build_setup_page"]

_COLOURS = {
    "ok": "text-green-400",
    "warn": "text-amber-400",
    "fail": "text-red-400",
    "skip": "opacity-60",
}


def build_setup_page(dash: Any, ui: Any) -> None:
    """Render the Setup page (state lives on ``dash.setup_run``: one command at a time)."""
    with nav_header(ui, "setup"):
        env_label = ui.label("").classes("text-xs opacity-70")

    env = model.environment()
    env_label.text = f"Python {env['python']} · {env['platform']}"

    with ui.row().classes("w-full px-2 gap-2 items-start flex-nowrap"):
        # ================================================================ left column
        with ui.column().classes("grow gap-2"):
            # ---- environment ------------------------------------------------------------
            with ui.card().classes("w-full p-3"):
                ui.label("Environment").classes("font-bold")
                note = env["sdk_python_note"]
                ui.markdown(
                    f"Python **{env['python']}** — `{env['executable']}`  \n"
                    f"{'virtual environment' if env['virtualenv'] else 'system interpreter'}: "
                    f"`{env['prefix']}`  \n"
                    f"AriaStreamLab **{env['ariastreamlab']}** · working directory "
                    f"`{env['cwd']}`  \n"
                    f"aria_gen2 CLI: `{env['aria_gen2_cli'] or 'not installed'}` · streaming "
                    f"certificate: {'present' if env['has_streaming_cert'] else 'none yet'} "
                    f"(`{env['certs_dir']}`)"
                ).classes("text-sm break-all")
                if note:
                    ui.label(note).classes("text-sm text-amber-400")
                    ui.markdown(
                        "The application cannot replace the interpreter it runs in. Create a "
                        "compatible environment with the bootstrap script and start the "
                        "application from it:\n\n"
                        "```\nbash scripts/install.sh        # macOS / Linux\n"
                        "powershell scripts/install.ps1  # Windows\n```"
                    ).classes("text-sm")

            # ---- packages ---------------------------------------------------------------
            with ui.card().classes("w-full p-3"):
                with ui.row().classes("items-center gap-3"):
                    ui.label("Packages").classes("font-bold")
                    ui.button("refresh", icon="refresh", on_click=lambda: refresh_packages()).props(
                        "flat dense"
                    )
                pkg_table = (
                    ui.table(
                        columns=[
                            {"name": "name", "label": "package", "field": "name", "align": "left"},
                            {"name": "version", "label": "installed", "field": "version"},
                            {
                                "name": "purpose",
                                "label": "purpose",
                                "field": "purpose",
                                "align": "left",
                            },
                            {"name": "pip", "label": "pip spec", "field": "pip", "align": "left"},
                        ],
                        rows=[],
                        row_key="name",
                    )
                    .classes("w-full")
                    .props("dense flat")
                )

            # ---- checks -----------------------------------------------------------------
            with ui.card().classes("w-full p-3"):
                with ui.row().classes("items-center gap-3"):
                    ui.label("Checks (doctor)").classes("font-bold")
                    ui.button(
                        "run checks", icon="health_and_safety", on_click=lambda: run_doctor(False)
                    ).props("dense")
                    ui.button(
                        "run checks with the glasses",
                        icon="visibility",
                        on_click=lambda: run_doctor(True),
                    ).props("flat dense").tooltip(
                        "Adds `aria_gen2 device list` and `auth check` (slow; the glasses must be "
                        "connected over USB)"
                    )
                    checks_status = ui.label("").classes("text-xs opacity-70")
                checks_col = ui.column().classes("w-full gap-0 font-mono text-sm")

        # ================================================================ right column
        with ui.column().classes("w-[560px] max-w-[45%] shrink-0 gap-2 sticky top-[70px]"):
            with ui.card().classes("w-full p-3"):
                ui.label("Install and configure").classes("font-bold")
                ui.label(
                    "Each button runs one command inside this environment and shows its output "
                    "below. Pairing and certificates need the glasses on the USB cable."
                ).classes("text-xs opacity-70")
                buttons: dict[str, Any] = {}
                with ui.column().classes("w-full gap-1"):
                    for action in model.ACTIONS:
                        with ui.row().classes("items-center gap-2 w-full"):
                            buttons[action.key] = (
                                ui.button(action.label, on_click=lambda a=action: start_action(a))
                                .props("dense outline")
                                .classes("min-w-[240px] text-left")
                            )
                            ui.label(action.description).classes("text-xs opacity-70 grow")
            with ui.card().classes("w-full p-3"):
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.label("Output").classes("font-bold")
                    run_status = ui.label("").classes("text-xs opacity-70 grow")
                    ui.button("cancel", icon="cancel", on_click=lambda: cancel_run()).props(
                        "flat dense color=red"
                    )
                log = ui.log(max_lines=400).classes("w-full h-[380px] text-xs")

    # ---- confirmation dialog -----------------------------------------------------------------
    with ui.dialog() as confirm_dialog, ui.card():
        confirm_text = ui.label("")
        pending: dict[str, Any] = {"action": None}
        with ui.row():

            def _yes() -> None:
                confirm_dialog.close()
                a = pending["action"]
                if a is not None:
                    launch(a)

            ui.button("continue", on_click=_yes)
            ui.button("cancel", on_click=confirm_dialog.close).props("flat")

    # ---- behaviour ---------------------------------------------------------------------------
    shown_lines = [0]
    shown_run: list[Any] = [None]

    def refresh_packages() -> None:
        rows = []
        for p in model.packages():
            rows.append(
                {
                    "name": p.name + ("" if p.installed else ("  ✗" if p.required else "  –")),
                    "version": p.version or ("MISSING" if p.required else "not installed"),
                    "purpose": p.purpose,
                    "pip": p.pip,
                }
            )
        pkg_table.rows = rows
        pkg_table.update()

    def run_doctor(with_device: bool) -> None:
        checks_status.text = "running…"
        checks_col.clear()

        def _work() -> None:
            result = model.checks(with_device=with_device)
            dash.setup_checks = result

        import threading

        threading.Thread(target=_work, daemon=True).start()

    def render_checks() -> None:
        result = getattr(dash, "setup_checks", None)
        if result is None or getattr(dash, "_setup_checks_shown", None) is result:
            return
        dash._setup_checks_shown = result
        checks_col.clear()
        with checks_col:
            for c in result:
                with ui.row().classes("items-start gap-2 w-full"):
                    ui.label(c.status.upper()).classes(f"w-[48px] {_COLOURS.get(c.status, '')}")
                    ui.label(c.name).classes("w-[220px]")
                    ui.label(c.detail).classes("grow opacity-80 break-all")
        n_fail = sum(1 for c in result if c.status == "fail")
        n_warn = sum(1 for c in result if c.status == "warn")
        checks_status.text = (
            f"{len(result)} checks · {n_fail} failed · {n_warn} warnings"
            if n_fail or n_warn
            else f"{len(result)} checks, all green"
        )

    def start_action(action: model.Action) -> None:
        current = getattr(dash, "setup_run", None)
        if current is not None and current.running:
            ui.notify("a command is still running", type="warning")
            return
        if action.confirm:
            pending["action"] = action
            confirm_text.text = action.confirm
            confirm_dialog.open()
            return
        launch(action)

    def launch(action: model.Action) -> None:
        try:
            argv = action.argv()
        except RuntimeError as exc:
            ui.notify(str(exc), type="negative")
            return
        log.clear()
        shown_lines[0] = 0
        run = model.CommandRun(argv).start(timeout_s=action.timeout_s)
        dash.setup_run = run
        shown_run[0] = run
        log.push(f"$ {' '.join(argv)}")
        run_status.text = f"running: {action.label}"

    def cancel_run() -> None:
        run = getattr(dash, "setup_run", None)
        if run is not None and run.running:
            run.cancel()
            ui.notify("cancelled")

    def poll() -> None:
        render_checks()
        run: model.CommandRun | None = getattr(dash, "setup_run", None)
        if run is None:
            return
        if shown_run[0] is not run:
            log.clear()
            shown_lines[0] = 0
            shown_run[0] = run
            log.push(f"$ {' '.join(run.argv)}")
        lines = run.lines
        if len(lines) > shown_lines[0]:
            for line in lines[shown_lines[0] :]:
                log.push(line)
            shown_lines[0] = len(lines)
        if run.running:
            return
        if run_status.text.startswith("running"):
            if run.error:
                run_status.text = f"failed: {run.error}"
            elif run.ok:
                run_status.text = "done"
            else:
                run_status.text = f"exited with code {run.returncode}"
            refresh_packages()  # an install may have changed what is importable
            env_now = model.environment()
            env_label.text = f"Python {env_now['python']} · {env_now['platform']}"

    refresh_packages()
    ui.timer(0.5, poll)
    ui.timer(0.2, lambda: run_doctor(False), once=True)
