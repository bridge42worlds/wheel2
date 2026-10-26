// Общее меню Колеса-2: два места — «▦ Установка» (основной экран: всё управление и наблюдение, подробное —
// всплывающими окнами) и «? Документы». Каждое живёт в своей вкладке (target по имени): переход в документы не
// закрывает установку — её таймеры и звук тревоги работают дальше. Внутри окна установки (iframe, ?embed)
// меню не показывается. Вставляется в <span id="nav">, если он есть, иначе — строкой в начало страницы.
// Без зависимостей: подключается и к страницам без ui.js (/acc, /old, /flow).
(function () {
  "use strict";
  const PAGES = [
    ["plan", "/", "▦", "Установка", "основной экран: колёса, режимы, статика с Гауссом и спектром, лента шагов, серии, запуск; подробное — окнами"],
    ["docs", "/docs/", "?", "Документы", "как всё устроено, схемы подключения, параметры, замеры; история — приложением"],
  ];
  const embed = new URLSearchParams(location.search).has("embed") || window.self !== window.top;
  if (embed) {                                     // внутри окна установки: без меню и без своих ссылок «↗»
    const st = document.createElement("style");
    st.textContent = ".w2-noembed { display:none !important; }";
    document.head.appendChild(st);
    return;
  }
  const path = location.pathname.replace(/\.html$/, "").replace(/\/index$/, "/");
  const cur = path.startsWith("/docs") ? "docs" : path === "/" || path === "/plan" ? "plan" : null;
  if (cur) { try { window.name = cur; } catch (e) { /* без имени — новая вкладка */ } }
  const css = `.w2nav { display:inline-flex; flex-wrap:wrap; gap:2px; align-items:center; font-size:.9em; }
.w2nav a { color:inherit; text-decoration:none; padding:.12em .5em; border:1px solid transparent; border-radius:6px; white-space:nowrap; }
.w2nav a:hover { border-color:var(--line2, #c9c4b8); }
.w2nav a.cur { border-color:var(--on, #0072B2); font-weight:650; }
.w2nav a .i { opacity:.8; margin-right:.2em; }
.w2nav-bar { padding:4px 10px; border-bottom:1px solid var(--line, #dcd8cf); background:var(--card, transparent); }`;
  function build() {
    const st = document.createElement("style");
    st.textContent = css;
    document.head.appendChild(st);
    const nav = document.createElement("nav");
    nav.className = "w2nav";
    nav.setAttribute("aria-label", "разделы");
    nav.innerHTML = PAGES.map(([k, href, icon, word, note]) =>
      `<a href="${href}" target="${k}" class="${k === cur ? "cur" : ""}" title="${note}"${k === cur ? ' aria-current="page"' : ""}><span class="i">${icon}</span><span class="w">${word}</span></a>`).join("");
    const slot = document.getElementById("nav");
    if (slot) slot.appendChild(nav);
    else {
      const bar = document.createElement("div");
      bar.className = "w2nav-bar";
      bar.appendChild(nav);
      document.body.insertBefore(bar, document.body.firstChild);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
