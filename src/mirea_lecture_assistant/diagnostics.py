"""What an AI on duty (or a person) needs to tell what is wrong, without secrets.

* a network check: are MIREA's sites reachable, is the internet, is a VPN on;
* the recent warnings and errors of the log, as event names only;
* a report scrubbed of names, addresses, codes and personal paths;
* the list of MIREA and MTS Link addresses a VPN should leave alone.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

MIREA_SITES = ("sso.mirea.ru", "pulse.mirea.ru", "online-edu.mirea.ru", "my.mts-link.ru")
CONTROL_SITES = ("ya.ru", "www.google.com")
VPN_MARKERS = (
    "amnezia",
    "wireguard",
    "wintun",
    "tap-windows",
    "openvpn",
    "outline",
    "vpn",
    "hiddify",
    "v2ray",
    "nekoray",
    "clash",
    "sing-box",
    "warp",
    "tun2socks",
)

# For "these addresses go past the VPN" lists. MIREA's sign-in, Pulse, the СДО and
# the MTS Link lecture rooms; the subnets cover every address these names have.
BYPASS = [
    ("91.215.40.0/22", ""),
    ("193.41.140.0/22", ""),
    ("37.130.193.0/24", ""),
    ("mirea.ru", "91.215.42.222"),
    ("www.mirea.ru", "91.215.42.222"),
    ("pulse.mirea.ru", "91.215.42.222"),
    ("sso.mirea.ru", "193.41.141.68"),
    ("login.mirea.ru", "193.41.140.67"),
    ("online-edu.mirea.ru", "193.41.141.77"),
    ("attendance.mirea.ru", "91.215.42.222"),
    ("attendance-app.mirea.ru", "193.41.141.68"),
    ("lk.mirea.ru", "193.41.141.68"),
    ("api.mirea.ru", "193.41.140.44"),
    ("app.mirea.ru", "193.41.140.67"),
    ("mts-link.ru", "37.130.193.75"),
    ("my.mts-link.ru", "37.130.193.14"),
    ("events.mts-link.ru", "37.130.193.7"),
    ("webinar.ru", "37.130.193.75"),
    ("events.webinar.ru", "37.130.193.14"),
]

VPN_HELP = (
    "Сайты МИРЭА часто не открываются через VPN. Пусть VPN пропускает их мимо себя:\n"
    "• AmneziaVPN: «Настройки → Раздельное туннелирование сайтов» → включить → режим "
    "«Адреса из списка НЕ должны открываться через VPN» → «Импорт» → выбрать файл "
    "mirea-bypass-amnezia.json из папки, которую открывает приложение.\n"
    "• Другой VPN: в его настройках «исключения», «раздельное туннелирование» или "
    "«split tunneling» добавьте адреса из файла mirea-bypass.txt.\n"
    "• Или просто выключайте VPN на время пар."
)


def _reachable(host: str, timeout: float) -> bool:
    import httpx

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            client.head(f"https://{host}/")
        return True  # any answer, even an error page, means the site is reachable
    except Exception:  # noqa: BLE001 - every failure means the same here
        return False


def vpn_adapters() -> list[str]:
    """Names of network adapters that are up and look like a VPN (Windows)."""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "Get-NetAdapter | Where-Object Status -eq 'Up' | "
                    "ForEach-Object { $_.InterfaceDescription + ' | ' + $_.Name }"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if any(marker in line.casefold() for marker in VPN_MARKERS)
    ]


def network_report(timeout: float = 5) -> dict:
    """Which of MIREA's sites answer, whether the internet does, whether a VPN is on."""
    hosts = [*MIREA_SITES, *CONTROL_SITES]
    with ThreadPoolExecutor(max_workers=len(hosts) + 1) as pool:
        adapters = pool.submit(vpn_adapters)
        answers = dict(zip(hosts, pool.map(lambda h: _reachable(h, timeout), hosts)))
        vpn = adapters.result()
    mirea = {host: answers[host] for host in MIREA_SITES}
    internet = any(answers[host] for host in CONTROL_SITES)
    if all(mirea.values()):
        verdict = "ok"
    elif not internet:
        verdict = "offline"
    elif vpn:
        verdict = "vpn_suspected"
    else:
        verdict = "mirea_unreachable"
    return {"verdict": verdict, "internet": internet, "mirea": mirea, "vpn_adapters": vpn}


def write_bypass_files(folder: Path) -> Path:
    """The bypass list as AmneziaVPN imports it and as plain text; returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    entries = [{"hostname": host, "ip": ip} for host, ip in BYPASS]
    (folder / "mirea-bypass-amnezia.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "mirea-bypass.txt").write_text(
        "\n".join(host for host, _ip in BYPASS) + "\n", encoding="utf-8"
    )
    return folder


LOG_LINE = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ (?P<level>WARNING|ERROR|CRITICAL) "
    r"\S+ (?P<source>\S+) (?P<event>[A-Za-z0-9_.]+)"
)


def recent_problems(log_path: Path, limit: int = 30) -> list[dict]:
    """The latest warnings and errors: time, level, module and event name — no values."""
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 512 * 1024))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for line in reversed(lines):
        match = LOG_LINE.match(line)
        if match:
            found.append(match.groupdict())
            if len(found) >= limit:
                break
    return found


def scrub(text: str, *, private: list[str] = ()) -> str:
    """A report safe to publish: no e-mails, links with tokens, codes, paths or names."""
    text = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "<почта>", text)
    text = re.sub(r"https?://([^/\s?#]+)[^\s]*", r"https://\1/…", text)
    text = re.sub(r"(?i)([A-Z]:\\Users\\)[^\\\s]+", r"\1<пользователь>", text)
    text = re.sub(r"/(home|Users)/[^/\s]+", r"/\1/<пользователь>", text)
    text = re.sub(r"\b\d{6,}\b", "<число>", text)
    text = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "<ключ>", text)
    for value in sorted({v.strip() for v in private if v and len(v.strip()) >= 3}, key=len)[::-1]:
        text = re.sub(re.escape(value), "<личное>", text, flags=re.IGNORECASE)
    return text[:6000]
