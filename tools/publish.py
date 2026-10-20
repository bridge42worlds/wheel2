# -*- coding: utf-8 -*-
"""Открытая копия проекта на GitHub: снимок ветки main одним коммитом поверх ветки public.

    python tools/publish.py              проверить и сделать коммит в ветке public
    python tools/publish.py --push       то же и отправить: public → github/main

Рабочая история (ветка main) остаётся только на этом компьютере: в открытую копию уходит лишь
дерево файлов, автор — bridge42worlds. Перед коммитом — проверка на утечки: имя и пароль сети из
secrets/wifi.ini, ключ secrets/ota.key, адреса локальной сети, адреса почты. Найдено — ничего не
публикуется, печатается файл и что найдено (сами секреты — никогда).
"""
import os, re, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = "github"
AUTHOR = {"GIT_AUTHOR_NAME": "bridge42worlds", "GIT_AUTHOR_EMAIL": "bridge42worlds@users.noreply.github.com",
          "GIT_COMMITTER_NAME": "bridge42worlds", "GIT_COMMITTER_EMAIL": "bridge42worlds@users.noreply.github.com"}
sys.stdout.reconfigure(encoding="utf-8")


def git(*args, env=None, check=True):
    r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, env={**os.environ, **(env or {})})
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode("utf-8", "replace").strip()


def secrets():
    """Что не должно уйти наружу: (что это, значение). Значения не печатаются."""
    out = []
    ini = os.path.join(ROOT, "secrets", "wifi.ini")
    if os.path.exists(ini):
        for ln in open(ini, encoding="utf-8"):
            k, _, v = ln.partition("=")
            k, v = k.strip().lower(), v.strip()
            if k == "ssid" and v:
                out.append(("имя сети", v))
            if k == "password" and len(v) >= 6:
                out.append(("пароль сети", v))
    key = os.path.join(ROOT, "secrets", "ota.key")
    if os.path.exists(key):
        out.append(("ключ OTA", open(key, encoding="utf-8").read().strip()))
    return out


LAN = re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
MAIL = re.compile(r"[\w.+-]+@[\w-]+\.(?:com|ru|net|org|io)\b")
MAIL_OK = re.compile(r"@(users\.noreply\.github\.com|anthropic\.com|example\.(com|org))$")


def leaks(tree):
    """Пройти все файлы дерева: найденные утечки — список строк «файл: что»."""
    found, sec = [], secrets()
    for path in git("ls-tree", "-r", "--name-only", tree).splitlines():
        data = subprocess.run(["git", "-C", ROOT, "show", f"{tree}:{path}"], capture_output=True).stdout
        if b"\0" in data[:4096]:
            continue                                   # двоичный файл
        text = data.decode("utf-8", "replace")
        for what, v in sec:
            if what == "имя сети":                     # короткое имя — только отдельным словом
                if re.search(r"(?<![\w])" + re.escape(v) + r"(?![\w])", text):
                    found.append(f"{path}: {what}")
            elif v in text:
                found.append(f"{path}: {what}")
        for m in LAN.finditer(text):
            found.append(f"{path}: адрес локальной сети {m.group(0)}")
        for m in MAIL.finditer(text):
            if not MAIL_OK.search(m.group(0)):
                found.append(f"{path}: адрес почты {m.group(0)}")
    return found


def main():
    push = "--push" in sys.argv
    msg = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("в рабочей папке есть незакоммиченные изменения — сначала коммит в main")
    tree = git("rev-parse", "main^{tree}")
    bad = leaks(tree)
    if bad:
        print("✗ не публикуется — найдено:")
        for b in bad:
            print("  ", b)
        raise SystemExit(1)
    parent = git("rev-parse", "--verify", "-q", "public", check=False)
    if parent and git("rev-parse", f"{parent}^{{tree}}") == tree:
        print("✓ в ветке public уже этот снимок")
    else:
        msg = msg or f"Снимок проекта от {time.strftime('%d.%m.%Y')}"
        args = ["commit-tree", tree, "-m", msg] + (["-p", parent] if parent else [])
        commit = git(*args, env=AUTHOR)
        git("update-ref", "refs/heads/public", commit)
        print(f"✓ ветка public: {commit[:7]} — {msg.splitlines()[0]}")
    if push:
        if not git("remote", "get-url", REMOTE, check=False):
            raise SystemExit(f"нет удалённого репозитория «{REMOTE}»: git remote add {REMOTE} https://github.com/bridge42worlds/wheel2.git")
        r = subprocess.run(["git", "-C", ROOT, "push", REMOTE, "public:main"])
        if r.returncode:
            raise SystemExit("отправка не удалась")
        print("✓ отправлено: github.com/bridge42worlds/wheel2")


if __name__ == "__main__":
    main()
