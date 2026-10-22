"""The application's *Home* and *Sessions* pages.

The designer and the monitor have their own modules; every page is rendered inside the
shared header of :func:`~.app.nav_header`.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any

from ariastreamlab.dashboard.app import nav_header
from ariastreamlab.dashboard.sessions import list_sessions, session_details

__all__ = ["build_home_page", "build_sessions_page"]


def build_home_page(dash: Any, ui: Any) -> None:
    """The start screen: what to do next, and the state of the current session."""
    with nav_header(ui, "home"):
        version_label = ui.label("").classes("text-xs opacity-70")
    try:
        from importlib.metadata import version

        version_label.text = f"v{version('ariastreamlab')}"
    except Exception:
        pass

    with ui.column().classes("w-full items-center gap-6 pt-10"):
        ui.label("AriaStreamLab").classes("text-4xl font-bold")
        ui.label(
            "Experiments with Meta Project Aria Gen 2 glasses: design, record, watch, annotate, "
            "replay."
        ).classes("text-lg opacity-80")
        status = ui.markdown("").classes("text-sm")
        with ui.row().classes("gap-4 flex-wrap justify-center pt-4"):
            cards = (
                (
                    "Setup",
                    "build",
                    "/setup",
                    "Check this computer, install Meta's SDK, pair the glasses, install the "
                    "streaming certificates, start the hotspot.",
                ),
                (
                    "Design an experiment",
                    "science",
                    "/design",
                    "Pick a preset, choose streams and processors, validate, save — and run it "
                    "from the same page, with or without the glasses.",
                ),
                (
                    "Monitor",
                    "monitor_heart",
                    "/monitor",
                    "Images with the gaze overlay, signals, rates and drops, device health, "
                    "markers and intervals, live parameter changes, replay controls.",
                ),
                (
                    "Sessions",
                    "folder",
                    "/sessions",
                    "Browse recorded sessions, read their quality summaries and events, "
                    "replay one through the pipeline, open its folder.",
                ),
            )
            for title, icon, path, text in cards:
                with (
                    ui.card()
                    .classes("w-[300px] p-5 cursor-pointer hover:bg-[#2a2a2a]")
                    .on("click", lambda _e, p=path: ui.navigate.to(p))
                ):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon(icon).classes("text-3xl text-primary")
                        ui.label(title).classes("text-xl font-bold")
                    ui.label(text).classes("text-sm opacity-80")
        with ui.expansion("Command line", icon="terminal").classes("w-[1000px] max-w-full"):
            ui.markdown(
                "```\n"
                "ariastreamlab run experiment.json --duration 300   # record with the glasses\n"
                "ariastreamlab run experiment.json --mock           # synthetic data\n"
                "ariastreamlab replay sessions/<folder> --speed 0   # re-process a recording\n"
                "ariastreamlab inspect sessions/<folder> --events   # what was written\n"
                "ariastreamlab doctor --device                      # environment and pairing\n"
                "```"
            )

    def refresh() -> None:
        rs = dash.run_state()
        st = dash.state
        if st is not None and st.session:
            sess = st.session
            status.content = (
                f"current session: **{sess.get('experiment', '?')}** / "
                f"{sess.get('participant', '?')} · {sess.get('origin', '?')} · "
                f"{sess.get('state', '')} — [monitor](/monitor)"
            )
        elif rs is not None:
            status.content = f"last run: **{rs['state']}** · {rs['experiment']} · {rs['origin']}"
            if rs.get("error"):
                status.content += f" — {rs['error']}"
        else:
            status.content = "_no session yet — design one, or open a recorded session_"

    ui.timer(1.0, refresh)
    ui.timer(0.05, refresh, once=True)


def build_sessions_page(dash: Any, ui: Any) -> None:
    """Browse the session directories under the experiment's storage root."""
    with nav_header(ui, "sessions"):
        count_label = ui.label("").classes("text-sm opacity-80")

    selected: dict[str, Path | None] = {"path": None}

    with ui.row().classes("w-full px-2 gap-2 items-start flex-nowrap"):
        with ui.column().classes("grow gap-2"), ui.card().classes("w-full p-3"):
            with ui.row().classes("items-end gap-2 w-full"):
                root_in = ui.input("sessions folder", value=str(dash.sessions_root)).classes("grow")
                ui.button("refresh", icon="refresh", on_click=lambda: reload()).props("flat")
            table = (
                ui.table(
                    columns=[
                        {"name": "name", "label": "session", "field": "name", "align": "left"},
                        {"name": "experiment", "label": "experiment", "field": "experiment"},
                        {"name": "participant", "label": "participant", "field": "participant"},
                        {"name": "origin", "label": "origin", "field": "origin"},
                        {"name": "status", "label": "status", "field": "status"},
                        {"name": "started", "label": "started (UTC)", "field": "started"},
                        {"name": "duration", "label": "duration", "field": "duration"},
                        {"name": "streams", "label": "streams", "field": "streams"},
                        {"name": "events", "label": "events", "field": "events"},
                        {"name": "warnings", "label": "warnings", "field": "warnings"},
                    ],
                    rows=[],
                    row_key="path",
                    selection="single",
                    pagination=15,
                )
                .classes("w-full")
                .props("dense flat")
            )
        with ui.column().classes("w-[560px] max-w-[45%] shrink-0 gap-2 sticky top-[70px]"):
            with ui.card().classes("w-full p-3") as detail_card:
                detail_title = ui.label("select a session").classes("font-bold")
                detail_meta = ui.markdown("").classes("text-sm break-all")
                with ui.row().classes("items-end gap-2 flex-wrap"):
                    speed = ui.select([0, 0.5, 1, 2, 4], value=1, label="speed").classes(
                        "w-[100px]"
                    )
                    speed.tooltip("0 = as fast as possible (every sample, back-pressure)")

                    def _replay() -> None:
                        p = selected["path"]
                        if p is None:
                            return
                        try:
                            dash.launch_replay(p, speed=float(speed.value or 0))
                        except Exception as exc:
                            ui.notify(str(exc), type="negative")
                            return
                        ui.notify("replay started — opening the monitor")
                        ui.navigate.to("/monitor")

                    ui.button("replay", icon="play_arrow", on_click=_replay)

                    def _open_folder() -> None:
                        p = selected["path"]
                        if p is not None:
                            webbrowser.open(p.as_uri())  # Finder / Explorer / file manager

                    ui.button("open folder", icon="folder_open", on_click=_open_folder).props(
                        "flat"
                    )
                    ui.button("stop current run", icon="stop", on_click=dash.stop_run).props(
                        "flat color=red"
                    )
                summary_code = ui.code("", language="text").classes(
                    "w-full text-xs overflow-x-auto"
                )
                with ui.expansion("events").classes("w-full"):
                    events_table = (
                        ui.table(
                            columns=[
                                {"name": "t_s", "label": "t (s)", "field": "t_s"},
                                {"name": "kind", "label": "kind", "field": "kind", "align": "left"},
                                {"name": "name", "label": "name", "field": "name", "align": "left"},
                                {"name": "source", "label": "time", "field": "source"},
                                {
                                    "name": "payload",
                                    "label": "payload",
                                    "field": "payload",
                                    "align": "left",
                                },
                            ],
                            rows=[],
                            row_key="t_s",
                            pagination=10,
                        )
                        .classes("w-full")
                        .props("dense flat")
                    )
                with ui.expansion("intervals").classes("w-full"):
                    intervals_table = (
                        ui.table(
                            columns=[
                                {"name": "name", "label": "name", "field": "name", "align": "left"},
                                {"name": "start_s", "label": "start (s)", "field": "start_s"},
                                {"name": "end_s", "label": "end (s)", "field": "end_s"},
                                {
                                    "name": "duration_s",
                                    "label": "duration (s)",
                                    "field": "duration_s",
                                },
                                {
                                    "name": "payload",
                                    "label": "payload",
                                    "field": "payload",
                                    "align": "left",
                                },
                            ],
                            rows=[],
                            row_key="start_s",
                            pagination=10,
                        )
                        .classes("w-full")
                        .props("dense flat")
                    )
                problems_md = ui.markdown("").classes("text-sm text-amber-400")
            detail_card.visible = True

    def reload() -> None:
        root = Path(str(root_in.value or "")).expanduser()
        rows = list_sessions(root)
        table.rows = [r.as_row() for r in rows]
        table.update()
        count_label.text = f"{len(rows)} session(s) under {root}"

    def show(path: Path) -> None:
        selected["path"] = path
        d = session_details(path)
        m = d.manifest
        detail_title.text = path.name
        detail_meta.content = (
            f"**{m.get('experiment', '?')}** / {m.get('participant', '?')} · {m.get('origin', '?')}"
            f" · {m.get('status', '?')}  \nstarted {m.get('started_utc', '?')} · "
            f"duration {m.get('duration_s', '?')} s · on disk {d.size_text}  \n"
            + (f"raw: {', '.join(d.raw_files)}  \n" if d.raw_files else "")
            + f"`{path}`"
        )
        summary_code.content = d.summary_text or "(no quality summary)"
        events_table.rows = d.events
        events_table.update()
        intervals_table.rows = d.intervals
        intervals_table.update()
        problems_md.content = "\n".join(f"- {p}" for p in d.problems)

    def _selected(e: Any) -> None:
        sel = e.selection or []
        if sel:
            show(Path(sel[0]["path"]))

    table.on_select(_selected)

    def _row_click(e: Any) -> None:  # a click anywhere on the row selects it
        args = e.args if isinstance(e.args, list) else []
        row = args[1] if len(args) > 1 and isinstance(args[1], dict) else None
        if row and row.get("path"):
            table.selected = [row]
            show(Path(row["path"]))

    table.on("rowClick", _row_click)
    reload()
