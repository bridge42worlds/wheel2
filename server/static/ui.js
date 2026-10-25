// Общее для двух экранов — пульт (/pult) и наблюдение (/watch), docs/ui_concept.md.
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function fmt(x, d = 0) {
  if (x == null || !isFinite(x)) return "—";
  return Number(x).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtZ(z) { return z == null ? "—" : (z > 0 ? "+" : z < 0 ? "−" : "") + fmt(Math.abs(z), 1); }
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
}
function ago(t) { return t ? fmtDur(Date.now() / 1000 - t) + " назад" : "—"; }
// Демонстрация (опубликованная копия, tools/build_docs.py --static): ответы сервера — из снимка window.DEMO_DATA,
// часы идут от момента снимка, ничего не отправляется.
const DEMO = typeof window !== "undefined" && window.DEMO_DATA ? window.DEMO_DATA : null;
if (DEMO) {
  const t0 = DEMO.__t * 1000, p0 = performance.now();
  Date.now = () => t0 + (performance.now() - p0);
}
function demoGet(url) {
  const u = new URL(url, "http://x/"), path = u.pathname, q = u.searchParams;
  const key = [path, ...["mac", "id"].filter(k => q.has(k)).map(k => k + "=" + q.get(k))].join("?");
  if (key in DEMO) return JSON.parse(JSON.stringify(DEMO[key]));
  if (path in DEMO) return JSON.parse(JSON.stringify(DEMO[path]));
  throw new Error("в демонстрации нет этих данных");
}
// Запросы с пределом ожидания: повисший запрос (планшет потерял Wi-Fi, сервер занят) раньше
// останавливал опрос насовсем — часы шли, а данные стояли без единого признака.
const FETCH_MS = 8000;
async function fetchJSON(url, opts) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), FETCH_MS);
  try {
    const r = await fetch(url, { ...opts, signal: ctl.signal });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || r.status);
    return j;
  } catch (e) {
    throw e.name === "AbortError" ? new Error(`нет ответа ${FETCH_MS / 1000} с`) : e;
  } finally { clearTimeout(timer); }
}
async function getJSON(url) {
  if (DEMO) return demoGet(url);
  return fetchJSON(url, { cache: "no-store" });
}
async function postJSON(url, body) {
  if (DEMO) throw new Error("демонстрация: без сервера ничего не отправляется");
  return fetchJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}
