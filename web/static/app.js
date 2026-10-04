/*
 * Small behaviours shared across pages: rating slider labels, the
 * confirm-matches form, and a banner when a request fails.
 */
(function () {
  "use strict";

  // Show the label for a rating slider's current value ("1 · Newest").
  function syncRating(input) {
    const out = document.querySelector('output[for="' + input.id + '"]');
    if (!out) return;
    let labels = {};
    try { labels = JSON.parse(input.dataset.labels || "{}"); } catch (e) { /* plain number then */ }
    out.value = labels[input.value] || input.value;
  }

  // On the confirm-matches form, the roster dropdown only matters when
  // "Different person" is picked.
  function syncConfirmRow(row) {
    const select = row.querySelector("select");
    const other = row.querySelector('input[value="choose_other"]');
    if (select && other) select.disabled = !other.checked;
  }

  document.addEventListener("input", (e) => {
    if (e.target.matches('input[type="range"][data-labels]')) syncRating(e.target);
  });
  document.addEventListener("change", (e) => {
    const row = e.target.closest && e.target.closest("[data-confirm-row]");
    if (row) syncConfirmRow(row);
  });

  htmx.onLoad((el) => {
    el.querySelectorAll('input[type="range"][data-labels]').forEach(syncRating);
    el.querySelectorAll("[data-confirm-row]").forEach(syncConfirmRow);
  });

  // HTMX leaves the page as it was when a request fails, so say so.
  function showError(text) {
    const banner = document.getElementById("error-banner");
    if (!banner) return;
    banner.textContent = text;
    banner.hidden = false;
  }
  document.addEventListener("htmx:beforeRequest", () => {
    const banner = document.getElementById("error-banner");
    if (banner) banner.hidden = true;
  });
  document.addEventListener("htmx:responseError", (e) => {
    showError("Something went wrong on the server (error " + e.detail.xhr.status + "). Try again, and if it keeps happening, check the app's logs.");
  });
  document.addEventListener("htmx:sendError", () => {
    showError("Couldn't reach the server. Check your connection and try again.");
  });
})();
