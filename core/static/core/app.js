// Nebula — small progressive enhancements. Every action also works without JS (plain forms).
(function () {
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg; document.body.appendChild(t);
    setTimeout(() => t.remove(), 3500);
  }
  const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";

  // Swipe a proposal: right = add to calendar, left = reject (phone).
  document.querySelectorAll(".swipe").forEach((card) => {
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
      f.innerHTML = `<input type="hidden" name="csrfmiddlewaretoken" value="${csrf}">` +
                    `<input type="hidden" name="next" value="${location.pathname + location.search}">`;
      document.body.appendChild(f); f.submit();
    });
  });

  // "Study this": copy the tutor prompt, then open Gemini / NotebookLM.
  document.querySelectorAll("[data-copy]").forEach((b) => {
    b.addEventListener("click", () => {
      // Both start inside the same tap: iPhone Safari blocks a window.open that runs
      // after an await, and a clipboard write outside a tap.
      const copied = navigator.clipboard ? navigator.clipboard.writeText(b.dataset.copy) : Promise.reject();
      window.open(b.dataset.open, "_blank", "noopener");
      copied.then(() => toast("Copied — long-press the Gemini box and Paste"))
            .catch(() => window.prompt("Copy this, then paste it in Gemini:", b.dataset.copy));
    });
  });

  // PC inbox: open a message in the right pane instead of a new page.
  if (window.matchMedia("(min-width: 1024px)").matches) {
    document.querySelectorAll("a.msg[data-pane]").forEach((a) => {
      a.addEventListener("click", (e) => { e.preventDefault(); location.search = a.dataset.pane.slice(1); });
    });
  }

  // While a sweep runs, poll and reload when it finishes.
  if (window.NEBULA_RUNNING) {
    const t = setInterval(async () => {
      try {
        const r = await fetch("/status.json", { credentials: "same-origin" });
        const s = await r.json();
        if (!s.running) { clearInterval(t); location.reload(); }
      } catch (e) { /* offline: try again */ }
    }, 5000);
  }

  // Destructive forms ask first.
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // Capture: while Gemini reads a note, poll and reload once it is done.
  if (window.NEBULA_CAPTURE_WAIT) {
    let tries = 0;
    const t = setInterval(async () => {
      if (++tries > 36) return clearInterval(t);          // 3 minutes, then leave it to a manual refresh
      try {
        const s = await (await fetch("/status.json", { credentials: "same-origin" })).json();
        if (!s.captures_waiting) { clearInterval(t); location.reload(); }
      } catch (e) { /* offline: try again */ }
    }, 5000);
  }

  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
})();
