// Общее меню страниц Колеса-2: одна строка «куда перейти», текущая страница — рамкой и жирным.
// Каждая страница живёт в своей вкладке (target по имени): переход с планшетки на статику не закрывает
// планшетку — её таймеры, звук тревоги и открытые окна продолжают работать, а повторный переход
// возвращает в уже открытую вкладку. Вставляется в <span id="nav">, если он есть, иначе — строкой
// в начало страницы. Без зависимостей: подключается и к страницам без ui.js (/acc, /, /flow).
(function () {
  "use strict";
  const PAGES = [
    ["plan", "/plan", "▦", "Планшетка", "главный экран: колёса, режимы, статика, лента шагов, запуск программы"],
    ["acc", "/acc", "◆", "Статика", "статические акселерометры подробно: спектры против коридора эталона, события, запись эталона"],
    ["watch", "/watch", "◉", "Наблюдение", "второй экран: статика и поток простых крупно, читается издалека"],
    ["report", "/report", "☰", "Отчёт", "все шаги программ: режимы, простые, статика, вывод; выгрузка CSV"],
    ["pult", "/pult", "⚙", "Пульт", "логические группы 1–4 и наборы, пуск / стоп / тушение, телеметрия"],
    ["old", "/", "☷", "Подробно", "прежняя панель со всеми счётчиками, графиками и параметрами"],
    ["flow", "/flow", "↻", "Схема", "живая схема протокола: шаги каждого режима, замедленно"],
    ["docs", "/docs/", "?", "Документы", "как всё устроено, параметры, замеры, история находок"],
  ];
  const path = location.pathname.replace(/\.html$/, "").replace(/\/index$/, "/");
  const cur = path.startsWith("/docs") ? "docs" : (PAGES.find(p => p[1] === path) || [])[0];
  if (cur) { try { window.name = cur; } catch (e) { /* без имени — новая вкладка */ } }
  const css = `.w2nav { display:inline-flex; flex-wrap:wrap; gap:2px; align-items:center; font-size:.9em; }
.w2nav a { color:inherit; text-decoration:none; padding:.12em .5em; border:1px solid transparent; border-radius:6px; white-space:nowrap; }
.w2nav a:hover { border-color:var(--line2, #c9c4b8); }
.w2nav a.cur { border-color:var(--on, #0072B2); font-weight:650; }
.w2nav a .i { opacity:.8; margin-right:.2em; }
.w2nav-bar { padding:4px 10px; border-bottom:1px solid var(--line, #dcd8cf); background:var(--card, transparent); }
@media (max-width: 700px) { .w2nav a .w { display:none; } .w2nav a.cur .w { display:inline; } }`;
  function build() {
    const st = document.createElement("style");
    st.textContent = css;
    document.head.appendChild(st);
    const nav = document.createElement("nav");
    nav.className = "w2nav";
    nav.setAttribute("aria-label", "страницы");
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
