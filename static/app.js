// Small helpers for the owner pages. Plain JS, no build step.
window.VD = (function () {
  let dirty = false;
  document.addEventListener("input", (e) => { if (e.target.closest("form.watch")) dirty = true; });
  document.addEventListener("submit", () => { dirty = false; });

  // Copy buttons: <button data-copy="#inputId">
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    const el = document.querySelector(b.dataset.copy);
    try { await navigator.clipboard.writeText(el.value); } catch { el.select(); document.execCommand("copy"); }
    const t = b.textContent; b.textContent = "Copied"; setTimeout(() => (b.textContent = t), 1500);
  });

  // Confirm before risky submits: <form data-confirm="...">
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });

  // Landing page tabs
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    document.querySelectorAll(".tabpane").forEach((p) => (p.hidden = p.dataset.pane !== t.dataset.tab));
  }));

  // "Connect to Claude": scroll to the panel, flash it and select the link so it's obvious something happened.
  const cb = document.getElementById("connectbtn");
  if (cb) cb.addEventListener("click", () => {
    const panel = document.getElementById("connect"), url = document.getElementById("mcpurl");
    panel.scrollIntoView({ behavior: "smooth", block: "center" });
    panel.classList.remove("flash-panel"); void panel.offsetWidth; panel.classList.add("flash-panel");
    url.focus({ preventScroll: true }); url.select();
  });

  function busy() {
    const a = document.activeElement;
    return dirty || (a && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName) && !a.readOnly);
  }

  // Reload when the server-side record changes (Claude or the vendor acted), unless the user is editing.
  function poll(url, stamp) {
    setInterval(async () => {
      if (document.hidden || busy()) return;
      try {
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) return;
        const d = await r.json();
        if (d.stamp && d.stamp !== stamp) location.reload();
      } catch { /* offline; try again next tick */ }
    }, 5000);
  }

  function boardFilters() {
    const q = document.getElementById("q"), st = document.getElementById("fstatus");
    if (!q) return;
    const apply = () => {
      const term = q.value.trim().toLowerCase(), s = st.value;
      document.querySelectorAll(".vcard").forEach((c) => {
        c.hidden = !!term && !c.dataset.search.includes(term);
      });
      document.querySelectorAll(".col").forEach((c) => (c.hidden = !!s && c.dataset.col !== s));
      document.querySelectorAll(".tile").forEach((t) => t.classList.toggle("on", t.dataset.filterStatus === s));
    };
    q.addEventListener("input", apply);
    st.addEventListener("change", apply);
    document.querySelectorAll(".tile").forEach((t) => t.addEventListener("click", () => {
      st.value = st.value === t.dataset.filterStatus ? "" : t.dataset.filterStatus; apply();
    }));
  }

  return { poll, boardFilters };
})();
