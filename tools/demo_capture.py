"""Снимок данных сервера для демонстрации основного экрана, пульта и наблюдения в опубликованной копии.

    python tools/demo_capture.py [адрес сервера] [файл]
    по умолчанию http://127.0.0.1:8093 → build/demo/demo.json

Снимать с тестового сервера на имитаторах (tools/sim_gen.py, tools/sim_agents.py), не с настоящего:
в копию уходит всё, что видит панель. Платы на USB (настоящие MAC) из снимка убираются.
Ключи — как их спрашивают экраны: путь и, где нужно, ?mac=… или ?id=… (ui.js, demoGet).
"""
import json, os, sys, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8093"
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "build", "demo", "demo.json")
    get = lambda p: json.load(urllib.request.urlopen(base + p, timeout=20))
    snap = {}
    d = get("/api/dash?window=10")
    d["usb"] = {"boards": [], "err": None, "job": None}          # настоящие платы на USB — не публикуются
    d["agents"] = [a for a in d["agents"] if a.get("status") != "never"]   # из реестра, не выходившие — не показывать
    snap["/api/dash"] = d
    snap["__t"] = d["time"]
    for s in d.get("static") or []:
        snap[f"/api/static/spectrum?mac={s['mac']}"] = get(f"/api/static/spectrum?n=6&mac={urllib.parse.quote(s['mac'])}")
    runs = get("/api/runs?n=30")
    snap["/api/runs"] = runs
    for r in runs.get("list", [])[:15]:
        snap[f"/api/run?id={r['id']}"] = get(f"/api/run?id={r['id']}")
    progs = get("/api/programs")
    snap["/api/programs"] = progs
    for p in ([progs["active"]] if progs.get("active") else []) + (progs.get("list") or [])[:3]:
        snap[f"/api/program?id={p['id']}"] = get(f"/api/program?id={p['id']}")
    snap["/api/agent_events"] = get("/api/agent_events?n=50")
    for a in d["agents"]:
        if a.get("status") != "never":
            snap[f"/api/agent_events?mac={a['mac']}"] = get(f"/api/agent_events?n=25&mac={urllib.parse.quote(a['mac'])}")
    # основной экран: моторы и катушки, живая статика (Гаусс, спектр, колебания) и поле по датчикам
    snap["/api/motors"] = get("/api/motors")
    snap["/api/coils"] = get("/api/coils")
    for s in d.get("static") or []:
        snap[f"/api/static/live?mac={s['mac']}"] = get(f"/api/static/live?sec=10&mac={urllib.parse.quote(s['mac'])}")
    for a in d["agents"]:
        if a.get("status") != "never" and (a.get("sens") or 0) & 2:
            try:
                snap[f"/api/field/live?mac={a['mac']}"] = get(f"/api/field/live?sec=60&mac={urllib.parse.quote(a['mac'])}")
            except Exception:                          # noqa: BLE001 — поля может не быть
                pass
    os.makedirs(os.path.dirname(out), exist_ok=True)
    text = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"снимок: {len(snap) - 1} ответов, {len(text) // 1024} КБ → {os.path.relpath(out, ROOT)}")


if __name__ == "__main__":
    main()
