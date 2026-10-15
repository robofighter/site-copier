"""Разбор WARC/WACZ: извлечение HTTP-ответов для дальнейшей сборки статики."""

import gzip
import io
import re
import zipfile
from urllib.parse import urlparse


def _parse_warc_bytes(data: bytes):
    """Итерирует записи WARC: возвращает (headers_dict, payload_bytes)."""
    pos = 0
    while data.startswith(b"WARC/", pos):
        head_end = data.find(b"\r\n\r\n", pos)
        if head_end < 0:
            break
        head = data[pos:head_end].decode("utf-8", "replace")
        m = re.search(r"Content-Length:\s*(\d+)", head)
        if not m:
            break
        length = int(m.group(1))
        body_start = head_end + 4
        body = data[body_start:body_start + length]
        meta = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        yield meta, body
        pos = body_start + length
        while data[pos:pos + 2] == b"\r\n":
            pos += 2


def _split_http(payload: bytes):
    """Отделяет HTTP-заголовки от тела. Заголовки в WARC — в нижнем регистре."""
    idx = payload.find(b"\r\n\r\n")
    if idx < 0:
        return {}, payload
    head = payload[:idx].decode("utf-8", "replace")
    headers = {}
    for line in head.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers, payload[idx + 4:]


def _maybe_gunzip(headers, body: bytes) -> bytes:
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            return gzip.decompress(body)
        except Exception:
            return body
    return body


def read_wacz(path: str):
    """Читает WACZ-файл, возвращает список записей.

    Каждая запись: dict(url, status, mime, body: bytes)
    """
    records = []
    with zipfile.ZipFile(path) as z:
        warcs = [n for n in z.namelist() if n.startswith("archive/") and n.endswith(".warc.gz")]
        for name in warcs:
            raw = z.read(name)
            try:
                data = gzip.decompress(raw)
            except Exception:
                continue
            for meta, body in _parse_warc_bytes(data):
                if meta.get("warc-type") != "response":
                    continue
                url = meta.get("warc-target-uri")
                if not url or not url.startswith("http"):
                    continue
                headers, payload = _split_http(body)
                payload = _maybe_gunzip(headers, payload)
                status = 200
                m = re.match(r"HTTP/[\d.]+\s+(\d+)", body[:40].decode("utf-8", "replace"))
                if m:
                    status = int(m.group(1))
                records.append({
                    "url": url,
                    "status": status,
                    "mime": headers.get("content-type", "").split(";")[0].strip(),
                    "body": payload,
                })
    return records


def read_warc_dir(directory: str):
    """Читает каталог с .warc.gz (как отдаёт browsertrix в crawls/collections/<c>/archive/)."""
    import glob
    import os

    records = []
    for path in sorted(glob.glob(os.path.join(directory, "*.warc.gz"))):
        with open(path, "rb") as fh:
            try:
                data = gzip.decompress(fh.read())
            except Exception:
                continue
        for meta, body in _parse_warc_bytes(data):
            if meta.get("warc-type") != "response":
                continue
            url = meta.get("warc-target-uri")
            if not url or not url.startswith("http"):
                continue
            headers, payload = _split_http(body)
            payload = _maybe_gunzip(headers, payload)
            status = 200
            m = re.match(r"HTTP/[\d.]+\s+(\d+)", body[:40].decode("utf-8", "replace"))
            if m:
                status = int(m.group(1))
            records.append({
                "url": url,
                "status": status,
                "mime": headers.get("content-type", "").split(";")[0].strip(),
                "body": payload,
            })
    return records


def url_to_relpath(url: str) -> str:
    """URL -> относительный путь файла (без учёта query)."""
    u = urlparse(url)
    path = u.path
    if path.endswith("/") or path == "":
        path += "index.html"
    if not path.startswith("/"):
        path = "/" + path
    return u.netloc + path
