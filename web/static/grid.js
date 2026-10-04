/*
 * Drag-to-select weekly availability grid, for the worker's form.
 *
 * A worker drags across cells to tick (or, starting on a ticked cell,
 * untick) a whole rectangle; clicking a day or time label toggles that
 * column or row; closed hours are greyed out and can't be selected.
 * Arrow keys move between open cells and Space/Enter toggles one.
 *
 * Mounts on every [data-availability-grid] element, reading data-days,
 * data-times, data-open (open[row][col]) and data-initial. Its value is
 * kept in the hidden input name="grid" inside the mount as JSON,
 * {day: [one bool per row]} - the shape
 * scheduler.ingest.in_app.availability_from_grid() expects.
 */
(function () {
  "use strict";

  function mount(root) {
    if (root.dataset.ready) return;
    root.dataset.ready = "1";

    const days = JSON.parse(root.dataset.days);
    const times = JSON.parse(root.dataset.times);
    const open = JSON.parse(root.dataset.open);
    const initial = JSON.parse(root.dataset.initial || "{}");
    const field = root.querySelector('input[name="grid"]');

    const value = {};
    days.forEach((d) => {
      value[d] = times.map((_, r) => !!(initial[d] && initial[d][r]) && open[r][days.indexOf(d)]);
    });

    const wrap = document.createElement("div");
    wrap.className = "ag";
    wrap.innerHTML = `
      <div class="ag-bar">
        <span class="ag-hint">Drag across boxes to fill a range. Start on a filled box to clear instead.
          Click a day or time to toggle the whole column or row.</span>
        <span class="ag-count" aria-live="polite"></span>
        <button type="button" class="btn quiet small ag-clear">Clear all</button>
      </div>
      <div class="ag-scroll"><table class="ag-table" role="grid" aria-label="Weekly availability"></table></div>
      <div class="ag-legend"><span><i style="background:var(--accent)"></i>Available</span>
        <span><i style="background:repeating-linear-gradient(135deg,transparent 0 3px,var(--closed) 3px 5px)"></i>Desk closed</span></div>`;
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
      field.value = JSON.stringify(value);
    }

    function setRect(r0, c0, r1, c1, on) {
      for (let r = Math.min(r0, r1); r <= Math.max(r0, r1); r++)
        for (let c = Math.min(c0, c1); c <= Math.max(c0, c1); c++)
          if (open[r][c]) value[days[c]][r] = on;
    }

    // --- drag to select a rectangle ------------------------------------
    let drag = null;   // {r0, c0, r1, c1, on, pointerId}

    function cellAt(x, y) {
      const hit = document.elementFromPoint(x, y);
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
    table.addEventListener("pointerup", (e) => {
      // The drag ends on the cell under the pointer when it's released,
      // not only the last cell a pointermove happened to report.
      if (drag && e.pointerId === drag.pointerId) {
        const td = cellAt(e.clientX, e.clientY);
        if (td) { drag.r1 = +td.dataset.row; drag.c1 = +td.dataset.col; }
      }
      endDrag(e, true);
    });
    table.addEventListener("pointercancel", (e) => endDrag(e, false));

    // --- whole column / row toggles ------------------------------------
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

    // --- keyboard: arrows move, Space/Enter toggles ----------------------
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

    commit();
  }

  // Runs on page load and on every piece of HTML that HTMX swaps in, so a
  // form re-rendered after submitting gets a fresh grid.
  htmx.onLoad((el) => {
    if (el.matches && el.matches("[data-availability-grid]")) mount(el);
    el.querySelectorAll && el.querySelectorAll("[data-availability-grid]").forEach(mount);
  });
})();
