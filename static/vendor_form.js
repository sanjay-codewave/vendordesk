// Public vendor form: autosave, live rule-based check messages, uploads, submit. Plain JS.
(function () {
  const base = location.pathname.replace(/\/$/, "");
  const form = document.getElementById("vform");
  const saveState = document.getElementById("savestate");
  const ITEMS = JSON.parse(document.getElementById("itemlabels").textContent);
  const FIELD_OF = { gstin: "gstin", state: "gstin", pan: "pan", pan_in_gstin: "pan", ifsc: "ifsc",
                     account: "bank_account_number", holder: "bank_account_name" };
  const KIND_LABEL = {}; // file kind -> item key
  Object.entries(ITEMS).forEach(([k, it]) => { if (it.file) KIND_LABEL[it.file] = k; });
  let state = JSON.parse(document.getElementById("initial").textContent);
  let timer = null, inflight = null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function render(s) {
    state = s;
    if (!s.editable) {
      document.getElementById("formarea").hidden = true;
      document.getElementById("locked").hidden = false;
      window.scrollTo(0, 0);
      return;
    }
    // Field messages
    document.querySelectorAll("#vform .fmsg").forEach((m) => { m.textContent = ""; m.className = "fmsg"; });
    const worst = {};
    const rank = { fail: 3, warn: 2, pass: 1 };
    s.checks.forEach((c) => {
      const f = FIELD_OF[c.key];
      if (!f) return;
      const m = document.querySelector(`#vform .fmsg[data-for="${f}"]`);
      if (!m || (worst[f] && rank[worst[f]] >= rank[c.status])) return;
      worst[f] = c.status;
      m.textContent = (c.status === "pass" ? "✓ " : c.status === "fail" ? "✕ " : "! ") + c.reason;
      m.className = "fmsg " + c.status;
    });
    document.querySelectorAll("#vform input, #vform textarea").forEach((i) =>
      i.classList.toggle("bad", worst[i.name] === "fail"));
    // Checklist
    let done = 0, total = 0;
    s.checks.filter((c) => c.key.startsWith("item:")).forEach((c) => {
      total++; if (c.status === "pass") done++;
      const li = document.querySelector(`#items [data-item="${c.key.slice(5)}"]`);
      if (li) li.className = c.status === "pass" ? "done" : "todo";
    });
    document.getElementById("progressbar").style.width = total ? (100 * done / total) + "%" : "0";
    document.getElementById("progresstext").textContent = `${done} of ${total} done`;
    // Files
    document.querySelectorAll(".upload").forEach((u) => {
      const list = u.querySelector(".ufiles");
      list.replaceChildren();
      s.files.filter((f) => f.kind === u.dataset.kind).forEach((f) => {
        const li = el("li", null, `${f.filename} (${Math.max(1, Math.round(f.size / 1024))} KB) `);
        const rm = el("button", "linkbtn", "Remove");
        rm.type = "button";
        rm.onclick = () => post(`${base}/files/${f.id}/delete`).then(render).catch(showErr(u));
        li.append(rm);
        list.append(li);
      });
      u.classList.toggle("has", list.children.length > 0);
    });
    // Summary list above submit
    const box = document.getElementById("checks");
    box.replaceChildren();
    s.checks.filter((c) => c.status !== "pass").forEach((c) => {
      const li = el("li", "check " + c.status);
      li.append(el("span", "ci"));
      const t = el("span"); t.append(el("b", null, c.label + " "), el("span", "muted", c.reason));
      li.append(t);
      box.append(li);
    });
    if (!box.children.length && s.checks.length) box.append(el("li", "check pass", "✓ Everything looks good. You can submit."));
  }

  async function post(url, body, isJson = true) {
    const opts = { method: "POST" };
    if (body instanceof FormData) opts.body = body;
    else if (body) { opts.body = JSON.stringify(body); opts.headers = { "Content-Type": "application/json" }; }
    const r = await fetch(url, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || d.detail || "Something went wrong. Please try again.");
    return d;
  }

  function showErr(container) {
    return (e) => { const m = container.querySelector(".fmsg"); if (m) { m.textContent = e.message; m.className = "fmsg fail"; } };
  }

  function data() {
    const d = {};
    new FormData(form).forEach((v, k) => (d[k] = v));
    return d;
  }

  async function save() {
    clearTimeout(timer);
    saveState.textContent = "Saving…";
    inflight = post(`${base}/save`, data());
    try { render(await inflight); saveState.textContent = "All changes saved"; }
    catch (e) { saveState.textContent = e.message; }
    finally { inflight = null; }
  }

  form.addEventListener("input", (e) => {
    if (e.target.classList.contains("mono")) {
      const p = e.target.selectionStart;
      e.target.value = e.target.value.toUpperCase();
      e.target.setSelectionRange(p, p);
    }
    saveState.textContent = "Typing…";
    clearTimeout(timer);
    timer = setTimeout(save, 700);
  });
  form.addEventListener("submit", (e) => e.preventDefault());

  document.querySelectorAll(".upload input[type=file]").forEach((inp) => inp.addEventListener("change", async () => {
    const u = inp.closest(".upload"), file = inp.files[0];
    if (!file) return;
    const m = u.querySelector(".fmsg");
    if (file.size > 5 * 1024 * 1024) { m.textContent = "File is larger than 5 MB."; m.className = "fmsg fail"; inp.value = ""; return; }
    m.textContent = "Uploading…"; m.className = "fmsg";
    const fd = new FormData(); fd.append("kind", u.dataset.kind); fd.append("file", file);
    try { render(await post(`${base}/upload`, fd)); m.textContent = "Uploaded ✓"; m.className = "fmsg pass"; }
    catch (e) { m.textContent = e.message; m.className = "fmsg fail"; }
    inp.value = "";
  }));

  // Demo account only: fill made-up sample details (fixed values, plain code).
  const SAMPLES = {
    good: { legal_name: "Sharma Packaging Pvt Ltd", trade_name: "", gstin: "27ABCCS1234K1ZD", pan: "ABCCS1234K",
            address: "Plot 14, MIDC Bhosari, Pune, Maharashtra 411026", bank_account_name: "Sharma Packaging Pvt Ltd",
            bank_account_number: "50100123456789", ifsc: "HDFC0001234", bank_name: "HDFC Bank", msme_number: "UDYAM-MH-26-0012345" },
    bad: { legal_name: "Sharma Packaging Pvt Ltd", trade_name: "", gstin: "27ABCCS1234K1ZA", pan: "ABCDS1234K",
           address: "Plot 14, MIDC Bhosari, Pune, Maharashtra 411026", bank_account_name: "Ravi Sharma",
           bank_account_number: "5010012AB", ifsc: "HDFC1001234", bank_name: "HDFC Bank", msme_number: "" },
  };
  document.querySelectorAll("[data-sample]").forEach((b) => b.addEventListener("click", () => {
    const sample = SAMPLES[b.dataset.sample];
    form.querySelectorAll("input, textarea").forEach((i) => { if (i.name in sample) i.value = sample[i.name]; });
    save();
  }));

  document.getElementById("submitbtn").addEventListener("click", async () => {
    if (timer) await save();
    if (inflight) await inflight.catch(() => {});
    const s = state.summary;
    if ((s.fail || s.missing) && !confirm(`${s.fail} check(s) failed and ${s.missing} item(s) are missing. Submit anyway?`)) return;
    try { render(await post(`${base}/submit`)); } catch (e) { saveState.textContent = e.message; }
  });

  render(state);
})();
