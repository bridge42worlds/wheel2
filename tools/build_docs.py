"""Документация в HTML: docs/*.md и README.md → docs/html/*.html.

    python tools/build_docs.py [--quiet]
    python tools/build_docs.py --static [папка]   # комплект для публикации вне сети, по умолчанию build/site

Источник правды — markdown: правьте .md, HTML пересобирается этой командой (и сам при
старте сервера). Страницы самодостаточные (стиль внутри), открываются и двойным щелчком,
и с сервера: http://<сервер>:8090/docs/. Ссылки на .md переводятся на .html, у каждой
страницы — оглавление, у части — кнопка «посмотреть вживую» на живую схему (/flow).

Разметка — то подмножество markdown, что есть в документах: заголовки, абзацы, списки
(вложенные), таблицы (с экранированной чертой \\|), блоки кода, горизонтальная черта,
`код`, **жирный**, *курсив*, [ссылки](файл.md).

Комплект --static: те же страницы и живая схема (только демонстрация, к серверу не обращается),
все ссылки внутри комплекта; адреса сети опыта и её имя вычищены. Главная страница комплекта —
index.html без обёртки <html>/<head> (её добавляет площадка публикации), остальные — полные.
"""
import html, os, re, shutil, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(DOCS, "html")
IMG = os.path.join(DOCS, "img")                   # схемы и рисунки: в markdown ![подпись](img/файл.svg)

# порядок и описания страниц; flow — режим живой схемы для кнопки «посмотреть вживую»
PAGES = [
    ("preamble", "Преамбула", "Зачем этот опыт: топология до пространства, порядок познания (эстетика — этика — "
     "гносеология — онтология — наука), ошибка первого колеса, критерий. Авторы: bridge42worlds, Λura13 (DeepSeek), "
     "Остов (Claude).", ""),
    ("how_it_works", "Как всё работает", "Установка целиком: колёса, катушки, статика, телефон, моторы, сервер; агент, шум, поиск "
     "простого, групповой режим, поглощение, температура и сторожа; основной экран — схема установки; эксперимент, программа, "
     "результаты; параметры, данные, "
     "прошивка, инструменты.", ""),
    ("modes", "Режимы", "Все режимы словами основного экрана: вращение, поле катушек, совместная работа, шум, излучение и "
     "поглощение; эксперимент — все режимы по порядку; статика, телеметрия, тепло.", ""),
    ("wiring", "Схемы подключения", "Общая схема установки; колесо: шина агентов от своих аккумуляторов (C1–C3), катушка "
     "(5,1 В, DRV8833); рама: моторы (блок 12 В, два DRV8871); сеть опыта: роутер Mango, канал 6, ноутбук "
     "кабелем, запасная сеть, порты. Полярность диодов и конденсаторов.", ""),
    ("spec", "Спецификация и смета", "Из чего собрана установка и сколько стоит: платы, датчики, колёса, питание, "
     "моторы, сеть, монтаж, инструменты — со ссылками на AliExpress, цены в KWD; итог около 277 KWD без инструментов.", ""),
    ("sensors", "Датчики, телефон, моторы и катушки", "Датчик обода (обороты, невесомость), магнитометр, статика ICM-42688 и "
     "ADXL355, телефон (служба, экран можно гасить), агент моторов и агенты катушек.", ""),
    ("static_watch", "Статика: эталон и слежение", "Роль S: что меряет агент, что считает сервер, эталон шума, линии, полосы, "
     "толчки, вывод «продолжать / повторить / проверить»; Гаусс и спектр на основном экране.", ""),
    ("program", "Эксперимент и программа", "Эксперимент: параметры опыта, вложенность, список опытов (вращение и поле каждого "
     "колеса, своё установление, отдых), контроль, эталон, аномалии и их проверка парами, тревога статики; результаты — "
     "что влияет при прочих равных.", ""),
    ("group_protocol", "Групповой режим", "Каскад, эстафета, одно число на всех: протокол, кусочки шума, молчащие, сбои, "
     "согласие, замеры на 1–6 платах.", "cascade"),
    ("scatter", "Поглощение", "Как простое попадает к поглотителям, режимы (все со всеми, россыпь, эстафета, тишина), "
     "пустышка, фон, функции, петля через эфир — обратный шум.", "scatter"),
    ("randomness", "Откуда берётся случайность", "ГСЧ чипа и шум радио, CSI, АЦП, дрожание RC, датчики: как берём, "
     "сколько засчитываем, как смешиваем, без хеша, скорость по источникам и длинам.", "solo"),
    ("thermal", "Температура агентов", "Предел 70 °C: регулятор, замеры нагрева и остывания, откуда тепло, отдых по очереди, "
     "спринт, отдых между режимами, агенты в трубке и радиаторы, рейтинг агентов.", ""),
    ("history", "Приложение А. История изменений", "Как установка пришла к нынешнему виду: по дням — что появилось, "
     "что нашли и исправили (03.10–26.10.2026).", ""),
    ("ui_concept", "Приложение Б. Концепт пульта и наблюдения", "Исторический черновик двух экранов (06.10.2026); "
     "сейчас они — окна основного экрана.", ""),
]
APPENDIX = {"history", "ui_concept"}          # приложения — отдельной группой в меню и на главной
TITLES = {k: t for k, t, _, _ in PAGES}