// Агент на связи: отвечает (online) или молчит по команде (quiet — телеметрия выключена, команды слышит)
const alive = st => st === "online" || st === "quiet";
// z: ✓ |z| < 3, ◐ < 5, ✗ дальше — значок, слово и класс цвета вместе
function mark(z, warn = 3, bad = 5) {
  if (z == null || !isFinite(z)) return { cls: "mut", icon: "·", word: "нет данных" };
  const a = Math.abs(z);
  return a < warn ? { cls: "ok", icon: "✓", word: "в пределах" } : a < bad ? { cls: "warn", icon: "◐", word: "заметно" }
    : { cls: "bad", icon: "✗", word: "вне статистики" };
}
function markP(p) {   // p-значение хи-квадрат
  if (p == null) return { cls: "mut", icon: "·", word: "мало данных" };
  return p > 1e-3 ? { cls: "ok", icon: "✓", word: "равномерно" } : p > 1e-6 ? { cls: "warn", icon: "◐", word: "заметно" }
    : { cls: "bad", icon: "✗", word: "неравномерно" };
}
const ROLES = {
  A: { icon: "▲", name: "генерация", cls: "A" }, B: { icon: "▼", name: "поглощение", cls: "B" },
  S: { icon: "◆", name: "статический", cls: "S" }, idle: { icon: "○", name: "стоит", cls: "" },
};
// Вывод статики за отрезок (static_watch.verdict) — один словарь на все страницы (docs/static_watch.md):
// ↻ повторить — данные отрезка испорчены помехой или прибором (программа повторит шаг сама);
// ⚠ проверить — отклонение от эталона без известной причины держится: нужен взгляд человека.
const STATIC_VERDICT = {
  ok: { cls: "ok", icon: "✓", word: "как эталон — продолжать", short: "продолжать" },
  check: { cls: "bad", icon: "⚠", word: "отклонение — проверить", short: "проверить" },
  repeat: { cls: "warn", icon: "↻", word: "помеха или прибор — повторить", short: "повторить" },
  nobase: { cls: "mut", icon: "◌", word: "нет эталона", short: "нет эталона" },
  nodata: { cls: "mut", icon: "○", word: "нет данных", short: "нет данных" },
};
// Состояние измерителя в эту минуту (static_watch.status → d.static[].status, text, age, base)
function staticStatus(s) {
  if (!s) return { cls: "mut", icon: "○", word: "отсчётов ещё нет" };
  const age = s.age;
  switch (s.status) {
    case "none": return age != null && age > 10 ? { cls: "bad", icon: "✗", word: `датчик молчит ${fmtDur(age)}` } : { cls: "mut", icon: "○", word: "копится первое окно (10 с)" };
    case "dist": return { cls: "warn", icon: "↻", word: "помеха — повторить" };
    case "inst": return { cls: "warn", icon: "↻", word: "прибор — повторить" };
    case "nobase": return { cls: "mut", icon: "◌", word: s.base && s.base.fits === false ? "эталон для другого режима" : "нет эталона" };
    case "dev": return { cls: "bad", icon: "⚠", word: "отклонение — проверить" };
    case "maybe": return { cls: "warn", icon: "◐", word: "разовое отклонение" };
    case "known": return { cls: "ok", icon: "✓", word: "как эталон (кроме известных линий)" };
    case "ok": return { cls: "ok", icon: "✓", word: "как эталон" };
  }
  return { cls: "mut", icon: "○", word: s.status || "—" };
}
// Виды находок статики — значок и слово (ключи — static_watch.KIND_NAMES)
const KIND = { knock: "↻ толчок", spikes: "↻ выбросы", moved: "⇅ сдвиг", quant: "⚙ квантование", gap: "⚙ пропуск отсчётов", sat: "⚙ зашкал",
  mode: "⚙ режим датчика", line: "│ линия", gone: "│ линия пропала", band: "≋ полоса", gauss: "∿ форма шума" };
