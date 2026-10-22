"""The ``/design`` page: forms over a :class:`~ariastreamlab.dashboard.designer.Designer`.

Every widget writes straight into the designer's document; a timer re-validates the document
and refreshes the side panel (problems, live plan, JSON preview) half a second after the last
change. Structural edits (streams toggled, processors added/removed) rebuild their section.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ariastreamlab.config.models import (
    DashboardConfig,
    DeviceConfig,
    ExecutionConfig,
    ExperimentInfo,
    LoggingConfig,
    MockConfig,
    ReplayConfig,
    StorageConfig,
)
from ariastreamlab.dashboard.designer import Designer, FieldSpec, fields_of

__all__ = ["build_design_page", "field_widget"]


def _label(spec: FieldSpec) -> str:
    return spec.name.replace("_", " ")


def field_widget(
    ui: Any, spec: FieldSpec, container: dict[str, Any], changed: Callable[[], None]
) -> Any:
    """One widget for one scalar field, writing into ``container[spec.name]``."""
    value = container.get(spec.name, spec.default)
    w: Any
    if spec.kind == "bool":
        w = ui.switch(_label(spec), value=bool(value))

        def _b(e: Any) -> None:
            container[spec.name] = bool(e.value)
            changed()

        w.on_value_change(_b)
    elif spec.kind == "choice":
        opts = list(spec.choices)
        w = ui.select(opts, value=value, label=_label(spec), clearable=spec.optional).classes(
            "min-w-[160px]"
        )

        def _c(e: Any) -> None:
            container[spec.name] = e.value
            changed()

        w.on_value_change(_c)
    elif spec.kind in ("int", "float"):
        w = ui.number(
            _label(spec),
            value=value,
            min=spec.minimum,
            max=spec.maximum,
            step=1 if spec.kind == "int" else None,
            format="%d" if spec.kind == "int" else None,
        ).classes("w-[140px]")

        def _n(e: Any) -> None:
            v = e.value
            if v is None or v == "":
                container[spec.name] = None if spec.optional else spec.default
            else:
                container[spec.name] = int(v) if spec.kind == "int" else float(v)
            changed()

        w.on_value_change(_n)
    elif spec.kind == "list":
        w = ui.input(_label(spec), value=", ".join(str(x) for x in (value or []))).classes(
            "min-w-[220px]"
        )

        def _l(e: Any) -> None:
            container[spec.name] = [x.strip() for x in str(e.value or "").split(",") if x.strip()]
            changed()

        w.on_value_change(_l)
    else:
        w = ui.input(_label(spec), value="" if value is None else str(value)).classes(
            "min-w-[200px]"
        )

        def _s(e: Any) -> None:
            text = str(e.value or "")
            container[spec.name] = (None if spec.optional else "") if text == "" else text
            changed()

        w.on_value_change(_s)
    if spec.description:
        w.tooltip(spec.description)
    return w


def _model_form(
    ui: Any,
    model: type,
    container: dict[str, Any],
    changed: Callable[[], None],
    *,
    skip: tuple[str, ...] = (),
) -> None:
    """Widgets for every field of ``model``; nested models become expansions."""
    scalars = [f for f in fields_of(model) if f.kind != "model" and f.name not in skip]
    nested = [f for f in fields_of(model) if f.kind == "model" and f.name not in skip]
    if scalars:
        with ui.row().classes("items-end gap-3 flex-wrap"):
            for spec in scalars:
                field_widget(ui, spec, container, changed)
    for spec in nested:
        sub = container.setdefault(spec.name, dict(spec.default or {}))
        with ui.expansion(_label(spec)).classes("w-full"):
            assert spec.model is not None
            _model_form(ui, spec.model, sub, changed)


def build_design_page(dash: Any, ui: Any) -> None:
    """Render the designer page for ``dash.designer``."""
    from ariastreamlab.dashboard.app import nav_header

    designer: Designer = dash.designer
    rev = {"shown": -1, "current": 0}

    def changed() -> None:
        rev["current"] += 1

    # ---- header ----------------------------------------------------------------------
    with nav_header(ui, "design"), ui.row().classes("items-center gap-2"):
        preset = ui.select(list(Designer.presets()), value="base", label="preset").classes(
            "w-[200px]"
        )

        def _start() -> None:
            dash.designer = Designer.from_preset(str(preset.value), base_dir=designer.base_dir)
            ui.navigate.to("/design")

        ui.button("start from preset", on_click=_start).props("flat color=white")
        load_path = ui.input("load file", placeholder="path/to/experiment.json").classes(
            "w-[260px]"
        )

        def _load() -> None:
            p = Path(str(load_path.value or "")).expanduser()
            if not p.is_absolute():
                p = designer.base_dir / p
            try:
                dash.designer = Designer.from_file(p)
            except Exception as exc:
                ui.notify(f"cannot load {p}: {exc}", type="negative")
                return
            ui.navigate.to("/design")

        ui.button("load", on_click=_load).props("flat color=white")

    doc = designer.doc
    with ui.row().classes("w-full px-2 gap-2 items-start flex-nowrap"):
        # ================================================================ left column
        with ui.column().classes("w-2/3 gap-2"):
            # ---- experiment ------------------------------------------------------------
            with ui.card().classes("w-full p-3"):
                ui.label("Experiment").classes("font-bold")
                _model_form(ui, ExperimentInfo, doc["experiment"], changed)

            # ---- streams ---------------------------------------------------------------
            with ui.card().classes("w-full p-3"):
                ui.label("Streams").classes("font-bold")
                ui.label(
                    "What the glasses record and deliver. Grey entries exist in the VRS only "
                    "(no live callback in the SDK); the live plan on the right says what "
                    "profile9 actually streams."
                ).classes("text-xs opacity-70")

                @ui.refreshable  # type: ignore[untyped-decorator]
                def streams_section() -> None:
                    for entry in Designer.stream_entries():
                        enabled = designer.stream_enabled(entry.key)
                        with ui.row().classes("items-center gap-3 w-full"):
                            sw = ui.switch(entry.key, value=enabled).classes("w-[170px]")

                            def _toggle(e: Any, key: str = entry.key) -> None:
                                designer.set_stream_enabled(key, bool(e.value))
                                changed()
                                streams_section.refresh()
                                processors_section.refresh()

                            sw.on_value_change(_toggle)
                            members = ", ".join(entry.members) if len(entry.members) > 1 else ""
                            d = entry.descriptor
                            meta = (
                                f"{d.category}"
                                + (f" · {d.nominal_rate_hz:g} Hz" if d.nominal_rate_hz else "")
                                + ("" if entry.live else " · VRS only")
                            )
                            ui.label(meta).classes(
                                "text-xs w-[170px] "
                                + ("opacity-80" if entry.live else "opacity-40")
                            )
                            if members:
                                ui.label(members).classes("text-xs opacity-60 w-[260px]")
                            if enabled:
                                for spec in entry.parameters:
                                    cur = designer.stream_param(entry.key, spec.name)
                                    if spec.choices is not None:
                                        w = ui.select(
                                            list(spec.choices), value=cur, label=spec.name
                                        ).classes("w-[150px]")
                                    else:
                                        w = ui.number(
                                            spec.name,
                                            value=cur,
                                            min=spec.minimum,
                                            max=spec.maximum,
                                        ).classes("w-[150px]")
                                    if spec.description:
                                        w.tooltip(spec.description)

                                    def _param(
                                        e: Any, key: str = entry.key, name: str = spec.name
                                    ) -> None:
                                        designer.set_stream_param(key, name, e.value)
                                        changed()

                                    w.on_value_change(_param)
                            if d.description:
                                sw.tooltip(d.description)

                streams_section()

            # ---- processors ------------------------------------------------------------
            with ui.card().classes("w-full p-3"):
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.label("Processors").classes("font-bold")
                    types = {e.type: f"{e.type} — {e.description}" for e in designer.catalogue()}
                    add_select = ui.select(types, label="add a processor", with_input=True).classes(
                        "grow"
                    )

                    def _add() -> None:
                        if not add_select.value:
                            return
                        designer.add_processor(str(add_select.value))
                        add_select.value = None
                        changed()
                        processors_section.refresh()

                    ui.button("add", on_click=_add, icon="add").props("flat")

                    def _rescan() -> None:
                        designer.catalogue(refresh=True)
                        add_select.options = {
                            e.type: f"{e.type} — {e.description}" for e in designer.catalogue()
                        }
                        add_select.update()
                        pp = designer.processors_path()
                        ui.notify(
                            f"{len(designer.catalogue())} processor types"
                            + (f" (files from {pp})" if pp else "")
                        )

                    ui.button("rescan", on_click=_rescan, icon="refresh").props("flat").tooltip(
                        "Re-read processors/ next to the experiment file and installed plugins"
                    )

                @ui.refreshable  # type: ignore[untyped-decorator]
                def processors_section() -> None:
                    procs = doc["processors"]
                    if not procs:
                        ui.label("no processors — raw streams only").classes("text-xs opacity-60")
                    for i, p in enumerate(procs):
                        entry = designer.entry_for(p["type"])
                        with ui.card().classes("w-full p-2 bg-[#262626]"):
                            with ui.row().classes("items-end gap-3 w-full flex-wrap"):
                                id_in = ui.input("id", value=p["id"]).classes("w-[160px]")

                                def _id(e: Any, p: dict[str, Any] = p) -> None:
                                    p["id"] = str(e.value or "")
                                    changed()

                                id_in.on_value_change(_id)
                                ui.label(p["type"]).classes("font-mono text-sm")
                                if entry is None:
                                    ui.label("unknown type").classes("text-xs text-red-400")
                                else:
                                    ui.label(f"v{entry.version} · {entry.origin}").classes(
                                        "text-xs opacity-60"
                                    )
                                en = ui.switch("enabled", value=p.get("enabled", True))

                                def _en(e: Any, p: dict[str, Any] = p) -> None:
                                    p["enabled"] = bool(e.value)
                                    changed()
                                    processors_section.refresh()

                                en.on_value_change(_en)
                                so = ui.switch("save output", value=p.get("save_output", True))

                                def _so(e: Any, p: dict[str, Any] = p) -> None:
                                    p["save_output"] = bool(e.value)
                                    changed()

                                so.on_value_change(_so)
                                ui.space()
                                for icon, delta in (("arrow_upward", -1), ("arrow_downward", 1)):

                                    def _mv(i: int = i, delta: int = delta) -> None:
                                        designer.move_processor(i, delta)
                                        changed()
                                        processors_section.refresh()

                                    ui.button(icon=icon, on_click=_mv).props("flat dense")

                                def _rm(i: int = i) -> None:
                                    designer.remove_processor(i)
                                    changed()
                                    processors_section.refresh()

                                ui.button(icon="delete", on_click=_rm).props("flat dense color=red")
                            if entry and entry.description:
                                ui.label(entry.description).classes("text-xs opacity-70")
                            with ui.row().classes("items-end gap-3 w-full flex-wrap"):
                                options = designer.input_options(i)
                                # keep inputs that are no longer available visible (as errors)
                                for x in p["inputs"]:
                                    if x not in options:
                                        options.append(x)
                                inputs = (
                                    ui.select(
                                        options,
                                        value=list(p["inputs"]),
                                        multiple=True,
                                        label="inputs",
                                    )
                                    .classes("min-w-[280px]")
                                    .props("use-chips")
                                )

                                def _in(e: Any, p: dict[str, Any] = p) -> None:
                                    p["inputs"] = list(e.value or [])
                                    changed()

                                inputs.on_value_change(_in)
                                inputs.tooltip(
                                    "First input is the primary one (it triggers the processor)"
                                )
                                outputs = ui.input(
                                    "outputs", value=", ".join(p["outputs"])
                                ).classes("w-[220px]")

                                def _out(e: Any, p: dict[str, Any] = p) -> None:
                                    p["outputs"] = [
                                        x.strip()
                                        for x in str(e.value or "").split(",")
                                        if x.strip()
                                    ]
                                    changed()
                                    # other processors may read these: rebuild their input lists
                                    # lazily on the next structural refresh

                                outputs.on_value_change(_out)
                                outputs.tooltip("Derived stream id(s), comma separated")
                            if entry is not None and entry.parameters:
                                with ui.row().classes("items-end gap-3 w-full flex-wrap"):
                                    ui.label("parameters").classes("text-xs opacity-70 w-full")
                                    for spec in entry.parameters:
                                        field_widget(ui, spec, p["parameters"], changed)
                            with ui.expansion("execution").classes("w-full text-sm"):
                                _model_form(ui, ExecutionConfig, p["execution"], changed)

                processors_section()

            # ---- device / storage / dashboard / mock ----------------------------------
            with ui.card().classes("w-full p-3"):
                ui.label("Device").classes("font-bold")
                ui.label(
                    "How the laptop talks to the glasses (ignored by --mock and replays)."
                ).classes("text-xs opacity-70")
                _model_form(ui, DeviceConfig, doc["device"], changed)
            with ui.card().classes("w-full p-3"):
                ui.label("Storage").classes("font-bold")
                _model_form(ui, StorageConfig, doc["storage"], changed)
            with ui.card().classes("w-full p-3"):
                ui.label("Dashboard").classes("font-bold")
                _model_form(ui, DashboardConfig, doc["dashboard"], changed, skip=("show",))
                with ui.row().classes("items-end gap-3"):

                    @ui.refreshable  # type: ignore[untyped-decorator]
                    def show_select() -> None:
                        opts = designer.all_stream_ids()
                        cur = doc["dashboard"].get("show")
                        sel = (
                            ui.select(
                                opts,
                                value=list(cur) if cur else [],
                                multiple=True,
                                label="show (empty ⇒ everything)",
                            )
                            .classes("min-w-[320px]")
                            .props("use-chips")
                        )

                        def _show(e: Any) -> None:
                            doc["dashboard"]["show"] = list(e.value) if e.value else None
                            changed()

                        sel.on_value_change(_show)

                    show_select()
                    ui.button(icon="refresh", on_click=show_select.refresh).props(
                        "flat dense"
                    ).tooltip("Refresh the list after changing streams or processors")
            with ui.expansion("Mock source, replay, logging").classes("w-full"):
                with ui.card().classes("w-full p-3"):
                    ui.label("Mock").classes("font-bold")
                    _model_form(ui, MockConfig, doc["mock"], changed)
                with ui.card().classes("w-full p-3"):
                    ui.label("Replay").classes("font-bold")
                    _model_form(ui, ReplayConfig, doc["replay"], changed)
                with ui.card().classes("w-full p-3"):
                    ui.label("Logging").classes("font-bold")
                    _model_form(ui, LoggingConfig, doc["logging"], changed)
                with ui.card().classes("w-full p-3"):
                    pp = ui.input(
                        "processors_path", value=str(doc.get("processors_path") or "")
                    ).classes("w-full")

                    def _pp(e: Any) -> None:
                        text = str(e.value or "").strip()
                        if text:
                            doc["processors_path"] = text
                        else:
                            doc.pop("processors_path", None)
                        designer.catalogue(refresh=True)
                        changed()

                    pp.on_value_change(_pp)
                    pp.tooltip("Folder with processor files, relative to the experiment file")

        # ================================================================ right column
        with ui.column().classes("w-1/3 gap-2 sticky top-[70px]"):
            with ui.card().classes("w-full p-3"):
                status = ui.label("").classes("font-bold")
                problems = ui.markdown("")
                plan_md = ui.markdown("").classes("text-xs")
            with ui.card().classes("w-full p-3"):
                with ui.row().classes("items-end gap-2 w-full"):
                    save_path = ui.input("save as", value="experiment.json").classes("grow")

                    def _save() -> None:
                        rep = designer.validate()
                        if not rep.ok:
                            ui.notify("fix the problems first", type="warning")
                            return
                        try:
                            p = designer.save(str(save_path.value or "experiment.json"))
                        except OSError as exc:
                            ui.notify(f"cannot write: {exc}", type="negative")
                            return
                        ui.notify(f"saved {p}", type="positive")
                        dash.last_saved = p

                    ui.button("save", on_click=_save, icon="save")
                ui.label(f"relative paths are under {designer.base_dir}").classes(
                    "text-xs opacity-60"
                )
            with ui.card().classes("w-full p-3"):
                ui.label("Run").classes("font-bold")
                with ui.row().classes("items-end gap-3 w-full"):
                    mock = ui.switch("mock (no glasses)", value=not dash.sdk_available())
                    duration = ui.number("duration s", value=None, min=1).classes("w-[120px]")
                    duration.tooltip("empty: until you press stop")

                    def _run() -> None:
                        rep = designer.validate()
                        if not rep.ok:
                            ui.notify("fix the problems first", type="warning")
                            return
                        try:
                            dash.launch(
                                designer,
                                mock=bool(mock.value),
                                duration_s=None
                                if duration.value in (None, "")
                                else float(duration.value),
                            )
                        except Exception as exc:
                            ui.notify(str(exc), type="negative")
                            return
                        ui.notify("session starting — switch to the monitor to watch it")

                    run_btn = ui.button("run", on_click=_run, icon="play_arrow")
                    stop_btn = ui.button("stop", on_click=dash.stop_run, icon="stop").props(
                        "color=red"
                    )
                run_status = ui.markdown("")
            with ui.card().classes("w-full p-3"):
                ui.label("experiment.json").classes("font-bold")
                preview = ui.code("", language="json").classes("w-full text-xs")

    # ---- the hotspot prompt --------------------------------------------------------------
    with ui.dialog() as prompt_dialog, ui.card():
        prompt_text = ui.markdown("")

        def _prompt_done() -> None:
            dash.answer_prompt()
            prompt_dialog.close()

        ui.button("done, continue", on_click=_prompt_done)
    prompt_seen = {"id": None}

    # ---- periodic refresh ----------------------------------------------------------------
    def refresh_side() -> None:
        if rev["shown"] != rev["current"]:
            rev["shown"] = rev["current"]
            rep = designer.validate()
            if rep.ok:
                status.text = "✓ valid"
                status.classes(remove="text-red-400", add="text-green-400")
                problems.content = ""
            else:
                status.text = f"✗ {len(rep.errors)} problem(s)"
                status.classes(remove="text-green-400", add="text-red-400")
                problems.content = "\n".join(f"- {e}" for e in rep.errors)
            lines = []
            if rep.plan is not None:
                pl = rep.plan
                lines.append(
                    f"**live plan** · {pl.interface} · {pl.profile_name or 'custom profile'}"
                    f" · batch {pl.batch_period_ms} ms · streams: {', '.join(pl.streams) or '–'}"
                )
            if rep.config is not None:
                lines.append(
                    f"**sessions** → `{(designer.base_dir / rep.config.storage.root).resolve()}`"
                )
            for w in rep.warnings:
                lines.append(f"- ⚠ {w}")
            plan_md.content = "\n\n".join(lines)
            preview.content = designer.to_json(compact=True)
        # run state
        rs = dash.run_state()
        if rs is None:
            run_status.content = "_no session started from here_"
            run_btn.enable()
            stop_btn.disable()
        else:
            link = " — [open the monitor](/monitor)" if rs.get("running") else ""
            err = f"\n\n**error:** {rs['error']}" if rs.get("error") else ""
            run_status.content = (
                f"**{rs['state']}** · {rs['experiment']} · {rs['origin']}{link}{err}"
            )
            if rs.get("running"):
                run_btn.disable()
                stop_btn.enable()
            else:
                run_btn.enable()
                stop_btn.disable()
        # prompt
        pending = dash.pending_prompt()
        if pending is not None and prompt_seen["id"] != pending[0]:
            prompt_seen["id"] = pending[0]
            prompt_text.content = "```\n" + pending[1] + "\n```"
            prompt_dialog.open()
        elif pending is None and prompt_dialog.value:
            with contextlib.suppress(Exception):
                prompt_dialog.close()

    ui.timer(0.5, refresh_side)
    ui.timer(0.05, refresh_side, once=True)