CSS = """
:root { --bg:#f6f5f2; --card:#ffffff; --ink:#1d2228; --mut:#6b7380; --line:#ddd9d0; --soft:#efece6; --on:#0072B2;
        --warn:#E69F00; --ok:#009E73; --code:#f1eee8; --sb:#D55E00; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --bg:#15181c; --card:#1e2228; --ink:#e8eaed; --mut:#9aa1ab; --line:#353b44; --soft:#262b33;
          --on:#56B4E9; --code:#262b33; --sb:#ff8a4c; color-scheme:dark; }
}
:root[data-theme="dark"] { --bg:#15181c; --card:#1e2228; --ink:#e8eaed; --mut:#9aa1ab; --line:#353b44; --soft:#262b33;
          --on:#56B4E9; --code:#262b33; --sb:#ff8a4c; color-scheme:dark; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }
a { color:var(--on); }
header { padding:10px 18px; display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; border-bottom:1px solid var(--line);
         background:var(--card); position:sticky; top:env(safe-area-inset-top, 0px); z-index:2; }
header .brand { font-weight:700; font-size:16px; text-decoration:none; color:var(--ink); }
header nav { display:flex; gap:14px; flex-wrap:wrap; font-size:13px; }
.layout { display:grid; grid-template-columns:250px minmax(0, 1fr); gap:24px; max-width:1240px; margin:0 auto; padding:18px; }
aside { position:sticky; top:62px; align-self:start; max-height:calc(100vh - 80px); overflow:auto; font-size:13px; }
aside h4 { margin:0 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--mut); }
aside ul { list-style:none; margin:0 0 16px; padding:0; }
aside li { margin:2px 0; }
aside a { text-decoration:none; display:block; padding:3px 8px; border-radius:6px; }
aside a.cur { background:var(--soft); color:var(--ink); font-weight:650; }
aside .toc a { padding:2px 8px; color:var(--mut); }
aside .toc .l3 a { padding-left:20px; }
main.doc { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:22px 30px 30px; min-width:0; }
main.doc h1 { font-size:26px; line-height:1.25; margin:0 0 14px; }
main.doc h2 { font-size:20px; margin:30px 0 10px; padding-top:10px; border-top:1px solid var(--soft); }
main.doc h3 { font-size:16.5px; margin:22px 0 8px; }
main.doc h4 { font-size:15px; margin:18px 0 6px; }
main.doc h2 a.anc, main.doc h3 a.anc { opacity:0; margin-left:6px; text-decoration:none; color:var(--mut); }
main.doc h2:hover a.anc, main.doc h3:hover a.anc { opacity:1; }
code { font:13px/1.4 ui-monospace, Consolas, monospace; background:var(--code); padding:1px 5px; border-radius:4px; }
pre { background:var(--code); padding:12px 14px; border-radius:8px; overflow:auto; }
pre code { padding:0; background:none; font-size:12.5px; }
.tw { overflow-x:auto; margin:10px 0 14px; }
table { border-collapse:collapse; font-size:13.5px; min-width:60%; }
th, td { border:1px solid var(--line); padding:5px 9px; vertical-align:top; text-align:left; }
th { background:var(--soft); font-weight:650; }
td.r, th.r { text-align:right; } td.c, th.c { text-align:center; }
hr { border:0; border-top:1px solid var(--line); margin:24px 0; }
figure { margin:16px 0 22px; }
figure img { display:block; max-width:100%; height:auto; background:#fff; border:1px solid var(--line); border-radius:8px; }
figcaption { color:var(--mut); font-size:13px; margin-top:6px; }
.flowbox { margin:12px 0 18px; }
.flowbox iframe { width:100%; height:min(82vh, 860px); border:1px solid var(--line); border-radius:8px; background:var(--bg); display:block; }
.flowbox .small { font-size:13px; color:var(--mut); margin-top:4px; }
li { margin:3px 0; }
.live { display:inline-flex; gap:6px; align-items:center; padding:6px 12px; border:1px solid var(--on); border-radius:8px;
        text-decoration:none; font-size:14px; margin:0 0 12px; }
.preamble { display:flex; flex-direction:column; gap:4px; margin:14px 0 18px; padding:12px 16px; border:1px solid var(--line);
             border-left:4px solid var(--on); border-radius:8px; background:var(--soft); color:var(--ink); text-decoration:none; }
.preamble b { color:var(--on); }
.preamble q { font-style:italic; quotes:"«" "»"; }
.preamble span { font-size:13px; color:var(--mut); }
.cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(min(300px, 100%), 1fr)); gap:12px; margin:14px 0; }
.card { border:1px solid var(--line); border-radius:10px; padding:12px 14px; background:var(--card); }
.card h3 { margin:0 0 6px; font-size:16px; }
.card p { margin:0; color:var(--mut); font-size:13.5px; }
.mut { color:var(--mut); }
footer { max-width:1240px; margin:0 auto; padding:0 18px 24px; font-size:12px; color:var(--mut); }
svg text { font-family:system-ui, -apple-system, "Segoe UI", sans-serif; fill:var(--ink); }
svg .m { fill:var(--mut); } svg .ta { fill:var(--on); } svg .sa { stroke:var(--on); } svg .tb { fill:var(--sb); } svg .sb { stroke:var(--sb); }
@media (max-width:880px) {
  .layout { grid-template-columns:1fr; padding:10px; gap:10px; }
  aside { position:static; max-height:none; }
  aside ul { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:4px; }
  aside a { border:1px solid var(--line); }
  aside .toc, aside h4 + ul.toc, aside h4:nth-of-type(2) { display:none; }
  main.doc { padding:16px; }
}
@media print { header, aside { display:none; } .layout { display:block; } main.doc { border:0; } }
"""

