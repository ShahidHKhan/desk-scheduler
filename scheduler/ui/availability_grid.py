# ruff: noqa: E501  (embedded CSS/JS strings read better unwrapped)
"""
Drag-to-select weekly availability grid, as a Streamlit custom component.

Replaces st.data_editor for the in-app form: that grid sorts columns when
a header is clicked and can't tick a range of checkboxes at once. Here a
worker drags across cells to tick (or, starting on a ticked cell, untick)
a whole rectangle; clicking a day or time label toggles that column or
row; closed hours are greyed out and can't be selected at all.

The grid keeps its own state in the browser and reports it to Python on
every change as {day: [bool per form row]}, the shape
scheduler.ingest.in_app.availability_from_grid() expects.
"""

import streamlit as st

from scheduler.ingest.in_app import FORM_SLOTS, FORM_TIME_LABELS
from scheduler.ingest.schema import DAYS
from scheduler.solver.model_input import OPERATING_SLOTS

_CSS = """
.ag { font-family: var(--st-font, "Source Sans Pro", sans-serif); color: var(--st-text-color, #31333f); user-select: none; }
.ag-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 8px; font-size: 14px; }
.ag-hint { opacity: .75; flex: 1 1 260px; }
.ag-count { font-weight: 600; font-variant-numeric: tabular-nums; }
.ag-clear { font: inherit; font-size: 14px; padding: 4px 12px; border-radius: 6px; cursor: pointer;
  border: 1px solid rgba(128,128,128,.4); background: transparent; color: inherit; }
.ag-clear:hover { border-color: var(--st-primary-color, #ff4b4b); color: var(--st-primary-color, #ff4b4b); }
.ag-scroll { overflow-x: auto; }
.ag-table { border-collapse: separate; border-spacing: 3px; touch-action: none; min-width: 420px; width: 100%; table-layout: fixed; }
.ag-table col.ag-col-time { width: 56px; }
.ag-table th { font-size: 13px; font-weight: 600; padding: 0; }
.ag-head, .ag-time { font: inherit; font-size: 13px; font-weight: 600; color: inherit; background: transparent;
  border: 0; border-radius: 4px; cursor: pointer; width: 100%; padding: 4px 2px; }
.ag-time { text-align: right; padding-right: 8px; font-variant-numeric: tabular-nums; font-weight: 400; white-space: nowrap; }
.ag-head:hover, .ag-time:hover { background: var(--st-secondary-background-color, #f0f2f6); }
.ag-cell { height: 22px; border-radius: 4px; cursor: pointer;
  background: var(--st-secondary-background-color, #f0f2f6); border: 1px solid rgba(128,128,128,.25); }
.ag-cell.on { background: var(--st-primary-color, #ff4b4b); border-color: transparent; }
.ag-cell.closed { cursor: not-allowed; border-color: transparent;
  background: repeating-linear-gradient(135deg, transparent 0 4px, rgba(128,128,128,.18) 4px 6px); }
.ag-cell.pv-on { background: var(--st-primary-color, #ff4b4b); opacity: .55; }
.ag-cell.pv-off { background: var(--st-secondary-background-color, #f0f2f6); outline: 2px dashed var(--st-primary-color, #ff4b4b); outline-offset: -2px; }
.ag-cell:focus-visible, .ag-head:focus-visible, .ag-time:focus-visible { outline: 2px solid var(--st-primary-color, #ff4b4b); outline-offset: 1px; }
.ag-legend { display: flex; gap: 16px; font-size: 12px; opacity: .75; margin-top: 6px; }
.ag-legend i { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: -2px; margin-right: 5px; }
"""

