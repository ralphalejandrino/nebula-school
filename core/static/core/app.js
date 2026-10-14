// Nebula — small progressive enhancements. Every action also works without JS (plain forms).
(function () {
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg; document.body.appendChild(t);
    setTimeout(() => t.remove(), 3500);
  }
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  let timers = [];                                   // cleared whenever the page content changes

  // ---------------------------------------------------------------- per-page wiring
  function initPage(root) {
    // Swipe a proposal: right = add to calendar, left = reject (phone).
    root.querySelectorAll(".swipe").forEach((card) => {
      let x0 = null, dx = 0;
      card.addEventListener("touchstart", (e) => { x0 = e.touches[0].clientX; dx = 0; }, { passive: true });
      card.addEventListener("touchmove", (e) => {
        if (x0 === null) return;
        dx = e.touches[0].clientX - x0;
        card.style.transform = `translateX(${dx}px)`;
        card.classList.toggle("go-right", dx > 60);
        card.classList.toggle("go-left", dx < -60);
      }, { passive: true });
      card.addEventListener("touchend", () => {
        const url = dx > 110 ? card.dataset.approve : dx < -110 ? card.dataset.reject : null;
        card.style.transform = ""; card.classList.remove("go-right", "go-left"); x0 = null;
        if (!url) return;
        const f = document.createElement("form");
        f.method = "post"; f.action = url;
        f.innerHTML = `<input type="hidden" name="csrfmiddlewaretoken" value="${csrf()}">` +
                      `<input type="hidden" name="next" value="${location.pathname + location.search}">`;
        document.body.appendChild(f); f.submit();
      });
    });

    // While a sweep runs, or Gemini reads a note, poll and refresh once it is done.
    function poll(done, maxTries) {
      let tries = 0;
      const t = setInterval(async () => {
        if (maxTries && ++tries > maxTries) return clearInterval(t);
        try {
          const s = await (await fetch("/status.json", { credentials: "same-origin" })).json();
          if (done(s)) { clearInterval(t); go(location.href, false); }
        } catch (e) { /* offline: try again */ }
      }, 5000);
      timers.push(t);
    }
    if (window.NEBULA_RUNNING) poll((s) => !s.running);
    if (window.NEBULA_CAPTURE_WAIT) poll((s) => !s.captures_waiting, 36);   // 3 minutes
  }

  // ---------------------------------------------------------------- tab switching
  // Each tab used to be a whole new page, and the phone blanked the screen between them
  // ("it flickers", the student in class, 2026-10-12 — still flaky with view transitions). Now
  // the next page is fetched and only what CHANGED is swapped in; the header and tab bar
  // never leave the screen. Anything unexpected falls back to a normal page load.
  function morph(oldEl, newEl) {
    if (!oldEl || !newEl) return;
    const a = [...oldEl.children], b = [...newEl.children];
    if (a.length !== b.length) { oldEl.replaceChildren(...b.map((n) => document.importNode(n, true))); }
    else a.forEach((n, i) => { if (n.outerHTML !== b[i].outerHTML) n.replaceWith(document.importNode(b[i], true)); });
    if (oldEl.className !== newEl.className) oldEl.className = newEl.className;
  }

  // Each tap still waited a round trip to the laptop over mobile data (~90 ms + the page),
  // so tabs are PRELOADED and the stored copy is shown at once, then replaced by a fresh one
  // a moment later; a thin bar at the top stays on until it is. A stored copy older than
  // MAX_AGE is never shown: a stale "nothing due" is exactly what this app must not do.
  const cache = new Map();                           // url → {html, at}
  const MAX_AGE = 5 * 60 * 1000;
  const TABS = () => [...document.querySelectorAll(".tabbar a")].map((a) => a.href);
  const abs = (u) => new URL(u, location.href).href;

  async function fetchPage(url, signal) {
    const res = await fetch(url, { credentials: "same-origin", signal });
    const html = await res.text();
    const ok = res.ok && !new URL(res.url).pathname.startsWith("/login");
    if (ok) cache.set(abs(url), { html, at: Date.now(), final: res.url });
    return { res, html, ok };
  }

  function prefetchTabs() {
    const run = () => TABS().forEach((u) => {
      const c = cache.get(u);
      if (u !== location.href && (!c || Date.now() - c.at > 60000)) fetchPage(u).catch(() => {});
    });
    (window.requestIdleCallback || ((f) => setTimeout(f, 800)))(run);
  }

  function markTab(url) {                            // instant feedback on the tap itself
    document.querySelectorAll(".tabbar a, .sidebar nav a").forEach((a) =>
      a.classList.toggle("on", a.href === abs(url)));
  }

  let inflight = null;
  async function go(url, push = true) {
    url = abs(url);
    if (inflight) inflight.abort();
    const ctl = new AbortController(); inflight = ctl;
    markTab(url);
    const hit = cache.get(url);
    if (hit && Date.now() - hit.at < MAX_AGE) {
      render(hit.html, hit.final || url, push);      // the stored copy, at once…
      push = false;
    }
    document.documentElement.classList.add("loading");
    let page;
    try {
      page = await fetchPage(url, ctl.signal);       // …then the fresh one
    } catch (e) {
      if (e.name !== "AbortError") { document.documentElement.classList.remove("loading"); if (!hit) location.href = url; }
      return;
    }
    if (inflight === ctl) document.documentElement.classList.remove("loading");
    if (!page.ok) { location.href = url; return; }   // signed out, an error page, …: let the browser do it
    if (inflight !== ctl) return;
    render(page.html, page.res.url, push);
    prefetchTabs();
  }

  function render(html, finalUrl, push) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const main = doc.querySelector("main.main");
    if (!main || !doc.querySelector(".tabbar")) { location.href = finalUrl; return; }
    const res = { url: finalUrl };
    timers.forEach(clearInterval); timers = [];
    morph(document.querySelector("main.main"), main);
    morph(document.querySelector(".tabbar"), doc.querySelector(".tabbar"));
    morph(document.querySelector(".sidebar"), doc.querySelector(".sidebar"));
    document.title = doc.title;
    if (push) history.pushState({ nebula: 1 }, "", res.url);
    else if (res.url !== location.href) history.replaceState({ nebula: 1 }, "", res.url);
    if (push) window.scrollTo(0, 0);
    // Page flags come from small inline scripts; DOMParser never runs them, so run them here.
    window.NEBULA_RUNNING = false; window.NEBULA_CAPTURE_WAIT = false;
    doc.querySelectorAll("body script:not([src])").forEach((s) => {
      const n = document.createElement("script"); n.textContent = s.textContent;
      document.body.appendChild(n); n.remove();
    });
    initPage(document.querySelector("main.main"));
  }

  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest(".tabbar a, .sidebar nav a, a[data-pane]");
    if (!a) return;
    let url = a.href;
    if (a.dataset.pane) {
      if (!window.matchMedia("(min-width: 1024px)").matches) return;   // phone: open the message page
      url = location.pathname + a.dataset.pane;                         // PC: show it in the right pane
    }
    e.preventDefault();
    go(url);
  });
  window.addEventListener("popstate", () => go(location.href, false));

  // ---------------------------------------------------------------- delegated actions
  // On iPhone, gemini.google.com/app is NOT a link the Gemini app claims (its
  // apple-app-site-association lists /app/download/ios, /app/download/mobile, …), so it
  // opened Safari (2026-10-12, in class). /app/download/ios IS claimed: the app opens.
  const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
              (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const GEMINI_APP = "https://gemini.google.com/app/download/ios";
  function openLink(url) {
    if (IOS && url.startsWith("https://gemini.google.com/app")) url = GEMINI_APP;
    // A real link tap is what iOS hands to the app that claims it.
    const a = document.createElement("a");
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
  }

  // "Study this": copy the tutor prompt, then open Gemini / NotebookLM.
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    // Both start inside the same tap: iPhone Safari blocks a window.open that runs
    // after an await, and a clipboard write outside a tap.
    const copied = navigator.clipboard ? navigator.clipboard.writeText(b.dataset.copy) : Promise.reject();
    openLink(b.dataset.open);
    copied.then(() => toast("Copied — long-press the Gemini box and Paste"))
          .catch(() => window.prompt("Copy this, then paste it in Gemini:", b.dataset.copy));
  });

  // Destructive forms ask first.
  document.addEventListener("submit", (e) => {
    const f = e.target.closest("form[data-confirm]");
    if (f && !window.confirm(f.dataset.confirm)) e.preventDefault();
  });

  initPage(document);
  cache.set(location.href, { html: document.documentElement.outerHTML, at: Date.now(), final: location.href });
  prefetchTabs();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
})();
