// Підтвердження небезпечних дій
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-confirm]");
  if (btn && !confirm(btn.dataset.confirm)) e.preventDefault();
});

// Автооновлення листування на сторінці звернення
(function () {
  const box = document.getElementById("messages");
  if (!box || !box.dataset.poll) return;
  const scrollDown = () => { box.scrollTop = box.scrollHeight; };
  scrollDown();
  let last = box.innerHTML;
  setInterval(async () => {
    if (document.hidden) return;
    try {
      const r = await fetch(box.dataset.poll, { credentials: "same-origin" });
      if (!r.ok) return;
      const html = await r.text();
      if (html !== last) {
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        box.innerHTML = html; last = html;
        if (atBottom) scrollDown();
      }
    } catch (_) {}
  }, 5000);
})();

// Дашборд оновлюється раз на хвилину
if (location.pathname === "/") setTimeout(() => location.reload(), 60000);
