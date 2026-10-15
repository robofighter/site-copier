"""Веб-панель для копирования сайтов.

Запуск:  python3 server.py            (порт 8200)
         PORT=9000 python3 server.py
"""

import functools
import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import crawler
from convert import convert, make_zip
from warcutil import read_wacz

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE, "jobs")
STATIC_DIR = os.path.join(BASE, "static")
PORT = int(os.environ.get("PORT", "8200"))

os.makedirs(JOBS_DIR, exist_ok=True)

JOBS = {}
LOCK = threading.Lock()

# Предпросмотр: каждому заданию — свой мини-сервер на отдельном порту.
# Так корне-абсолютные пути /assets/... и service worker работают как при
# реальном размещении (в превью по подпути они бы ломались).
PREVIEW_SERVERS = {}          # job_id -> (port, httpd)
PREVIEW_LOCK = threading.Lock()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        # чтобы предпросмотр всегда показывал свежую сборку
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _ensure_preview(job_id):
    """Поднимает (один раз) статический сервер для сайта задания. Возвращает порт."""
    with PREVIEW_LOCK:
        existing = PREVIEW_SERVERS.get(job_id)
        if existing:
            return existing[0]
        site = os.path.join(_job_dir(job_id), "site")
        if not os.path.isdir(site):
            return None
        handler = functools.partial(_QuietHandler, directory=site)
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError:
            return None
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        PREVIEW_SERVERS[job_id] = (port, httpd)
        return port


# ------------------------------------------------------------------ задачи

def _job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def _save_job(job):
    """Сохраняет метаданные задания, чтобы список не терялся при перезапуске."""
    try:
        d = _job_dir(job["id"])
        os.makedirs(d, exist_ok=True)
        meta = {k: v for k, v in job.items() if k != "log"}
        with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _seed_url_from_crawl(job_id):
    """Достаёт URL сида из pages.jsonl (для заданий без job.json)."""
    pages = os.path.join(_job_dir(job_id), "crawl", "collections", "site",
                         "pages", "pages.jsonl")
    if not os.path.isfile(pages):
        return None
    try:
        with open(pages, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("seed") and rec.get("url"):
                    return rec["url"]
                if rec.get("url"):
                    return rec["url"]
    except OSError:
        return None
    return None


def _recover_job(job_id):
    """Собирает метаданные для старого каталога задания без job.json."""
    jd = _job_dir(job_id)
    site_dir = os.path.join(jd, "site")
    zip_path = os.path.join(jd, "site.zip")
    if not os.path.isdir(site_dir) and not os.path.isfile(zip_path):
        return None
    size = count = 0
    if os.path.isdir(site_dir):
        for root, _d, files in os.walk(site_dir):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
                count += 1
    zip_mb = 0.0
    if os.path.isfile(zip_path):
        try:
            zip_mb = round(os.path.getsize(zip_path) / 1e6, 2)
        except OSError:
            pass
    url = _seed_url_from_crawl(job_id) or "(URL не сохранён)"
    return {
        "id": job_id,
        "url": url,
        "scope": "host",
        "limit": 0,
        "workers": 2,
        "timeLimit": 900,
        "behaviors": "",
        "hideSelectors": [],
        "status": "done" if (count or zip_mb) else "error",
        "stage": "done" if (count or zip_mb) else "error",
        "log": ["[--] Задание восстановлено с диска (без job.json)."],
        "pages": 0,
        "stats": {
            "files": count,
            "sizeMB": round(size / 1e6, 2),
            "pages": 0,
            "assets": 0,
            "zipMB": zip_mb,
        } if (count or zip_mb) else None,
        "error": None,
        "created": os.path.getmtime(jd) if os.path.exists(jd) else time.time(),
    }


def _load_jobs():
    """Подхватывает завершённые задания с диска при старте."""
    if not os.path.isdir(JOBS_DIR):
        return
    for name in os.listdir(JOBS_DIR):
        if not os.path.isdir(os.path.join(JOBS_DIR, name)):
            continue
        meta_path = os.path.join(JOBS_DIR, name, "job.json")
        meta = None
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, json.JSONDecodeError):
                meta = None
        if meta is None:
            # старые каталоги заданий без job.json — восстанавливаем
            meta = _recover_job(name)
            if meta is None:
                continue
            _save_job(meta)
        # незавершённые после перезапуска помечаем как прерванные
        if meta.get("status") in ("running", "queued"):
            meta["status"] = "error"
            meta["error"] = "прервано перезапуском панели"
        meta.setdefault("log", [])
        JOBS[meta["id"]] = meta


