"""Запуск browsertrix-crawler в Docker и получение WACZ."""

import json
import os
import shutil
import subprocess
import threading
import time
import collections
from urllib.parse import urlparse

IMAGE = os.environ.get("BTRIX_IMAGE", "webrecorder/browsertrix-crawler:1.15.0")


class CrawlError(RuntimeError):
    pass


def docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def image_present() -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", IMAGE],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def ensure_image(log=print):
    if image_present():
        return
    log(f"Скачиваю образ {IMAGE} (может занять несколько минут)…")
    r = subprocess.run(["docker", "pull", IMAGE], capture_output=True, text=True)
    if r.returncode != 0:
        raise CrawlError("Не удалось скачать образ: " + (r.stderr or "")[-500:])


def _vol_name(work_dir: str) -> str:
    """Стабильное имя тома для задания: тот же work_dir -> тот же том."""
    import hashlib
    digest = hashlib.md5(os.path.abspath(work_dir).encode()).hexdigest()[:12]
    return "sitecopier-%s" % digest


def _docker(args, timeout=None):
    return subprocess.run(["docker"] + args, capture_output=True, text=True,
                          timeout=timeout)


def _copy_back(volume: str, work_dir: str, log=print) -> bool:
    """Копирует результат краулинга из тома Docker в каталог задания на хосте."""
    r = _docker(["run", "--rm",
                "-v", "%s:/src:ro" % volume,
                "-v", "%s:/dst" % work_dir,
                IMAGE, "sh", "-c", "cp -a /src/. /dst/ 2>/dev/null || true"],
               timeout=600)
    if r.returncode != 0:
        log("  ! не удалось скопировать результат из тома: " +
            (r.stderr or r.stdout or "")[-300:])
        return False
    return True


def crawl(url: str, work_dir: str, collection: str = "site",
          scope: str = "host", limit: int = 0, workers: int = 2,
          time_limit: int = 900, behaviors: str = "autoplay,autofetch,autoscroll,siteSpecific",
          extra_args=None, log=print, on_progress=None) -> str:
    """Запускает краулинг. Возвращает путь к WACZ."""
    if not docker_available():
        raise CrawlError("Docker недоступен. Запустите OrbStack/Docker Desktop.")

    ensure_image(log=log)
    os.makedirs(work_dir, exist_ok=True)

    # Краулинг пишет в ТОМ Docker, а не в каталог хоста.
    #
    # Зачем: Browsertrix на старте проверяет заполненность диска каталога сборки
    # (isDiskFull: Use% >= 99 — порог ЗАШИТ в код, см. /app/dist/util/storage.js).
    # Примонтированный каталог хоста — это том macOS /System/Volumes/Data, и если
    # он забит хотя бы до 99% (даже если свободно 15 ГБ!), краулер мгновенно
    # падает с «Out of disk space». Диск самой VM Docker при этом почти пустой
    # (том /dev/vdb1, ~22%), поэтому запись туда проходит проверку ensures.
    # Готовый WACZ/WARC копируется обратно на хост контейнером-хелпером.
    volume = _vol_name(work_dir)
    used_volume = False
    r = _docker(["volume", "create", volume])
    if r.returncode == 0:
        used_volume = True
    else:
        log("  ! том Docker не создан, краулю прямо в каталог задания")

    mount = "{}:/crawls/".format(volume if used_volume else work_dir)
    cmd = [
        "docker", "run", "--rm",
        "-v", mount,
        IMAGE, "crawl",
        "--url", url,
        "--collection", collection,
        "--scopeType", scope,
        "--workers", str(workers),
        "--behaviors", behaviors,
        "--generateWACZ",
        "--text",
        "--timeLimit", str(time_limit),
        "--pageLoadTimeout", "120",
        "--behaviorTimeout", "180",
        "--postLoadDelay", "3",
    ]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    if extra_args:
        cmd += list(extra_args)

    log("Запуск краулинга: " + url)
    log(" ".join(cmd))

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    pages = 0
    raw_tail = collections.deque(maxlen=60)
    for line in proc.stdout:
        line = line.rstrip("\n")
        raw_tail.append(line)
        if not line.startswith("{"):
            # не-JSON вывод (ошибки docker, предупреждения и т.п.) — не теряем его
            low = line.lower()
            if low.strip() and any(k in low for k in (
                    "error", "failed", "cannot", "denied", "unable",
                    "exception", "not found", "no such", "permission")):
                log("  ! " + line[:300])
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ctx = ev.get("context")
        if ctx == "crawlStatus":
            d = ev.get("details", {})
            pages = d.get("crawled", pages)
            if on_progress:
                on_progress(pages, d.get("total", 0))
            log(f"  просканировано страниц: {pages}")
        elif ctx == "pageStatus":
            pass
    proc.wait()

    # результат из тома -> каталог задания (с него потом читает конвертер)
    if used_volume:
        if _copy_back(volume, work_dir, log=log):
            _docker(["volume", "rm", "-f", volume])
            used_volume = False
        else:
            log("  ! результат остался в томе %s — скопируйте вручную" % volume)

    wacz = _find_wacz(work_dir, collection)
    if not wacz:
        tail = "\n".join(list(raw_tail)[-12:]) or "(нет вывода контейнера)"
        hint = ""
        if "disk space" in tail.lower():
            hint = ("\nПохоже, закончилось место на диске. Browsertrix отказывается "
                    "работать, если диск каталога сборки занят на 99% и больше (порог "
                    "зашит в код), даже когда свободно десятки гигабайт. Освободите "
                    "немного места (хватит ~3 ГБ) и запустите снова; при сильной "
                    "нехватке помогут: docker builder prune -f && docker image prune -f")
        raise CrawlError(
            f"WACZ не создан — краулинг не дал результата (код {proc.returncode}).\n"
            f"Последние строки контейнера:\n{tail}{hint}"
        )
    log(f"Архив готов: {os.path.basename(wacz)} ({os.path.getsize(wacz)/1e6:.1f} MB)")
    _cleanup(work_dir, collection, log=log)
    return wacz


def _find_wacz(work_dir: str, collection: str):
    direct = os.path.join(work_dir, "collections", collection, f"{collection}.wacz")
    if os.path.exists(direct):
        return direct
    for root, _dirs, files in os.walk(work_dir):
        for f in files:
            if f.endswith(".wacz"):
                return os.path.join(root, f)
    return None


def _cleanup(work_dir: str, collection: str, log=print):
    """Удаляет браузерный профиль (~100+ МБ на задание) в фоне.

    Выполняется в отдельном демон-потоке: удаление через virtiofs иногда
    «зависает», и если делать это синхронно, задание навсегда остаётся в статусе
    «сканирование», хотя WACZ уже готов.
    """
    profile = os.path.join(work_dir, "collections", collection, "profile")

    def _job():
        if not os.path.isdir(profile):
            return
        try:
            subprocess.run(["rm", "-rf", profile], timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("профиль браузера очищен")
        except Exception:
            pass

    threading.Thread(target=_job, daemon=True).start()