_JS = """
export default function (component) {
  const { data, setStateValue, parentElement } = component;
  const root = parentElement;
  // The function re-runs on every Streamlit rerun; the grid and its state
  // persist in the DOM, so build only once.
  if (root.querySelector(".ag")) return;

  const days = data.days, times = data.times, open = data.open;   // open[row][col]
  const value = {};
  days.forEach((d) => { value[d] = times.map(() => false); });

  const wrap = document.createElement("div");
  wrap.className = "ag";
  wrap.innerHTML = `
    <div class="ag-bar">
      <span class="ag-hint">Drag across boxes to fill a range. Start on a filled box to clear instead.
        Click a day or time to toggle the whole column or row.</span>
      <span class="ag-count" aria-live="polite"></span>
      <button type="button" class="ag-clear">Clear all</button>
    </div>
    <div class="ag-scroll"><table class="ag-table" role="grid" aria-label="Weekly availability"></table></div>
    <div class="ag-legend"><span><i style="background:var(--st-primary-color,#ff4b4b)"></i>Available</span>
      <span><i style="background:repeating-linear-gradient(135deg,transparent 0 3px,rgba(128,128,128,.35) 3px 5px)"></i>Desk closed</span></div>`;
  root.appendChild(wrap);

  const table = wrap.querySelector("table");
  const cells = [];   // cells[row][col]
  let html = '<colgroup><col class="ag-col-time">' + days.map(() => "<col>").join("") + "</colgroup><thead><tr><th></th>";
  days.forEach((d, c) => { html += `<th><button type="button" class="ag-head" data-col="${c}">${d}</button></th>`; });
  html += "</tr></thead><tbody>";
  times.forEach((t, r) => {
    html += `<tr><th><button type="button" class="ag-time" data-row="${r}">${t}</button></th>`;
    days.forEach((d, c) => {
      const isOpen = open[r][c];
      html += `<td class="ag-cell${isOpen ? "" : " closed"}" data-row="${r}" data-col="${c}" role="gridcell"` +
        (isOpen ? ` tabindex="-1" aria-checked="false" aria-label="${d} ${t}"` : ` aria-disabled="true" aria-label="${d} ${t}, closed"`) + "></td>";
    });
    html += "</tr>";
  });
  table.innerHTML = html + "</tbody>";
  table.querySelectorAll("td.ag-cell").forEach((td) => {
    const r = +td.dataset.row, c = +td.dataset.col;
    (cells[r] = cells[r] || [])[c] = td;
  });
  const firstOpen = table.querySelector("td.ag-cell:not(.closed)");
  if (firstOpen) firstOpen.tabIndex = 0;

  const countEl = wrap.querySelector(".ag-count");

  function paint() {
    let n = 0;
    times.forEach((_, r) => days.forEach((d, c) => {
      const on = value[d][r];
      if (on) n++;
      const td = cells[r][c];
      td.classList.toggle("on", on);
      if (open[r][c]) td.setAttribute("aria-checked", on ? "true" : "false");
    }));
    countEl.textContent = `${n * 0.5} h marked`;
  }

  function commit() {
    paint();
    const copy = {};
    days.forEach((d) => { copy[d] = value[d].slice(); });
    setStateValue("grid", copy);
  }

  function setRect(r0, c0, r1, c1, on) {
    for (let r = Math.min(r0, r1); r <= Math.max(r0, r1); r++)
      for (let c = Math.min(c0, c1); c <= Math.max(c0, c1); c++)
        if (open[r][c]) value[days[c]][r] = on;
  }

  // --- drag to select a rectangle --------------------------------------
  let drag = null;   // {r0, c0, r1, c1, on, pointerId}

  function cellAt(x, y) {
    const hit = root.elementFromPoint ? root.elementFromPoint(x, y) : document.elementFromPoint(x, y);
    const td = hit && hit.closest ? hit.closest("td.ag-cell") : null;
    return td && table.contains(td) ? td : null;
  }

  function preview() {
    table.querySelectorAll(".pv-on, .pv-off").forEach((td) => td.classList.remove("pv-on", "pv-off"));
    if (!drag) return;
    const cls = drag.on ? "pv-on" : "pv-off";
    for (let r = Math.min(drag.r0, drag.r1); r <= Math.max(drag.r0, drag.r1); r++)
      for (let c = Math.min(drag.c0, drag.c1); c <= Math.max(drag.c0, drag.c1); c++)
        if (open[r][c]) cells[r][c].classList.add(cls);
  }

  table.addEventListener("pointerdown", (e) => {
    const td = e.target.closest && e.target.closest("td.ag-cell");
    if (!td || td.classList.contains("closed") || e.button > 0) return;
    e.preventDefault();
    const r = +td.dataset.row, c = +td.dataset.col;
    drag = { r0: r, c0: c, r1: r, c1: c, on: !value[days[c]][r], pointerId: e.pointerId };
    table.setPointerCapture(e.pointerId);
    td.focus({ preventScroll: true });
    preview();
  });

  table.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const td = cellAt(e.clientX, e.clientY);
    if (!td) return;
    const r = +td.dataset.row, c = +td.dataset.col;
    if (r !== drag.r1 || c !== drag.c1) { drag.r1 = r; drag.c1 = c; preview(); }
  });

  function endDrag(e, apply) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    if (apply) setRect(drag.r0, drag.c0, drag.r1, drag.c1, drag.on);
    drag = null;
    preview();
    if (apply) commit();
  }
  table.addEventListener("pointerup", (e) => endDrag(e, true));
  table.addEventListener("pointercancel", (e) => endDrag(e, false));

  // --- whole column / row toggles --------------------------------------
  function toggleLine(rows, cols) {
    const openCells = [];
    rows.forEach((r) => cols.forEach((c) => { if (open[r][c]) openCells.push([r, c]); }));
    if (!openCells.length) return;
    const allOn = openCells.every(([r, c]) => value[days[c]][r]);
    openCells.forEach(([r, c]) => { value[days[c]][r] = !allOn; });
    commit();
  }
  const allRows = times.map((_, r) => r), allCols = days.map((_, c) => c);
  table.querySelectorAll(".ag-head").forEach((b) => b.addEventListener("click", () => toggleLine(allRows, [+b.dataset.col])));
  table.querySelectorAll(".ag-time").forEach((b) => b.addEventListener("click", () => toggleLine([+b.dataset.row], allCols)));

  wrap.querySelector(".ag-clear").addEventListener("click", () => {
    days.forEach((d) => value[d].fill(false));
    commit();
  });

  // --- keyboard: arrows move, Space/Enter toggles ------------------------
  table.addEventListener("keydown", (e) => {
    const td = e.target.closest && e.target.closest("td.ag-cell");
    if (!td) return;
    let r = +td.dataset.row, c = +td.dataset.col;
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      value[days[c]][r] = !value[days[c]][r];
      commit();
      return;
    }
    const step = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[e.key];
    if (!step) return;
    e.preventDefault();
    // Skip over closed cells in the direction of travel.
    do { r += step[0]; c += step[1]; } while (r >= 0 && r < times.length && c >= 0 && c < days.length && !open[r][c]);
    if (r < 0 || r >= times.length || c < 0 || c >= days.length) return;
    td.tabIndex = -1;
    cells[r][c].tabIndex = 0;
    cells[r][c].focus();
  });

  paint();
}
"""

_component = st.components.v2.component("availability_grid", css=_CSS, js=_JS)


def blank_grid() -> dict[str, list[bool]]:
    return {day: [False] * len(FORM_SLOTS) for day in DAYS}


def availability_grid(key: str) -> dict[str, list[bool]]:
    """Render the grid and return its current value: {day: [bool per form row]}."""
    open_mask = [[slot in OPERATING_SLOTS[day] for day in DAYS] for slot in FORM_SLOTS]
    result = _component(
        key=key,
        data={"days": DAYS, "times": FORM_TIME_LABELS, "open": open_mask},
        default={"grid": blank_grid()},
        on_grid_change=lambda: None,
    )
    return result.grid or blank_grid()
