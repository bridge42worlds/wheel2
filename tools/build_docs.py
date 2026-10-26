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
    ("modes", "Режимы", "Все режимы установки в одном месте: стоп, пуск, тушение, отдых; генерация (каждый сам, "
     "два ядра, все на одном числе, эстафета, каскад); шум; поглощение (сет B); статические; телеметрия; программа.", ""),
    ("ui_concept", "Концепт: пульт и наблюдение", "Два экрана: пульт (36 агентов, группы и режимы, опыт, программа "
     "«план — факт», журнал) и наблюдение (статика и простые в реальном времени). Черновик для обсуждения.", ""),
    ("how_it_works", "Как всё работает", "Система целиком: агент и его ядра, источники шума, поиск простого, сообща, "
     "предел температуры, параметры, сессии, страницы, телеметрия, прошивка по Wi-Fi, замеры, инструменты.", ""),
    ("group_protocol", "Сообща: протокол группы", "Все на одном числе (третья версия): число с итогом, базы по кругу, "
     "запас и кусочки, сбои, когерентность, согласие, результаты на 1–4 платах. Эстафета и каскад: число из шума всех, "
     "проверяет один или все, оценка по модели.", "group"),
    ("scatter", "Рассеяние: сет B", "Как простое попадает к сету B, режимы (каждый, россыпь, эстафета, тишина), пустышка, "
     "фон, функции, окна по местам, петля через эфир, проверка на четырёх платах.", "pieces"),
    ("randomness", "Откуда берётся случайность", "ГСЧ чипа и шум радио, CSI, АЦП, дрожание RC: как берём, сколько "
     "засчитываем, как смешиваем, скорость по источникам и длинам.", "solo"),
    ("thermal", "Температура агентов", "Предел 70 °C: регулятор доли работы, замеры нагрева и остывания, откуда тепло, "
     "частота, отдых по очереди, спринт после отдыха, отдых между режимами, рейтинг агентов по истории ошибок.", ""),
    ("program", "Программа эксперимента", "Шаги, контроль, случайный порядок, эталон шума, аномалии и их проверка "
     "парами, испорченные шаги.", ""),
    ("static_watch", "Статические акселерометры", "Роль S: что меряет агент, что считает сервер, эталон шума, линии, "
     "полосы, толчки, вывод «продолжать / повторить / проверить».", ""),
    ("sensors", "Датчики", "Колесо (обороты и невесомость), магнитное поле, статический акселерометр; подключение и пакеты.", ""),
    ("wiring", "Схемы подключения", "Общая схема установки; колесо: питание агентов по шине от своих аккумуляторов, управление катушкой "
     "(5,1 В, DRV8833); рама: моторы колёс (блок 12 В, агент моторов, два DRV8871). Полярность диодов и конденсаторов.", ""),
]
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
li { margin:3px 0; }
.live { display:inline-flex; gap:6px; align-items:center; padding:6px 12px; border:1px solid var(--on); border-radius:8px;
        text-decoration:none; font-size:14px; margin:0 0 12px; }
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
    nav = "".join(f'<li><a href="{k}.html"{" class=cur" if k == name else ""}>{esc(t)}</a></li>' for k, t, _, _ in PAGES)
    toc_html = "".join(f'<li class="l{lvl}"><a href="#{k}">{esc(t)}</a></li>' for lvl, k, t in toc)
    flow = "flow.html" if static else "../flow"
    live_btn = f'<a class="live" href="{flow}#{live}">▶ посмотреть вживую на живой схеме</a>' if live else ""
    src = "README.md" if name == "index" else f"docs/{name}.md"
    # с сервера — то же меню страниц, что на экранах (server/static/nav.js): каждая страница — своя вкладка
    links = ('<a href="pult.html">пульт</a><a href="watch.html">наблюдение</a><a href="flow.html">живая схема</a>' if static else
             "".join(f'<a href="{h}" target="{k}">{t}</a>' for k, h, t in (
                 ("plan", "/plan", "▦ Планшетка"), ("acc", "/acc", "◆ Статика"), ("watch", "/watch", "◉ Наблюдение"),
                 ("report", "/report", "☰ Отчёт"), ("pult", "/pult", "⚙ Пульт"), ("old", "/", "☷ Подробно"),
                 ("flow", "/flow", "↻ Схема"))))
    foot = ("Опубликованная копия (tools/build_docs.py --static): пульт и наблюдение — демонстрация на снимке с имитаторов, "
            "живая схема — на замерах; с платами всё работает только на сервере в сети опыта." if static else
            f"Страница собрана из {src} командой tools/build_docs.py — правьте markdown. Ссылки на живую схему и панель\n"
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


SYSTEM_SVG = """
<svg viewBox="0 0 860 330" role="img" aria-label="схема системы" style="width:100%;max-width:860px;display:block;margin:6px 0 4px">
  <defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
    <path d="M0 0 L10 5 L0 10 z" fill="#8a8f98"/></marker></defs>
  <g font-size="13">
    <rect x="20" y="20" width="230" height="64" rx="10" fill="none" stroke="#8a8f98"/>
    <text x="34" y="44" font-weight="700">▣ сервер (ПК)</text>
    <text x="34" y="64" font-size="12" class="m">телеметрия · команды · журнал · страницы</text>
    <rect x="315" y="20" width="200" height="64" rx="10" fill="none" stroke="#8a8f98"/>
    <text x="329" y="44" font-weight="700">⌂ роутер Wi-Fi</text>
    <text x="329" y="64" font-size="12" class="m">свой канал</text>
    <path d="M250 52 L315 52" stroke="#8a8f98" marker-end="url(#ar)"/>
    <text x="560" y="40" font-size="12" class="m">UDP 5005 — телеметрия раз в 1 с</text>
    <text x="560" y="58" font-size="12" class="m">UDP 5007 — команды CFG, OTA</text>
    <text x="560" y="76" font-size="12" class="m">UDP 5006 — маяк сервера</text>
    <rect x="20" y="130" width="470" height="120" rx="12" fill="none" class="sa" stroke-dasharray="5 4"/>
    <text x="34" y="150" font-weight="700" class="ta">сет A — генерация</text>
    <g>
      <circle cx="80" cy="200" r="26" fill="none" class="sa" stroke-width="2"/><text x="80" y="205" text-anchor="middle" font-weight="700">№3</text>
      <circle cx="190" cy="200" r="26" fill="none" stroke="#E69F00" stroke-width="3"/><text x="190" y="205" text-anchor="middle" font-weight="700">№4</text>
      <text x="190" y="242" text-anchor="middle" font-size="12" fill="#E69F00">★ мастер</text>
      <circle cx="300" cy="200" r="26" fill="none" class="sa" stroke-width="2"/><text x="300" y="205" text-anchor="middle" font-weight="700">№5</text>
      <circle cx="410" cy="200" r="26" fill="none" class="sa" stroke-width="2"/><text x="410" y="205" text-anchor="middle" font-weight="700">№6</text>
      <path d="M106 200 L162 200 M216 200 L272 200 M326 200 L382 200" stroke="#8a8f98" stroke-dasharray="3 3" marker-end="url(#ar)"/>
    </g>
    <rect x="560" y="130" width="280" height="120" rx="12" fill="none" class="sb" stroke-dasharray="5 4"/>
    <text x="574" y="150" font-weight="700" class="tb">сет B — рассеяние</text>
    <path d="M640 180 L660 200 L640 220 L620 200 Z" fill="none" class="sb" stroke-width="2"/>
    <path d="M760 180 L780 200 L760 220 L740 200 Z" fill="none" class="sb" stroke-width="2"/>
    <text x="700" y="238" text-anchor="middle" font-size="12" class="m">смешивают простое со своим шумом</text>
    <path d="M436 190 C 500 150, 560 150, 616 190" stroke="#CC79A7" fill="none" marker-end="url(#ar)"/>
    <text x="526" y="150" text-anchor="middle" font-size="12" fill="#CC79A7">PRIME</text>
    <path d="M620 215 C 560 270, 480 270, 436 215" stroke="#CC79A7" fill="none" stroke-dasharray="4 3" marker-end="url(#ar)"/>
    <text x="526" y="282" text-anchor="middle" font-size="12" fill="#CC79A7">эфир B → шум A (петля)</text>
    <text x="34" y="312" font-size="12" class="m">ESP-NOW между агентами на канале роутера: адресные кадры группы (итог, ответы, кусочки) и широковещательные — сету B</text>
  </g>
</svg>
"""


def index_page(readme_md, static=False):
    cards = "".join(f'<a class="card" href="{k}.html" style="text-decoration:none;color:inherit"><h3>{esc(t)}</h3><p>{esc(d)}</p></a>'
                    for k, t, d, _ in PAGES)
    live = [("", "все режимы: выбор вверху страницы"), ("solo", "каждый сам"), ("dc", "два ядра — конвейер"),
            ("group", "сообща: мастер, эстафета, запас"), ("relay", "эстафета сета A: проверяет один, ход по кругу"), ("cascade", "каскад сета A: проверяют все, шум от всех ко всем"), ("scatter", "сообща + рассеяние"), ("pieces", "россыпь"),
            ("chain", "эстафета сета B — цепочка рассеяния"), ("silence", "тишина и фон"), ("sham", "пустышка"), ("loop", "петля через эфир"),
            ("csi", "сообща на медленном шуме (CSI)")]
    flow = "flow.html" if static else "../flow"
    live_html = "".join(f'<li><a href="{flow}{"#" + k if k else ""}">{esc(t)}</a></li>' for k, t in live)
    panel = ('<li><a href="pult.html">Пульт</a> — экран 1: агенты по колёсам, группы и режимы, программа «план — факт», '
             'журнал опытов со статикой, телеметрия. Здесь — демонстрация на снимке с имитаторов: кнопки ничего не отправляют.</li>'
             '<li><a href="watch.html">Наблюдение</a> — экран 2: спектры статических акселерометров против эталона, '
             'страж статистики простых, лента опыта (демонстрация).</li>'
             '<li>С платами пульт, наблюдение и прежняя панель работают на сервере в сети опыта '
             '(<code>http://&lt;сервер&gt;:8090/pult</code>, <code>/watch</code>, <code>/</code>).</li>' if static else
             '<li><a href="../">Главная панель</a> — агенты, управление, сессии, итоги, графики, журнал опытов, '
             'прошивка по Wi-Fi; у каждого параметра значок ⓘ.</li>')
    acc = "" if static else '<li><a href="../acc">Второй экран</a> — статические акселерометры: спектр, эталон шума, вывод.</li>'
    readme_body, _, _ = render(readme_md)
    readme_body = re.sub(r"^<h1>.*?</h1>", "", readme_body, count=1)
    body = f"""<h1>Колесо-2 — документация</h1>
<p>Агенты — платы XIAO ESP32-S3 с общей прошивкой <code>prime_lab</code>: генерируют простые числа (каждый сам или сообща),
рассеивают их (сет B), меряют ускорение (роль S). Сервер на ПК собирает телеметрию, раздаёт настройки и прошивки,
ведёт сессии, программы и журнал опытов.</p>
{SYSTEM_SVG}
<h2 id="pages">Живые страницы</h2>
<ul>
{panel}
<li><a href="{flow}">Живая схема</a> — протокол по шагам, замедленно: шум, кандидат, отсев, Миллер — Рабин, мастер и эстафета, кусочки, рассеяние, петля; характерные времена «модель / на платах».</li>
{acc}
</ul>
<h3 id="modes">Режимы на живой схеме</h3>
<ul>{live_html}</ul>
<h2 id="docs">Документы</h2>
<div class="cards">{cards}</div>
<h2 id="readme">Папки проекта</h2>
{readme_body}"""
    toc = [(2, "pages", "Живые страницы"), (2, "docs", "Документы"), (2, "readme", "Папки проекта")]
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


def static_screens():
    """Пульт и наблюдение в комплект: общее (ui.css, ui.js) — внутрь страницы, ответы сервера — из снимка
    build/demo/demo.json (tools/demo_capture.py). Снимка нет — экраны в комплект не идут."""
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
    for name in ("pult", "watch"):
        html = scrub(open(os.path.join(st, name + ".html"), encoding="utf-8").read(), name + ".html")
        for a, b in [('<link rel="stylesheet" href="/ui.css">', "<style>\n" + css + "\n</style>"),
                     ('<span id="conn" class="small"></span>', '<span id="conn" class="small"></span>' + DEMO_NOTE),
                     ('<script src="/ui.js"></script>', "<script>window.DEMO_DATA = @@DEMO@@;</script>\n<script>\n" + js + "\n</script>")]:
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
            f.write(text if fn in ("pult.html", "watch.html") else scrub(text, fn))   # экраны проверены в static_screens
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