const CLS_WORD = { dist: "помеха — повторить", inst: "прибор — повторить", dev: "отклонение — проверить" };
// Значки состояний агента и ролей — одни на все страницы: план и факт одним языком
const ICONS = { gen: "▶", abs: "▼", idle: "⏸", meas: "◆", quiet: "◇", radio: "◌", done: "☑", off: "✗", restart: "⟳", late: "⋯", hot: "♨", rest: "⏸" };
// Пиктограммы режимов: рисуются цветом текста, 28×18
const PICT = {
  solo: `<svg viewBox="0 0 28 18" width="28" height="18" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.6"><rect x="1" y="5" width="5" height="8"/><rect x="8" y="5" width="5" height="8"/><rect x="15" y="5" width="5" height="8"/><rect x="22" y="5" width="5" height="8"/></g></svg>`,
  solo_dc: `<svg viewBox="0 0 28 18" width="28" height="18" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.6"><rect x="1" y="5" width="5" height="8"/><rect x="8" y="5" width="5" height="8"/><rect x="15" y="5" width="5" height="8"/><rect x="22" y="5" width="5" height="8"/></g><g fill="currentColor"><rect x="2.5" y="1" width="2" height="2"/><rect x="9.5" y="1" width="2" height="2"/><rect x="16.5" y="1" width="2" height="2"/><rect x="23.5" y="1" width="2" height="2"/><rect x="2.5" y="15" width="2" height="2"/><rect x="9.5" y="15" width="2" height="2"/><rect x="16.5" y="15" width="2" height="2"/><rect x="23.5" y="15" width="2" height="2"/></g></svg>`,
  one: `<svg viewBox="0 0 28 18" width="28" height="18" aria-hidden="true"><circle cx="14" cy="9" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><g fill="currentColor"><circle cx="3" cy="3" r="2"/><circle cx="25" cy="3" r="2"/><circle cx="3" cy="15" r="2"/><circle cx="25" cy="15" r="2"/></g><g stroke="currentColor" stroke-width="1"><line x1="5" y1="4" x2="10" y2="7"/><line x1="23" y1="4" x2="18" y2="7"/><line x1="5" y1="14" x2="10" y2="11"/><line x1="23" y1="14" x2="18" y2="11"/></g></svg>`,
  relay: `<svg viewBox="0 0 28 18" width="28" height="18" aria-hidden="true"><path d="M7 9 A7 6 0 1 1 14 15" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M11 13 L14 15 L11 17.5" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="14" cy="9" r="2" fill="currentColor"/></svg>`,
  cascade: `<svg viewBox="0 0 28 18" width="28" height="18" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 3 H22 M18 0.5 L22 3 L18 5.5"/><path d="M2 9 H22 M18 6.5 L22 9 L18 11.5"/><path d="M2 15 H22 M18 12.5 L22 15 L18 17.5"/></g></svg>`,
  loop: `<svg viewBox="0 0 28 18" width="28" height="18" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 12 C4 2, 24 2, 24 9"/><path d="M24 9 C24 16, 8 16, 7 12"/><path d="M4 9 L7 12 L10 9"/></g></svg>`,
};
// Наборы генерации на пульте (ключи — PRESETS сервера) и поглощение (параметры группы sm, sh, bgr)
const GEN_MODES = [
  { key: "solo", name: "каждый сам", desc: "каждый агент сам набирает шум, складывает число и проверяет; ~26 простых/с на агента" },
  { key: "solo_dc", name: "два ядра", desc: "каждый сам, шум и отсев — на ядре связи; ~46 простых/с на агента, греет сильнее" },
  { key: "one", name: "на одном числе", desc: "одно число на всех, базы проверки поделены; ~11 простых/с на четверых; когерентность цикла" },
  { key: "relay", name: "эстафета", desc: "число из шума всех, проверяет один, ход по кругу; ~37 простых/с на группу" },
  { key: "cascade", name: "каскад", desc: "числа из шума всех, проверяют все сразу; ~124 простых/с из отдыха, ~85 долго" },
  { key: "loop", name: "петля", desc: "генерация берёт шум только из эфира рассеяния своей группы — возвращается ли рассеянное в числа" },
];
const ABS_MODES = [
  { sm: 1, name: "каждый сам", desc: "каждый агент B смешивает всё число со своим шумом и выпускает в эфир" },
  { sm: 2, name: "россыпь", desc: "каждый агент B берёт свой кусок числа: ни один кадр не несёт число целиком" },
  { sm: 3, name: "эстафета", desc: "по цепочке: каждый добавляет свой шум, последний выпускает один кадр" },
  { sm: 0, name: "тишина", desc: "агенты B только слушают и считают — эфир чистый" },
];
// Параметры по смыслу: ключ, подпись, значения, что значит и что изменится в опыте
const PARAM_GROUPS = [
  { name: "Что ищем", keys: ["bits"] },
  { name: "Откуда шум", keys: ["src", "raw"] },
  { name: "Как проверяем", keys: ["nb", "td", "all"] },
  { name: "Работа группы", keys: ["rk", "pw", "k", "pre"] },
  { name: "Ядра и тепло", keys: ["dc", "cpu", "tmax"] },
  { name: "Связь", keys: ["nr", "txp"] },
  { name: "Поглощение (сет B)", keys: ["sh", "bgr", "sf", "cf"] },
  { name: "Журнал", keys: ["pl"] },
];
const PARAMS = {
  bits: { label: "Длина числа", unit: "бит", values: [128, 256, 512, 1024, 2048],
    desc: "Сколько бит в каждом кандидате. Простых среди длинных чисел меньше, а проверка дольше.",
    change: "256 → ~26 простых/с на агента; 512 → ~3; 1024 → ~0,6. Длиннее — реже простые, дольше цикл группы." },
  src: { label: "Источники шума", bits: [[1, "ГСЧ чипа"], [2, "CSI Wi-Fi"], [4, "АЦП"], [8, "дрожание RC"], [16, "эфир сета B"]],
    desc: "Откуда берётся случайность числа. Несколько отмеченных складываются.",
    change: "ГСЧ — быстрый (45 тыс. слов/с); CSI, АЦП и дрожание — в десятки раз медленнее; эфир B — петля «рассеянное → в числа»." },
  raw: { label: "Как брать шум", values: [[0, "с хешем"], [1, "без хеша"], [2, "фон Нейман"]],
    desc: "С хешем SHA-256 числа равномерны, структура источника размешана. Без хеша — младшие биты сырья прямо в число.",
    change: "Без хеша видна физика источника (у АЦП 54–59 % единиц); фон Нейман снимает перекос 0 и 1." },
  nb: { label: "Баз проверки", values: [4, 6, 8, 10, 12],
    desc: "Сколько баз Миллера — Рабина проверяет каждое число. Ошибка — меньше 4⁻ⁿ.",
    change: "Больше баз — дольше проверка; 8 хватает с огромным запасом." },
  td: { label: "Отсев делителями до", values: [0, 100, 1000, 10000],
    desc: "Перед проверкой кандидат делится на все простые меньше этого числа.",
    change: "До 1000 отсеивается 84 % кандидатов — проверка Миллером — Рабином в 6 раз реже." },
  all: { label: "Все базы у каждого", values: [[0, "нет"], [1, "да"]],
    desc: "«На одном числе»: каждый агент проверяет все базы, ответы обязаны сойтись (согласие).",
    change: "Проверка согласия агентов; группа в разы медленнее." },
  rk: { label: "Простых за ход", values: [1, 2, 4, 8, 16],
    desc: "Эстафета и каскад: сколько простых найти, прежде чем передать ход.",
    change: "Больше — реже передача хода, меньше перемешивание по агентам." },
  pw: { label: "Ждать чужие кусочки", unit: "мс", values: [0, 5, 10, 20, 50, 100, 500],
    desc: "Эстафета и каскад: сколько ждать кусочки шума от всех. 0 — ждать всех (молчащего — после трёх просьб).",
    change: "Короче — быстрее, но больше кругов без чьих-то кусочков (они заменяются своим шумом: меньше когерентность)." },
  k: { label: "Кандидатов за сбор", values: [8, 16, 24, 32, 48, 64],
    desc: "Сколько чисел складывается из одного круга кусочков.", change: "Больше — меньше кругов на простое." },
  pre: { label: "Кусочки заранее", values: [[1, "да"], [0, "нет"]],
    desc: "Наборы шума готовятся до просьбы.", change: "Без них круг сбора дольше." },
  dc: { label: "Два ядра", values: [[0, "одно"], [1, "два"]],
    desc: "Ядро связи набирает шум и отсеивает, ядро счёта только проверяет.",
    change: "В 1,8 раза быстрее «каждый сам», но греет сильнее: регулятор режет работу у предела." },
  cpu: { label: "Частота процессора", unit: "МГц", values: [240, 160, 80],
    desc: "Тактовая частота. На нашей сборке напряжение ядра от неё не зависит.", change: "Ниже — медленнее и чуть прохладнее (≈ −3…5 °C в простое)." },
  tmax: { label: "Предел температуры", unit: "°C", values: [60, 65, 70],
    desc: "Регулятор держит кристалл на градус ниже, урезая долю работы.", change: "Ниже — прохладнее, но меньше простых." },
  nr: { label: "Скорость эфира", values: [[1, "24 Мбит/с"], [2, "6 Мбит/с"], [0, "1 Мбит/с"]],
    desc: "Скорость кадров ESP-NOW между агентами.", change: "Медленнее — кадр дольше в эфире: группа тормозит, плата греется." },
  txp: { label: "Мощность передатчика", unit: "дБм", values: [20, 14, 11, 8, 5, 2],
    desc: "Мощность радио агента.", change: "Ниже — меньше тепла и эфирного шума; платы рядом — 2 дБм почти не хуже 20." },
  sh: { label: "Пустышка", values: [[0, "нет"], [1, "да"]],
    desc: "Тот же трафик рассеяния, но вместо числа — шум.", change: "Контроль: отделяет действие числа от действия радио." },
  bgr: { label: "Фон сета B", unit: "кадров/с", values: [0, 5, 10, 20, 50, 100],
    desc: "Агенты B выпускают свой шум в эфир с этой частотой.", change: "Постоянный эфир поверх рассеяния; нужен для петли." },
  sf: { label: "Функция смешивания", values: [[0, "хеш"], [1, "XOR"], [2, "умножение"], [3, "перестановка"]],
    desc: "Как агент B смешивает число со своим шумом.", change: "Разные функции — разная «глубина» разрушения числа." },
  cf: { label: "CSI у агентов", values: [[0, "с любых кадров"], [1, "только роутер"], [2, "только сет B"]],
    desc: "С каких кадров генерация берёт шум CSI.", change: "«С любых» — рассеяние B попадает в шум A (петля по радио)." },
  pl: { label: "Журнал простых", values: [[1, "да"], [0, "нет"]],
    desc: "Каждое простое уходит на сервер и перепроверяется; по нему — страж статистики на экране наблюдения.",
    change: "Без журнала проверяется одно простое в секунду и стража нет." },
};
function paramText(key, v) {
  const p = PARAMS[key];
  if (!p) return String(v);
  if (p.bits) return p.bits.filter(([b]) => v & b).map(([, n]) => n).join(" + ") || "—";
  const it = (p.values || []).find(x => Array.isArray(x) ? x[0] === v : x === v);
  return Array.isArray(it) ? it[1] : `${fmt(v)}${p.unit ? " " + p.unit : ""}`;
}
// Опрос: свой период, ошибки — в строку состояния, не роняют страницу
function poll(fn, ms) {
  let busy = false;
  const go = async () => {
    if (busy) return;
    busy = true;
    try { await fn(); } catch (e) { const el = $("conn"); if (el) el.innerHTML = `<span class="bad">✗ нет связи с сервером: ${esc(e.message)}</span>`; }
    busy = false;
  };
  go();
  return setInterval(go, ms);
}
// Пояснение по нажатию на ⓘ: <button class="tip" data-tip="ключ">ⓘ</button>, тексты — в TIPS страницы
// {ключ: [заголовок, текст]}. На планшете подсказки title не видны, поэтому — по нажатию; ещё раз, мимо или Esc — закрыть.
let tipEl = null;
function tipClose() { if (tipEl) { tipEl.remove(); tipEl = null; } }
document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest(".tip[data-tip]");
  if (!b) { if (tipEl && !tipEl.contains(e.target)) tipClose(); return; }
  e.preventDefault(); e.stopPropagation();                // не открывать окна страницы под пояснением
  const key = b.dataset.tip, T = typeof TIPS !== "undefined" ? TIPS[key] : null;
  if (tipEl && tipEl.dataset.key === key) { tipClose(); return; }
  tipClose();
  if (!T) return;
  tipEl = document.createElement("div");
  tipEl.className = "tippop"; tipEl.dataset.key = key; tipEl.setAttribute("role", "note");
  tipEl.innerHTML = `<b>ⓘ ${esc(T[0])}</b><div>${esc(T[1])}</div>`;
  document.body.appendChild(tipEl);
  const r = b.getBoundingClientRect(), w = Math.min(400, innerWidth - 16);
  tipEl.style.width = w + "px";
  const h = tipEl.offsetHeight, x = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2));
  const y = r.bottom + 6 + h > innerHeight - 8 ? Math.max(8, r.top - h - 6) : r.bottom + 6;
  tipEl.style.left = x + "px"; tipEl.style.top = y + "px";
}, true);
document.addEventListener("keydown", e => { if (e.key === "Escape") tipClose(); });
// Звук тревоги: браузер даёт звук только после нажатия на страницу — звуковой контекст заводится при первом нажатии
let AC = null;
function audioUnlock() {
  try { AC = AC || new (window.AudioContext || window.webkitAudioContext)(); if (AC.state === "suspended") AC.resume(); } catch (e) { AC = null; }
}
document.addEventListener("pointerdown", audioUnlock, { passive: true });
document.addEventListener("keydown", audioUnlock);
function audioReady() { return !!AC && AC.state === "running"; }
// n коротких сигналов: высокий — низкий по очереди (слышно и сквозь шум моторов); false — звук не разрешён
function beep(n = 3) {
  if (!audioReady()) return false;
  const t0 = AC.currentTime + 0.02;
  for (let k = 0; k < n; ++k) {
    const o = AC.createOscillator(), g = AC.createGain(), t = t0 + k * 0.32;
    o.type = "square"; o.frequency.value = k % 2 ? 660 : 990;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.18, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.24);
    o.connect(g); g.connect(AC.destination); o.start(t); o.stop(t + 0.26);
  }
  return true;
}
function sheet(html, onclose) {
  const bg = document.createElement("div");
  bg.className = "sheet-bg";
  bg.innerHTML = `<div class="sheet">${html}</div>`;
  bg.addEventListener("click", e => { if (e.target === bg || e.target.dataset.close != null) { bg.remove(); onclose && onclose(); } });
  document.body.appendChild(bg);
  return bg.firstChild;
}