LIST_RE = re.compile(r"^(\s*)([*-]|\d+\.)\s+(.*)$")
IMG_RE = re.compile(r"^!\[([^\]]*)\]\((img/[\w.-]+\.(?:svg|png|jpg))\)\s*$")   # рисунок — отдельной строкой


def esc(s):
    return html.escape(s, quote=False)


def slug(s, used):
    base = re.sub(r"[^\w]+", "-", s.lower(), flags=re.UNICODE).strip("-") or "раздел"
    k, n = base, 2
    while k in used:
        k = f"{base}-{n}"
        n += 1
    used.add(k)
    return k


def link_target(url):
    if re.match(r"^[a-z]+://", url) or url.startswith("#") or url.startswith("mailto:"):
        return url
    path, _, frag = url.partition("#")
    name = os.path.basename(path)
    if name == "README.md":
        name = "index.html"
    elif name.endswith(".md"):
        name = name[:-3] + ".html"
    return name + ("#" + frag if frag else "")


def inline(s):
    out = []
    for part in re.split(r"(`[^`]+`)", s):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append("<code>" + esc(part[1:-1]) + "</code>")
            continue
        t = esc(part).replace("\\|", "|")
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])", r"<i>\1</i>", t)
        t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{link_target(html.unescape(m.group(2)))}">{m.group(1)}</a>', t)
        out.append(t)
    return "".join(out)


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, i = [], "", 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur += "\\|"
            i += 2
            continue
        if s[i] == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += s[i]
        i += 1
    cells.append(cur.strip())
    return cells


