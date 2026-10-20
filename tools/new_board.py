"""Первая прошивка новых плат по USB — одной командой (двойной щелчок по tools/new_board.cmd)
или кнопкой «Прошить по USB» на панели (раздел «Новые платы по USB»).

    python tools/new_board.py [--all] [--list]

Новая плата прошивается по USB один раз: так в неё попадают prime_lab с прошивкой
по Wi-Fi (OTA), данные сети и ключ подписи. Дальше её обновляют по Wi-Fi вместе со
всеми («Прошить всех по Wi-Fi» на панели или tools/ota.py).

Что делает: находит платы ESP32-S3 на USB (серийный номер USB у них — MAC: плату не
нужно перезагружать, чтобы узнать, кто это); тех, кого нет в реестре, записывает в
registry/devices.csv следующими номерами, собирает прошивку один раз и заливает в
каждую. Через ~10 с после заливки плата выходит на связь и появляется на панели в
«Агентах и группах» с отметкой «новый» — поставьте ей колесо и группу.

    --all   перепрошить по USB и уже записанные платы
    --list  только показать, что подключено

Плата не видна или заливка не идёт — зажмите на ней BOOT, переподключите кабель,
отпустите: в режиме загрузчика её видно всегда.
"""
import argparse, collections, csv, glob, os, re, shutil, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = os.path.join(ROOT, "registry", "devices.csv")
MAC_RE = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}")
ESPRESSIF_VID = 0x303A


def bash():
    for p in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if p and os.path.exists(p):
            return p
    raise RuntimeError("не найден bash (Git for Windows) — он нужен для tools/build.sh")


def esptool():
    c = sorted(glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Arduino15", "packages", "esp32",
                                      "tools", "esptool_py", "*", "esptool.exe")))
    return c[-1] if c else None


def scan():
    """Платы Espressif на USB: [(порт, mac или None)]. MAC — серийный номер USB, плату не трогаем;
    None — плата в своём режиме USB (номер не MAC): её MAC прочитает загрузчик при прошивке."""
    try:
        from serial.tools import list_ports
    except ImportError:
        raise RuntimeError("нужен pyserial: python -m pip install pyserial")
    out = []
    for p in list_ports.comports():
        if p.vid == ESPRESSIF_VID:
            sn = (p.serial_number or "").lower()
            out.append((p.device, sn if MAC_RE.fullmatch(sn) else None))
    return sorted(out, key=lambda x: (len(x[0]), x[0]))


def read_mac(port):
    """MAC через загрузчик (плата перезапустится)."""
    et = esptool()
    if not et:
        return None
    r = subprocess.run([et, "--port", port, "read-mac"], capture_output=True, text=True, errors="replace", timeout=60)
    m = re.search(r"MAC: *((?:[0-9a-f]{2}:){5}[0-9a-f]{2})", r.stdout, re.I)
    return m.group(1).lower() if m else None


def registry():
    with open(REG, encoding="utf-8") as f:
        return {r["mac"].strip().lower(): r for r in csv.DictReader(f)}


def _run(cmd, log, env=None):
    log("$ " + " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd)))
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})
    for line in p.stdout:
        if line.strip():
            log("  " + line.rstrip())
    if p.wait():
        raise RuntimeError(f"{os.path.basename(cmd[1] if len(cmd) > 1 else cmd[0])}: код {p.returncode}")


def flash(todo, log=print, step=lambda s: None):
    """Прошить платы [(порт, mac или None)]: записать в реестр, собрать раз, залить в каждую.
    Возвращает [(порт, mac, номер)]."""
    todo = list(todo)
    for i, (port, mac) in enumerate(todo):
        if mac is None:
            step(f"читаю MAC {port}")
            mac = read_mac(port)
            if not mac:
                raise RuntimeError(f"{port}: MAC не прочитан — зажмите BOOT, переподключите кабель, отпустите и повторите")
            log(f"{port}: MAC {mac}")
            todo[i] = (port, mac)
    reg = registry()
    for _, mac in todo:                                # в реестр — до сборки: прошивка знает номера плат
        if mac not in reg:
            _run([sys.executable, os.path.join("tools", "register.py"), mac], log)
    sh = bash()
    step("сборка прошивки (1–3 мин)")
    _run([sh, "tools/build.sh", "prime_lab"], log)     # одна сборка на всех
    for n, (port, mac) in enumerate(todo, 1):
        step(f"заливка {port} ({n} из {len(todo)})")
        _run([sh, "tools/build.sh", "prime_lab", port], log, env={"SKIP_BUILD": "1"})
    reg = registry()
    return [(port, mac, int(reg[mac]["id"]) if mac in reg else None) for port, mac in todo]


class Job:
    """Прошивка в фоне для сервера: одна за раз, ход — в brief()."""

    def __init__(self, on_done=None):
        self.lock = threading.Lock()
        self.busy, self.t0, self.t1, self.step_text = False, None, None, ""
        self.ports, self.result, self.error = [], None, None
        self.lines = collections.deque(maxlen=400)
        self.on_done = on_done

    def log(self, line):
        with self.lock:
            self.lines.append(line)

    def step(self, text):
        with self.lock:
            self.step_text = text
        self.log("— " + text)

    def start(self, todo):
        with self.lock:
            if self.busy:
                raise ValueError("прошивка по USB уже идёт")
            self.busy, self.t0, self.t1, self.step_text = True, time.time(), None, "начало"
            self.ports, self.result, self.error = [p for p, _ in todo], None, None
            self.lines.clear()
        threading.Thread(target=self._go, args=(list(todo),), daemon=True).start()

    def _go(self, todo):
        res = err = None
        try:
            res = flash(todo, self.log, self.step)
        except Exception as e:                         # noqa: BLE001 — любая ошибка — в панель
            err = str(e)
            self.log("✗ " + err)
        with self.lock:
            self.busy, self.t1, self.result, self.error = False, time.time(), res, err
            self.step_text = "готово" if res else "ошибка"
        if self.on_done:
            self.on_done(res, err)

    def brief(self):
        with self.lock:
            return {"busy": self.busy, "t0": self.t0, "t1": self.t1, "step": self.step_text, "ports": self.ports,
                    "result": [{"port": p, "mac": m, "id": i} for p, m, i in self.result or []],
                    "error": self.error, "log": list(self.lines)[-40:]}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    try:
        found = scan()
    except RuntimeError as e:
        raise SystemExit(str(e))
    reg = registry()
    if not found:
        print("плат ESP32-S3 на USB нет. Подключите плату кабелем с передачей данных "
              "(кабель «только для зарядки» Windows не видит вовсе).")
        return
    print("на USB:")
    for port, mac in found:
        r = reg.get(mac)
        print(f"  {port}: " + (f"{mac} — №{r['id']} в реестре" if r else f"{mac} — новая, в реестре нет" if mac
                               else "MAC не виден (плата в своём режиме USB) — узнаем при прошивке"))
    if a.list:
        return
    todo = [(port, mac) for port, mac in found if a.all or mac not in reg]
    if not todo:
        print("новых плат нет: записанные в реестр обновляются по Wi-Fi (--all — перепрошить их по USB)")
        return
    try:
        done = flash(todo, step=lambda s: print("— " + s, flush=True))
    except RuntimeError as e:
        raise SystemExit("✗ " + str(e))
    print("\nготово:")
    for port, mac, nid in done:
        print(f"  №{nid} ({mac}, {port}) прошита — через ~10 с появится на панели с отметкой «новый»")
    print("Дальше её можно питать любым кабелем, прошивку обновлять по Wi-Fi.")


if __name__ == "__main__":
    main()