def _log(job, msg):
    ts = time.strftime("%H:%M:%S")
    job["log"].append(f"[{ts}] {msg}")
    if len(job["log"]) > 400:
        del job["log"][:200]
    print(f"[{job['id']}] {msg}", flush=True)
    _save_job(job)


MAX_JOB_SECONDS = int(os.environ.get("MAX_JOB_SECONDS", "2400"))


def _watchdog():
    """Страховка: задание не должно висеть в статусе «выполняется» вечно."""
    while True:
        time.sleep(30)
        now = time.time()
        with LOCK:
            stuck = []
            for job in list(JOBS.values()):
                if job.get("status") != "running":
                    continue
                started = job.get("startedAt") or job.get("created") or now
                if now - started > MAX_JOB_SECONDS:
                    stuck.append(job)
            for job in stuck:
                job["status"] = "error"
                job["error"] = (f"Задание остановлено: превышен лимит времени "
                                f"({MAX_JOB_SECONDS // 60} мин).")
                _log(job, job["error"])


def _run_job(job_id):
    job = JOBS[job_id]
    jd = _job_dir(job_id)
    os.makedirs(jd, exist_ok=True)
    job["startedAt"] = time.time()

    try:
        url = job["url"]
        job["stage"] = "crawl"
        job["status"] = "running"

        def on_progress(pages, total):
            job["pages"] = pages

        wacz = crawler.crawl(
            url=url,
            work_dir=os.path.join(jd, "crawl"),
            collection="site",
            scope=job["scope"],
            limit=job["limit"],
            workers=job["workers"],
            time_limit=job["timeLimit"],
            behaviors=job["behaviors"],
            log=lambda m: _log(job, m),
            on_progress=on_progress,
        )

        job["stage"] = "convert"
        _log(job, "Разбор архива и сборка статического сайта…")
        records = read_wacz(wacz)
        _log(job, f"Записей в архиве: {len(records)}")

        out_dir = os.path.join(jd, "site")
        builder = convert(records, url, out_dir,
                          log=lambda m: _log(job, m),
                          extra_hide=job.get("hideSelectors"))

        job["stage"] = "zip"
        _log(job, "Упаковка ZIP…")
        zip_path = os.path.join(jd, "site.zip")
        make_zip(out_dir, zip_path)

        size = 0
        count = 0
        for root, _d, files in os.walk(out_dir):
            for f in files:
                size += os.path.getsize(os.path.join(root, f))
                count += 1

        job["stats"] = {
            "files": count,
            "sizeMB": round(size / 1e6, 2),
            "pages": len(builder.pages),
            "assets": len(builder.assets),
            "zipMB": round(os.path.getsize(zip_path) / 1e6, 2),
        }
        job["status"] = "done"
        job["stage"] = "done"
        _log(job, f"Готово: {count} файлов, {job['stats']['sizeMB']} MB")

    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        _log(job, "ОШИБКА: " + str(exc))
        traceback.print_exc()


# ------------------------------------------------------------------ сервер