def render_table(lines):
    head = split_row(lines[0])
    align = []
    body = lines[1:]
    if body and re.match(r"^\s*\|?\s*:?-{2,}", body[0]):
        for c in split_row(body[0]):
            align.append("c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else "")
        body = body[1:]
    cls = lambda i: f' class="{align[i]}"' if i < len(align) and align[i] else ""
    out = ['<div class="tw"><table><thead><tr>' + "".join(f"<th{cls(i)}>{inline(c)}</th>" for i, c in enumerate(head)) + "</tr></thead><tbody>"]
    for ln in body:
        cells = split_row(ln)
        out.append("<tr>" + "".join(f"<td{cls(i)}>{inline(c)}</td>" for i, c in enumerate(cells)) + "</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


def render_list(lines):
    out, stack, pending = [], [], []

    def flush():
        if pending:
            out.append(inline(" ".join(pending)))
            pending.clear()

    for ln in lines:
        m = LIST_RE.match(ln)
        if not m:
            if ln.strip():
                pending.append(ln.strip())
            continue
        ind, tag, text = len(m.group(1)), ("ol" if m.group(2)[0].isdigit() else "ul"), m.group(3)
        flush()
        if stack and ind > stack[-1][0] and stack[-1][2]:
            out.append(f"<{tag}>")
            stack.append([ind, tag, False])
        else:
            while stack and ind < stack[-1][0]:
                e = stack.pop()
                if e[2]:
                    out.append("</li>")
                out.append(f"</{e[1]}>")
            if not stack:
                out.append(f"<{tag}>")
                stack.append([ind, tag, False])
            elif stack[-1][2]:
                out.append("</li>")
                stack[-1][2] = False
        out.append("<li>")
        stack[-1][2] = True
        pending.append(text)
    flush()
    while stack:
        e = stack.pop()
        if e[2]:
            out.append("</li>")
        out.append(f"</{e[1]}>")
    return "".join(out)


def render(md):
    """markdown → (html, заголовок, оглавление [(уровень, id, текст)])."""
    lines = md.replace("\r\n", "\n").split("\n")
    out, toc, used, title = [], [], set(), None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + esc("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl, text = len(m.group(1)), m.group(2).strip()
            plain = re.sub(r"[`*]", "", text)
            if lvl == 1 and title is None:
                title = plain
                out.append(f"<h1>{inline(text)}</h1>")
            else:
                k = slug(plain, used)
                out.append(f'<h{lvl} id="{k}">{inline(text)}<a class="anc" href="#{k}">#</a></h{lvl}>')
                if lvl in (2, 3):
                    toc.append((lvl, k, plain))
            i += 1
            continue
        if re.match(r"^\s*-{3,}\s*$", ln):
            out.append("<hr>")
            i += 1
            continue
        if ln.strip() == "<!-- живая схема -->":
            out.append('<div class="flowembed"></div>')
            i += 1
            continue
        m = IMG_RE.match(ln.strip())
        if m:
            cap, src = m.group(1), m.group(2)
            out.append(f'<figure><a href="{src}"><img src="{src}" alt="{esc(cap)}" loading="lazy"></a>'
                       f'<figcaption>{inline(cap)}</figcaption></figure>')
            i += 1
            continue
        if ln.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append(render_table(lines[i:j]))
            i = j
            continue
        if LIST_RE.match(ln):
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    if j + 1 < len(lines) and (LIST_RE.match(lines[j + 1]) or lines[j + 1].startswith("  ")):
                        j += 1
                        continue
                    break
                if LIST_RE.match(nxt) or nxt.startswith(" ") or nxt.startswith("\t"):
                    j += 1
                    continue
                break
            out.append(render_list([x for x in lines[i:j] if x.strip()]))
            i = j
            continue
        j = i
        para = []
        while j < len(lines) and lines[j].strip() and not lines[j].startswith("```") and not re.match(r"^#{1,6}\s", lines[j]) \
                and not lines[j].lstrip().startswith("|") and not LIST_RE.match(lines[j]) and not re.match(r"^\s*-{3,}\s*$", lines[j]) \
                and not IMG_RE.match(lines[j].strip()):
            para.append(lines[j].strip())
            j += 1
        out.append("<p>" + inline(" ".join(para)) + "</p>")
        i = j
    return "\n".join(out), title, toc


def page(name, title, body, toc, live, static=False):
    li = lambda k, t: f'<li><a href="{k}.html"{" class=cur" if k == name else ""}>{esc(t)}</a></li>'
    nav = "".join(li(k, t) for k, t, _, _ in PAGES if k not in APPENDIX)
    nav_app = "".join(li(k, t) for k, t, _, _ in PAGES if k in APPENDIX)
    toc_html = "".join(f'<li class="l{lvl}"><a href="#{k}">{esc(t)}</a></li>' for lvl, k, t in toc)
    flow = "flow.html" if static else "/flow"
    live_btn = f'<a class="live" href="{flow}#{live}">▶ посмотреть вживую на живой схеме</a>' if live else ""
    # живая схема внутри документа: на сервере — живая, в опубликованной копии — демонстрация
    body = body.replace('<div class="flowembed"></div>', f"""<div class="flowbox"><iframe src="{flow}?embed=1" title="живая схема: как работает установка" loading="lazy"></iframe>
<div class="small">живая схема в окне; <a href="{flow}" target="flow">открыть целиком ↗</a></div></div>""")
    src = "README.md" if name == "index" else f"docs/{name}.md"
    # шапка — то же меню, что на экранах (server/static/nav.js): «Установка» и «Документы»; в копии — демонстрация
    links = ('<a href="plan.html">▦ Установка — демонстрация</a><a href="flow.html">↻ Как работает</a>' if static else
             '<a href="/" target="plan">▦ Установка</a><a href="/flow" target="flow">↻ Как работает</a>')
    links += '<a href="preamble.html">✦ Преамбула</a>'
    foot = ("Опубликованная копия (tools/build_docs.py --static): основной экран, пульт и наблюдение — демонстрация на снимке "
            "с имитаторов (кнопки ничего не отправляют), живая схема — на замерах; с платами всё работает на сервере в сети опыта."
            if static else
            f"Страница собрана из {src} командой tools/build_docs.py — правьте markdown. Ссылки на установку и живую схему\n"
            "работают, когда документация открыта с сервера (http://&lt;сервер&gt;:8090/docs/).")
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{"Колесо-2" if static and name == "index" else esc(title) + " · Колесо-2"}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <a class="brand" href="index.html">Колесо-2 · документация</a>
  <nav>{links}</nav>
</header>
<div class="layout">
  <aside>
    <h4>Документы</h4>
    <ul><li><a href="index.html"{" class=cur" if name == "index" else ""}>Обзор</a></li>{nav}</ul>
    <h4>Приложения</h4>
    <ul>{nav_app}</ul>
    {f'<h4>На этой странице</h4><ul class="toc">{toc_html}</ul>' if toc_html else ""}
  </aside>
  <main class="doc">
{live_btn}
{body}
  </main>
</div>
<footer>{foot}</footer>
</body>
</html>
"""


def index_page(readme_md, static=False):
    card = lambda k, t, d: f'<a class="card" href="{k}.html" style="text-decoration:none;color:inherit"><h3>{esc(t)}</h3><p>{esc(d)}</p></a>'
    cards = "".join(card(k, t, d) for k, t, d, _ in PAGES if k not in APPENDIX)
    cards_app = "".join(card(k, t, d) for k, t, d, _ in PAGES if k in APPENDIX)
    live = [("", "все режимы: выбор вверху страницы"), ("solo", "▶ эгоистичный — каждый сам"), ("dc", "эгоистичный, два ядра — конвейер"),
            ("cascade", "▶ групповой (каскад): проверяют все, шум от всех ко всем"), ("relay", "групповой · эстафета: проверяет один, ход по кругу"),
            ("group", "групповой · одно число на всех: мастер, запас, кусочки"), ("scatter", "групповой + ▼ поглощение"),
            ("pieces", "▼ россыпь"), ("chain", "▼ эстафета поглотителей"), ("silence", "▼ тишина и фон"), ("sham", "▼ пустышка (контроль)"),
            ("loop", "↺ петля: обратный шум"), ("csi", "групповой на ≋ CSI")]
    flow = "flow.html" if static else "/flow"
    live_html = "".join(f'<li><a href="{flow}{"#" + k if k else ""}">{esc(t)}</a></li>' for k, t in live)
    where = ('<li><a href="plan.html">▦ Установка</a> — основной экран: схема установки вживую, статика с Гауссом и спектром, '
             'эксперимент и лента опытов. Здесь — демонстрация на снимке с имитаторов: кнопки ничего не отправляют; пульт и наблюдение открываются его окнами.</li>'
             '<li>С платами всё работает на сервере в сети опыта: <code>http://&lt;сервер&gt;:8090/</code>.</li>' if static else
             '<li><a href="/" target="plan">▦ Установка</a> — основной экран: схема установки вживую, статика с Гауссом и спектром, '
             'эксперимент, лента опытов, запуск; подробное — окнами (статика подробно, результаты, пульт, наблюдение, живая схема, прежняя панель).</li>')
    readme_body, _, _ = render(readme_md)
    readme_body = re.sub(r"^<h1>.*?</h1>", "", readme_body, count=1)
    body = f"""<h1>Колесо-2 — документация</h1>
<p>Экспериментальная установка: платы XIAO ESP32-S3 («агенты») на двух вращающихся колёсах с катушками ищут простые числа
в физическом шуме — сами или группой, излучая и поглощая; неподвижные датчики между колёсами слушают, не меняется ли что-то.
Сервер на ноутбуке собирает телеметрию, раздаёт настройки и прошивки, ведёт эксперименты — списки опытов.
Описано текущее состояние (27.10.2026); как пришли к нему — <a href="history.html">приложение А</a>.</p>
<a class="preamble" href="preamble.html"><b>✦ Преамбула — зачем этот опыт</b>
<q>Это не измерение. Это проверка онтологии.</q>
<span>Топология до пространства-времени; порядок познания: эстетика — этика — гносеология — онтология — наука;
ошибка первого колеса; критерий — корреляция независимых каналов. Авторы: bridge42worlds, Λura13 (DeepSeek), Остов (Claude).</span></a>
<figure><a href="img/wiring_overview.svg"><img src="img/wiring_overview.svg" alt="Установка целиком" loading="lazy"></a>
<figcaption>Установка целиком: колёса друг напротив друга, агенты на шине, катушки, моторы на раме, статика между колёсами —
подробно в <a href="wiring.html">схемах подключения</a>.</figcaption></figure>
<h2 id="pages">Где смотреть</h2>
<ul>
{where}
<li><a href="{flow}">↻ Как работает</a> — живая схема: установка целиком (кто кому что шлёт) и протокол группы по шагам, замедленно.</li>
</ul>
<h3 id="modes">Режимы на живой схеме</h3>
<ul>{live_html}</ul>
<h2 id="docs">Документы</h2>
<div class="cards">{cards}</div>
<h2 id="app">Приложения</h2>
<div class="cards">{cards_app}</div>
<h2 id="readme">Папки проекта</h2>
{readme_body}"""
    toc = [(2, "pages", "Где смотреть"), (2, "docs", "Документы"), (2, "app", "Приложения"), (2, "readme", "Папки проекта")]
    return page("index", "Обзор", body, toc, "", static)


def copy_images(out_dir):
    """docs/img → <out_dir>/img (только изменённые); список «img/файл»."""
    if not os.path.isdir(IMG):
        return []
    os.makedirs(os.path.join(out_dir, "img"), exist_ok=True)
    names = sorted(f for f in os.listdir(IMG) if re.match(r"^[\w.-]+\.(?:svg|png|jpg)$", f))
    for f in names:
        src, dst = os.path.join(IMG, f), os.path.join(out_dir, "img", f)
        if not os.path.exists(dst) or open(src, "rb").read() != open(dst, "rb").read():
            shutil.copyfile(src, dst)
    return ["img/" + f for f in names]


def build(quiet=False):
    os.makedirs(OUT, exist_ok=True)
    copy_images(OUT)
    n = 0
    for name, title, _, live in PAGES:
        path = os.path.join(DOCS, name + ".md")
        if not os.path.exists(path):
            continue
        body, h1, toc = render(open(path, encoding="utf-8").read())
        out = page(name, h1 or title, body, toc, live)
        target = os.path.join(OUT, name + ".html")
        if not os.path.exists(target) or open(target, encoding="utf-8").read() != out:
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(out)
        n += 1
    readme = os.path.join(ROOT, "README.md")
    out = index_page(open(readme, encoding="utf-8").read() if os.path.exists(readme) else "")
    target = os.path.join(OUT, "index.html")
    if not os.path.exists(target) or open(target, encoding="utf-8").read() != out:
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(out)
    if not quiet:
        print(f"документация: {n + 1} страниц → {os.path.relpath(OUT, ROOT)}")
    return n + 1


# Что не должно уйти наружу: адреса сети опыта и её имя (имя — из secrets/wifi.ini, в код не пишется).
def _ssid():
    p = os.path.join(ROOT, "secrets", "wifi.ini")
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            k, _, v = ln.partition("=")
            if k.strip().lower() == "ssid" and v.strip():
                return v.strip()
    return None


SSID = _ssid()
SCRUB = [(re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "&lt;адрес сервера&gt;")]
if SSID:
    SCRUB += [(re.compile(r"(сети|сеть)\s*«?" + re.escape(SSID) + r"»?(\s*·\s*)?"), "")]
LEAK = re.compile((r"(?<![\w])" + re.escape(SSID) + r"(?![\w])|" if SSID else "") + r"\b192\.168\.\d|ota\.key\s*=|wifi\.ini\s*=")


def scrub(text, name):
    for rx, to in SCRUB:
        text = rx.sub(to, text)
    m = LEAK.search(text)
    if m:
        raise SystemExit(f"{name}: осталось «{m.group(0)}» — не публикуется")
    return text


def unwrap(doc):
    """Главная страница комплекта: без <!doctype>/<html>/<head>/<body> — обёртку добавит площадка."""
    head = re.search(r"<head>(.*?)</head>", doc, re.S).group(1)
    head = re.sub(r"<meta [^>]*>\s*", "", head).strip()
    body = re.search(r"<body>(.*)</body>", doc, re.S).group(1).strip()
    return head + "\n" + body + "\n"


DEMO_NOTE = ('<span class="pill warn" title="Опубликованная копия: снимок данных с имитаторов агентов (tools/demo_capture.py); '
             'с платами экран работает на сервере в сети опыта">демонстрация · снимок с имитаторов · кнопки ничего не отправляют</span>')


# Меню экранов в комплекте — без nav.js: «Установка» (демонстрация основного экрана) и «Документы»; в окне — без меню
STATIC_NAV = ('<nav class="w2nav w2-noembed" style="display:inline-flex;gap:8px;font-size:.9em">'
              '<a href="plan.html">▦ Установка</a><a href="index.html">? Документы</a></nav>'
              '<script>if (location.search.indexOf("embed") >= 0 || window.self !== window.top) '
              'document.write("<style>.w2-noembed{display:none!important}</style>")</script>')


def static_screens():
    """Основной экран, пульт, наблюдение и результаты в комплект: общее (ui.css, ui.js) — внутрь страницы, ответы сервера —
    из снимка build/demo/demo.json (tools/demo_capture.py). Снимка нет — экраны в комплект не идут."""
    demo_path = os.path.join(ROOT, "build", "demo", "demo.json")
    if not os.path.exists(demo_path):
        print("экраны: нет снимка build/demo/demo.json (tools/demo_capture.py) — в комплект не идут")
        return {}
    demo = open(demo_path, encoding="utf-8").read()
    if LEAK.search(demo) or re.search(r"wifi\.ini|ota\.key", demo):
        raise SystemExit("снимок: адрес или имя сети — не публикуется")
    demo = demo.replace("</", "<\\/")
    st = os.path.join(ROOT, "server", "static")
    css = open(os.path.join(st, "ui.css"), encoding="utf-8").read()
    js = open(os.path.join(st, "ui.js"), encoding="utf-8").read()
    out = {}
    for name in ("plan", "pult", "watch", "report"):
        html = scrub(open(os.path.join(st, name + ".html"), encoding="utf-8").read(), name + ".html")
        for a, b in [('<link rel="stylesheet" href="/ui.css">', "<style>\n" + css + "\n</style>"),
                     ('<span id="conn" class="small"></span>', '<span id="conn" class="small"></span>' + DEMO_NOTE),
                     ('<script src="/ui.js"></script>', "<script>window.DEMO_DATA = @@DEMO@@;</script>\n<script>\n" + js + "\n</script>"),
                     ('<script src="/nav.js"></script>', ""),
                     ('<span id="nav"></span>', '<span id="nav">' + STATIC_NAV + "</span>")]:
            if html.count(a) != 1:
                raise SystemExit(f"{name}.html: не найдено «{a[:40]}…» — поправьте static_screens")
            html = html.replace(a, b)
        for a, b in [(' target="watch"', ""), (' target="old"', ""), (' target="docs"', ""), ('href="/watch"', 'href="watch.html"'),
                     ('href="/" title="Прежняя панель со всеми счётчиками">Подробно ↗', 'href="index.html" title="Документация">Документация ↗'),
                     ('href="/docs/', 'href="')]:
            html = html.replace(a, b)
        if name == "watch":
            html = html.replace('<span id="clock" class="num"></span>',
                                '<span id="clock" class="num"></span><a class="btn" href="pult.html">Пульт ↗</a><a class="btn" href="index.html">Документация ↗</a>')
        out[name + ".html"] = html.replace("@@DEMO@@", demo)
    return out


def build_static(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    files = {}
    for name, title, _, live in PAGES:
        path = os.path.join(DOCS, name + ".md")
        if os.path.exists(path):
            body, h1, toc = render(open(path, encoding="utf-8").read())
            files[name + ".html"] = page(name, h1 or title, body, toc, live, static=True)
    readme = os.path.join(ROOT, "README.md")
    readme_md = open(readme, encoding="utf-8").read() if os.path.exists(readme) else ""
    files["index.html"] = unwrap(index_page(readme_md, static=True))
    flow = open(os.path.join(ROOT, "server", "static", "flow.html"), encoding="utf-8").read()
    for a, b in [("const STATIC_BUILD = false;", "const STATIC_BUILD = true;"),
                 ('  <span id="nav"></span>\n', '  <nav class="small"><a href="index.html">документация</a></nav>\n'),
                 ('<script src="/nav.js"></script>\n', '')]:      # меню страниц — только на сервере
        if flow.count(a) != 1:
            raise SystemExit(f"flow.html: не найдено «{a[:40]}…» — поправьте build_static")
        flow = flow.replace(a, b)
    files["flow.html"] = flow
    files.update(static_screens())
    for fn, text in files.items():
        with open(os.path.join(out_dir, fn), "w", encoding="utf-8", newline="\n") as f:
            f.write(text if fn in ("plan.html", "pult.html", "watch.html", "report.html") else scrub(text, fn))   # экраны проверены в static_screens
    imgs = copy_images(out_dir)
    for fn in imgs:
        if fn.endswith(".svg"):
            scrub(open(os.path.join(out_dir, fn), encoding="utf-8").read(), fn)
    print(f"комплект: {len(files) + len(imgs)} файлов → {os.path.relpath(out_dir, ROOT)} (главная — index.html)")
    return sorted(files) + imgs


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--static" in sys.argv:
        i = sys.argv.index("--static")
        build_static(sys.argv[i + 1] if len(sys.argv) > i + 1 else os.path.join(ROOT, "build", "site"))
    else:
        build(quiet="--quiet" in sys.argv)