class Handler(BaseHTTPRequestHandler):
    server_version = "SiteCopier/1.0"

    def log_message(self, fmt, *args):
        pass

    # --- helpers
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file(self, path, ctype):
        if not os.path.exists(path):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if path.endswith(".zip"):
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(path)}"')
        self.end_headers()
        self.wfile.write(data)

    # --- routes
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path

        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if path == "/app.js":
            return self._file(os.path.join(STATIC_DIR, "app.js"), "application/javascript; charset=utf-8")
        if path == "/style.css":
            return self._file(os.path.join(STATIC_DIR, "style.css"), "text/css; charset=utf-8")

        if path == "/api/status":
            return self._json({
                "docker": crawler.docker_available(),
                "image": crawler.IMAGE,
                "imageReady": crawler.image_present(),
            })

        if path == "/api/jobs":
            with LOCK:
                items = [self._summary(j) for j in JOBS.values()]
            items.sort(key=lambda x: x["created"], reverse=True)
            return self._json(items)

        m = re.match(r"^/api/jobs/([0-9a-f]{8,})$", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._json({"error": "not found"}, 404)
            return self._json(self._detail(job))

        m = re.match(r"^/api/jobs/([0-9a-f]{8,})/download$", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job or job["status"] != "done":
                return self._json({"error": "not ready"}, 404)
            return self._file(os.path.join(_job_dir(m.group(1)), "site.zip"), "application/zip")

        m = re.match(r"^/api/jobs/([0-9a-f]{8,})/open$", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job or job["status"] != "done":
                return self._json({"error": "not ready"}, 404)
            port = _ensure_preview(m.group(1))
            if not port:
                return self._json({"error": "нет собранного сайта"}, 404)
            return self._json({"url": f"http://127.0.0.1:{port}/"})

        m = re.match(r"^/api/jobs/([0-9a-f]{8,})/preview/(.*)$", path)
        if m:
            job_id, rest = m.group(1), m.group(2)
            base = os.path.join(_job_dir(job_id), "site")
            target = os.path.normpath(os.path.join(base, rest))
            if not target.startswith(os.path.abspath(base)):
                return self._send(403, b"forbidden", "text/plain")
            if os.path.isdir(target):
                target = os.path.join(target, "index.html")
            ctype = "application/octet-stream"
            ext = os.path.splitext(target)[1].lower()
            ctype = {
                ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".mjs": "application/javascript; charset=utf-8",
                ".json": "application/json", ".svg": "image/svg+xml",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".ico": "image/x-icon",
                ".woff2": "font/woff2", ".woff": "font/woff",
            }.get(ext, ctype)
            return self._file(target, ctype)

        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/jobs/rescan":
            with LOCK:
                running = {i: j for i, j in JOBS.items()
                           if j.get("status") in ("running", "queued")}
                JOBS.clear()
                _load_jobs()
                # выполняющиеся задания не трогаем — их состояние живёт в памяти
                for jid, job in running.items():
                    JOBS[jid] = job
                n = len(JOBS)
            return self._json({"ok": True, "jobs": n})

        if u.path != "/api/jobs":
            return self._json({"error": "not found"}, 404)

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid json"}, 400)

        url = (data.get("url") or "").strip()
        if not url:
            return self._json({"error": "Укажите URL"}, 400)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "url": url,
            "scope": data.get("scope") or "host",
            "limit": int(data.get("limit") or 0),
            "workers": max(1, min(8, int(data.get("workers") or 2))),
            "timeLimit": int(data.get("timeLimit") or 900),
            "behaviors": data.get("behaviors") or "autoplay,autofetch,autoscroll,siteSpecific",
            "hideSelectors": [
                s.strip() for s in (data.get("hideSelectors") or []) if s.strip()
            ],
            "status": "queued",
            "stage": "queued",
            "log": [],
            "pages": 0,
            "stats": None,
            "error": None,
            "created": time.time(),
        }
        with LOCK:
            JOBS[job_id] = job

        threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
        return self._json({"id": job_id, "status": "queued"}, 201)

    # --- serializers
    @staticmethod
    def _summary(job):
        return {
            "id": job["id"], "url": job["url"], "status": job["status"],
            "stage": job["stage"], "pages": job["pages"],
            "created": job["created"], "stats": job["stats"], "error": job["error"],
        }

    @staticmethod
    def _detail(job):
        d = Handler._summary(job)
        d["log"] = job["log"][-200:]
        return d


def main():
    _load_jobs()
    threading.Thread(target=_watchdog, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\n  Панель копирования сайтов: http://127.0.0.1:{PORT}")
    print(f"  Загружено заданий с диска: {len(JOBS)}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
