"""Сборка статического сайта из записей WACZ.

Логика:
  1. HTML-страницы раскладываются в файлы (/, /about -> about/index.html).
  2. Все прочие ресурсы — в assets/<host>/<path>.
  3. Абсолютные URL переписываются на относительные пути.
  4. Вычищаются артефакты Framer: бейдж «Made in Framer», панель редактора, аналитика.
  5. Недостающие ресурсы (шрифты и т.п.) догружаются из сети.
"""

import os
import re
import shutil
import threading
import time
import zipfile
from urllib.parse import urlparse, urljoin, unquote

from warcutil import url_to_relpath

# ---------------------------------------------------------------- утилиты

ASSET_RX = re.compile(
    r"https?://[^\s\"'`()<>\\]+?\.(?:woff2?|ttf|otf|eot|png|jpe?g|gif|svg|webp|avif|ico|css|mjs|js|json|mp4|webm|mp3|wav)"
    r"(?:\?[^\s\"'`()<>\\]*)?",
    re.IGNORECASE,
)

# Версионный суффикс в ИМЕНИ файла: "ArrowRight.js@0.0.57".
# Без чистки у файла «расширение» становится .57 и любой сервер отдаёт его как
# application/octet-stream — модуль не загружается (BadRequest/Invalid MIME).
# Срезаем ТОЛЬКО если перед "@" идёт точка с расширением — иначе "leaflet@1.9.4"
# (каталог unpkg) превратился бы в "leaflet" и путь сломался.
VER_SUFFIX_RX = re.compile(r"(\.[A-Za-z0-9]{2,8})@[\w.\-]*$")

# Тот же суффикс, но внутри шаблонной строки кода:
#   import(`${He}${_}.js@0.0.57`)  ->  import(`${He}${_}.js`)
# Файл локально сохранён без версии (см. VER_SUFFIX_RX), поэтому литерал
# в коде надо привести к тому же виду.
VER_TEMPLATE_RX = re.compile(r"\.js@[A-Za-z0-9][\w.\-]*(['\"`])")


def _is_page(rec, host):
    """HTML-документ внутри копируемого сайта."""
    if rec["mime"] not in ("text/html", "application/xhtml+xml"):
        return False
    return urlparse(rec["url"]).netloc == host


def _page_output_path(url: str, host: str) -> str:
    """URL страницы -> путь файла внутри сайта."""
    u = urlparse(url)
    path = unquote(u.path)
    if path in ("", "/"):
        return "index.html"
    path = path.strip("/")
    if "." in os.path.basename(path):
        return path
    return f"{path}/index.html"


def _rel(from_file: str, to_file: str) -> str:
    """Относительный путь от одного файла к другому (внутри корня сайта)."""
    rel = os.path.relpath(to_file, os.path.dirname(from_file) or ".")
    return rel.replace(os.sep, "/")


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _clean_asset_relpath(rel: str) -> str:
    """Убирает версионный суффикс из имени файла: 'ArrowRight.js@0.0.57' -> 'ArrowRight.js'."""
    head, _, name = rel.rpartition("/")
    name = VER_SUFFIX_RX.sub(r"\1", name)
    return (head + "/" + name) if head else name


# Шим для window.URL.
#
# Зачем: строковые URL внутри .js/.mjs мы переписываем на корне-абсолютные
# /assets/... — иначе (относительными) они разрешаются относительно ДОКУМЕНТА,
# а не файла модуля, и картинки 404-ят на внутренних страницах.
#
# Но рантайм Framer местами вызывает new URL(x) БЕЗ базы (например
# S_(e,t){...new URL(t.src)...}), а это для "/assets/..." бросает TypeError.
# Брошенное исключение во время рендера React приводит к тому, что гидратация
# НИКОГДА не завершается: в performance-разметке нет framer-hydration-render-end,
# Lenis не инициализируется, scroll-анимации не запускаются, низ страницы
# остаётся opacity:0 (пустое место). Наблюдалось на vertical.framer.media,
# oberon.framer.website, precept.framer.website.
#
# Шим достраивает базу до document.baseURI, когда база не передана.
URL_SHIM = """<script id="url-shim">
(function(){
  var N = window.URL;
  function U(url, base){
    if (base === undefined || base === null) return new N(url, document.baseURI);
    if (typeof base === 'string' && !/^[a-zA-Z][a-zA-Z0-9+.\\-]*:/.test(base)) {
      // База относительная ("../../modules/x.js") ИЛИ корне-абсолютная
      // ("/assets/x.js") — new URL() принимает только абсолютную, цепляем к документу.
      base = new N(base, document.baseURI).href;
    }
    return new N(url, base);
  }
  U.prototype = N.prototype;
  Object.getOwnPropertyNames(N).forEach(function(k){
    if (k === 'prototype' || k === 'length' || k === 'name') return;
    try { U[k] = N[k]; } catch(e){}
  });
  try { U.createObjectURL = N.createObjectURL.bind(N); } catch(e){}
  try { U.revokeObjectURL = N.revokeObjectURL.bind(N); } catch(e){}
  window.URL = U;
})();
</script>"""

# Скрывает служебные оверлеи Framer:
#   - бейдж «Made in Framer» (рантайм вставляет его в #__framer-badge-container)
#   - виджет покупки шаблона «Unlock from $129 / Secured By …»
#   - промо-плашка «Get Template / See what's inside»
# Селектор по data-framer-name работает на любом шаблоне Framer.
BADGE_CSS = """<style id="hide-framer-badge">
#__framer-badge-container,
[data-framer-name="Made in Framer"],
[data-framer-name="Template Buy Button"],
[data-framer-name="Payments Secured By"],
[data-framer-name="Get Template"],
[data-framer-name="Get Template Button"],
[data-framer-name="Template Badge"],
[data-framer-name="Remove Me (Remix Button)"],
[data-framer-name="Remove Me (Remix)"],
[data-framer-name="Remove Me"],
a[href*="lemonsqueezy.com/checkout"],
.framer-1y4qiiy-container {
  display:none!important;visibility:hidden!important;
  height:0!important;width:0!important;overflow:hidden!important;pointer-events:none!important;
}
</style>"""

# Рантайм Framer вставляет оверлеи с РАЗНЫМИ data-framer-name в зависимости от
# шаблона («Remove Me (Remix Button)», «Buy template», «Secured By …»), причём
# делает это уже ПОСЛЕ гидратации — CSS-селекторы по имени не покрывают все
# варианты. Поэтому дополнительно прописываем универсальный «сметщик»: он ловит
# вставленные узлы через MutationObserver и прячет всё, что похоже на промо-
# оверлей Framer. Защита от катастрофы: не трогаем элементы с большим текстом
# или большими размерами — иначе можно случайно скрыть половину страницы.
BADGE_JS = """<script id="hide-framer-badge-js">
(function(){
  var NAME_RX = /(remix|buy\\s*template|get\\s*template|see\\s*what'?s\\s*inside|made\\s*in\\s*framer|secured\\s*by|template\\s*buy|template\\s*badge|unlock\\s*from)/i;
  function hide(el){
    if (!el || !el.style) return;
    el.style.setProperty('display','none','important');
    el.style.setProperty('visibility','hidden','important');
    el.style.setProperty('height','0','important');
    el.style.setProperty('width','0','important');
    el.style.setProperty('overflow','hidden','important');
    el.style.setProperty('pointer-events','none','important');
  }
  function isOverlay(el){
    if (!el.getAttribute) return false;
    if (el.id === '__framer-badge-container') return true;
    var name = el.getAttribute('data-framer-name') || '';
    if (!name || !NAME_RX.test(name)) return false;
    // защита: не скрываем большую содержимующую часть страницы
    var txt = (el.textContent || '').trim();
    if (txt.length > 220) return false;
    var r = el.getBoundingClientRect();
    if (r.width > 420 || r.height > 420) return false;
    return true;
  }
  function sweep(root){
    if (!root || root.nodeType !== 1) return;
    if (isOverlay(root)) { hide(root); prune(root); }
    var all = root.querySelectorAll('[data-framer-name], #__framer-badge-container');
    for (var i=0;i<all.length;i++) if (isOverlay(all[i])) { hide(all[i]); prune(all[i]); }
  }
  // Спрятали промо — но у него мог остаться родитель-обёртка position:fixed
  // во весь экран (data-framer-name="Height:100dvh"). Она невидима, зато
  // перехватывает ВСЕ клики по сайту. Если в обёртке не осталось видимых
  // детей — прячем и её.
  function prune(el){
    for (var d = 0, p = el.parentElement; p && p !== document.body && d < 3; d++, p = p.parentElement) {
      var cs = getComputedStyle(p);
      if (cs.position !== 'fixed' && cs.position !== 'absolute') continue;
      var kids = p.children, anyVisible = false;
      for (var i = 0; i < kids.length; i++) {
        var kcs = getComputedStyle(kids[i]);
        if (kcs.display !== 'none' && kcs.visibility !== 'hidden' && kids[i].offsetParent !== null) { anyVisible = true; break; }
      }
      if (!anyVisible) hide(p);
      else break;
    }
  }
  function boot(){
    sweep(document.body);
    if (!window.MutationObserver) return;
    new MutationObserver(function(muts){
      for (var i=0;i<muts.length;i++){
        var ns = muts[i].addedNodes;
        for (var j=0;j<ns.length;j++) if (ns[j].nodeType === 1) sweep(ns[j]);
      }
    }).observe(document.documentElement, {childList:true, subtree:true});
  }
  if (document.body) boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
</script>"""

# Регистрация сервис-воркера: он отдаёт локальные копии вместо CDN Framer.
SW_REGISTER = """<script id="local-asset-sw">
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./sw.js', {scope: './', updateViaCache: 'none'}).catch(function(){});
}
</script>"""

# Сам сервис-воркер. Framer строит URL картинок и шрифтов в рантайме, поэтому
# статически их переписать нельзя — перехватываем запросы и подменяем на локальные.
SW_JS = """/* Автосгенерировано копировщиком сайтов.
   1) Перехватывает запросы к framerusercontent.com / fonts.gstatic.com /
      app.framerstatic.com / framer.com и отдаёт локальные копии из assets/.
   2) Если сайт размещён в подкаталоге, чинит корне-абсолютные /assets/...
      (их пишет рантайм-код Framer) на путь внутри области видимости.
   query отбрасываем: файлы сохранены без него, иначе ищем файл с "?" в имени. */
const HOSTS = ['framerusercontent.com', 'fonts.gstatic.com', 'app.framerstatic.com', 'framer.com'];
const SCOPE = self.registration.scope;
const SCOPE_PATH = new URL(SCOPE).pathname;

self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {
  const req = event.request;
  let u;
  try { u = new URL(req.url); } catch (e) { return; }

  // 1) внешние CDN Framer -> локальные копии
  if (HOSTS.includes(u.hostname)) {
    const local = new URL('assets/' + u.hostname + u.pathname, SCOPE).href;
    event.respondWith(
      fetch(local, { credentials: 'omit' })
        .then((r) => (r && r.ok ? r : fetch(req)))
        .catch(() => fetch(req))
    );
    return;
  }

  // 2) корне-абсолютные /assets/... при размещении в подкаталоге
  if (u.origin === self.location.origin && SCOPE_PATH !== '/' && u.pathname.indexOf('/assets/') === 0) {
    const local = new URL(u.pathname.slice(1) + u.search, SCOPE).href;
    if (local !== req.url) {
      event.respondWith(
        fetch(local, { credentials: 'omit' })
          .then((r) => (r && r.ok ? r : fetch(req)))
          .catch(() => fetch(req))
      );
    }
  }
});
"""


# ---------------------------------------------------------------- билдер

class SiteBuilder:
    def __init__(self, records, root_url, out_dir, log=print, extra_hide=None):
        self.records = records
        self.root_url = root_url
        self.host = urlparse(root_url).netloc
        self.out_dir = out_dir
        self.log = log
        # дополнительные CSS-селекторы, которые нужно скрыть на всех страницах
        self.extra_hide = [s.strip() for s in (extra_hide or []) if s.strip()]

        # url (без query) -> относительный путь файла
        self.file_map = {}
        self.pages = {}      # url -> путь страницы
        self.assets = {}     # url -> путь ресурса
        self._download_queue = {}
        self._cdn_hosts = None

    # --- шаг 1: распределить записи по файлам
    def plan(self):
        seed_base = _strip_query(self.root_url)

        for rec in self.records:
            url = rec["url"]
            if rec["status"] >= 400 or not rec["body"]:
                continue
            base = _strip_query(url)
            if base in self.file_map:
                continue

            if _is_page(rec, self.host):
                # стартовая страница всегда становится корневым index.html,
                # чтобы копия открывалась по адресу домена
                out = "index.html" if base == seed_base else _page_output_path(url, self.host)
                self.pages[url] = out
            else:
                out = "assets/" + _clean_asset_relpath(url_to_relpath(base))
                self.assets[base] = out
            self.file_map[base] = out

        self.log(f"страниц: {len(self.pages)}, ресурсов: {len(self.assets)}")

    # --- шаг 2: записать файлы
    def write_files(self):
        os.makedirs(self.out_dir, exist_ok=True)
        for rec in self.records:
            base = _strip_query(rec["url"])
            out = self.file_map.get(base)
            if not out:
                continue
            full = os.path.join(self.out_dir, out)
            if os.path.exists(full):
                continue
            os.makedirs(os.path.dirname(full) or self.out_dir, exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(rec["body"])
        self.log("файлы записаны")

    # --- шаг 3: переписать ссылки
    def rewrite(self):
        text_ext = (".html", ".htm", ".css", ".mjs", ".js", ".json", ".svg")
        for base, out in list(self.file_map.items()):
            if not out.lower().endswith(text_ext):
                continue
            full = os.path.join(self.out_dir, out)
            if not os.path.exists(full):
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue

            original = text
            is_js = out.lower().endswith((".js", ".mjs"))

            # чистка HTML — ДО переписывания URL: её regex'ы ищут абсолютные
            # адреса CDN (app.framerstatic.com/editorbar…), а префиксное
            # переписывание уже превратило бы их в /assets/… и они бы не совпали
            if out.lower().endswith((".html", ".htm")):
                text = self._clean_framer_html(text)

            text = self._rewrite_text(text, out, root_absolute=is_js)

            if text != original:
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(text)
            self._queue_dangling(text, out)
        self.log("ссылки переписаны")

    # --- шаг 3б: висячие ссылки на /assets/<host>/...
    # Префиксное переписывание («https://framerusercontent.com/» -> «/assets/framerusercontent.com/»)
    # выполняется ДО ASSET_RX, поэтому URL уничтожается и догрузка о нём не узнаёт.
    # Если файла такого пути локально нет — восстанавливаем абсолютный URL
    # и ставим его в очередь догрузки (иначе 404 в рантайме).
    DANGLING_RX = (
        r"/assets/%s/[^\s\"'`()<>\\]+?\.(?:woff2?|ttf|otf|eot|png|jpe?g|gif|svg|webp|avif|ico|css|mjs|js|json|mp4|webm|mp3|wav)"
        r"(?:\?[^\s\"'`()<>\\]*)?"
    )

    def _queue_dangling(self, text: str, from_file: str):
        for host in self._local_cdn_hosts():
            rx = re.compile(self.DANGLING_RX % re.escape(host))
            for m in rx.finditer(text):
                path = m.group(0).split("?", 1)[0]     # /assets/<host>/<rest>
                rest = path[len("/assets/"):]           # <host>/<rest>
                if os.path.exists(os.path.join(self.out_dir, path[1:])):
                    continue
                base = "https://" + rest                 # обратно в абсолютный URL
                if base not in self.file_map:
                    self._download_queue.setdefault(base, from_file)

    def _local_cdn_hosts(self):
        """Хосты CDN, для которых локально лежит каталог assets/<host>/."""
        if self._cdn_hosts is None:
            adir = os.path.join(self.out_dir, "assets")
            try:
                self._cdn_hosts = sorted(
                    d for d in os.listdir(adir)
                    if "." in d and os.path.isdir(os.path.join(adir, d))
                )
            except OSError:
                self._cdn_hosts = []
        return self._cdn_hosts

    def _rewrite_text(self, text: str, from_file: str, root_absolute=False) -> str:
        """Заменяет абсолютные URL на локальные пути там, где ресурс есть локально.

        root_absolute=True (для .js/.mjs): строковые URL внутри скриптов в рантайме
        попадают в атрибуты DOM (img.src и т.п.) и разрешаются относительно
        ДОКУМЕНТА, а не файла модуля. Поэтому относительный путь там неверен —
        используем корне-абсолютный /assets/... (и импорты, и картинки работают).
        """

        # 1) Захардкоженные ПРЕФИКСЫ CDN в коде. Framer-компоненты (например
        #    набор иконок Phosphor) собирают URL конкатенацией:
        #      He = `https://framer.com/m/phosphor-icons/`
        #      import(`${He}${name}.js@0.0.57`)
        #    В file_map такого URL нет (он не приходит отдельным запросом),
        #    поэтому ASSET_RX его не заменит — переписываем префикс целиком.
        for host in self._local_cdn_hosts():
            text = text.replace("https://%s/" % host, "/assets/%s/" % host)

        # 2) Версионный суффикс в шаблонной строке — файл локально без версии.
        if root_absolute:
            text = VER_TEMPLATE_RX.sub(r".js\1", text)

        def repl(match):
            url = match.group(0)
            base = _strip_query(url)
            target = self.file_map.get(base)
            if target:
                if root_absolute:
                    return "/" + target.replace(os.sep, "/")
                return _rel(from_file, target)
            # ресурс не захвачен — запомним для догрузки
            if base.startswith(("http://", "https://")):
                self._download_queue.setdefault(base, from_file)
            return url

        return ASSET_RX.sub(repl, text)

    # --- шаг 4: чистка Framer
    def _clean_framer_html(self, text: str) -> str:
        # скрипт, включающий панель редактора
        text = re.sub(
            r'<script>try\{if\(localStorage\.getItem\("__framer_force_showing_editorbar_since"\)\).*?</script>',
            "", text, flags=re.S)
        # тег панели редактора (если попал в разметку)
        text = re.sub(r'<script[^>]*src="(?:https://app\.framerstatic\.com|/assets/app\.framerstatic\.com)/editorbar[^"]*"[^>]*>\s*</script>', "", text)
        text = re.sub(r'<script[^>]*src="/bootstrap\.[^"]*"[^>]*>\s*</script>', "", text)
        # аналитика
        text = re.sub(r'<script[^>]*src="https://events\.framer\.com[^"]*"[^>]*>\s*</script>', "", text)
        # бейдж «Made in Framer» — прячем через CSS (на всех страницах, не только там,
        # где контейнер уже есть в разметке: на внутренних он создаётся рантаймом)
        if "hide-framer-badge" not in text:
            text = text.replace("</head>", self._badge_css() + "\n</head>", 1)
        # рантайм-сметщик оверлеев: имена data-framer-name у шаблонов разные,
        # а вставляются они уже после гидратации
        if "hide-framer-badge-js" not in text:
            text = text.replace("</body>", BADGE_JS + "\n</body>", 1)
        # шим window.URL: без него new URL("/assets/...") бросает исключение
        # и гидратация React не завершается (сайт «пустой» / без анимаций)
        if "url-shim" not in text:
            text = text.replace("</head>", URL_SHIM + "\n</head>", 1)
        # сервис-воркер: перехватывает запросы к framerusercontent.com и отдаёт локальные копии
        if "local-asset-sw" not in text and "</body>" in text:
            text = text.replace("</body>", SW_REGISTER + "\n</body>", 1)
        return text

    # --- шаг 5: догрузить недостающие ресурсы
    def fetch_missing(self, max_files=2000, workers=12, budget=300):
        """Догружает не захваченные краулером ресурсы (шрифты, картинки).

        Скачивание идёт параллельно, а подстановка путей — одним проходом по
        файлам. Раньше подстановка выполнялась внутри цикла загрузки (для каждого
        ресурса читались ВСЕ страницы), и на сайтах с сотнями ресурсов шаг
        «сборка» висел десятки минут.
        """
        if not self._download_queue:
            return
        import urllib.request
        from concurrent.futures import ThreadPoolExecutor, as_completed

        items = list(self._download_queue.items())[:max_files]
        self.log(f"догрузка {len(items)} ресурсов…")

        results = {}
        started = time.time()
        done = 0

        def fetch(item):
            url, _from = item
            base = _strip_query(url)
            rel = "assets/" + _clean_asset_relpath(url_to_relpath(base))
            full = os.path.join(self.out_dir, rel)
            if os.path.exists(full):
                return (base, rel)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = resp.read()
            except Exception:
                return None
            if not data:
                return None
            try:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as fh:
                    fh.write(data)
            except OSError:
                return None
            return (base, rel)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(fetch, it) for it in items]
            for fut in as_completed(futures):
                if time.time() - started > budget:
                    for f in futures:
                        f.cancel()
                    break
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res:
                    results[res[0]] = res[1]
                    done += 1
                    if done % 50 == 0:
                        self.log(f"  догружено {done}…")

        for base, rel in results.items():
            self.file_map[base] = rel
        self.log(f"догружено: {len(results)}")

        if results:
            self._substitute_downloaded(results)

    def _substitute_downloaded(self, mapping):
        """Один проход по текстовым файлам: заменить догруженные URL на локальные пути."""
        if not mapping:
            return
        # длинные URL первыми, чтобы короткий префикс не срезал более длинный
        keys = sorted(mapping.keys(), key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(k) for k in keys))
        text_ext = (".html", ".htm", ".css", ".mjs", ".js", ".json")

        for other_out in list(self.file_map.values()):
            if not other_out.lower().endswith(text_ext):
                continue
            full = os.path.join(self.out_dir, other_out)
            if not os.path.exists(full):
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue

            is_js = other_out.lower().endswith((".js", ".mjs"))

            if is_js:
                text = VER_TEMPLATE_RX.sub(r".js\1", text)

            def repl(m, _out=other_out, _js=is_js):
                rel = mapping.get(m.group(0))
                if not rel:
                    return m.group(0)
                return ("/" + rel.replace(os.sep, "/")) if _js else _rel(_out, rel)

            new = pattern.sub(repl, text)
            if new != text:
                try:
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write(new)
                except OSError:
                    pass

    # --- сборка целиком
    def build(self):
        self.plan()
        self.write_files()
        self.rewrite()
        self.fetch_missing()
        # после догрузки ссылки могли появиться заново — прогоняем перезапись ещё раз
        self.rewrite()
        self._write_service_worker()
        self.log("готово")

    def _write_service_worker(self):
        path = os.path.join(self.out_dir, "sw.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(SW_JS)

    def _badge_css(self) -> str:
        """CSS, скрывающий служебные оверлеи + пользовательские селекторы."""
        if not self.extra_hide:
            return BADGE_CSS
        extra = ",\n".join(self.extra_hide)
        block = (f"<style id=\"hide-framer-badge\">\n{extra} {{\n"
                 "  display:none!important;visibility:hidden!important;\n"
                 "  height:0!important;width:0!important;overflow:hidden!important;\n"
                 "  pointer-events:none!important;\n}\n</style>")
        # базовые селекторы оставляем, добавляем пользовательские
        base = BADGE_CSS.replace('id="hide-framer-badge"', 'id="hide-framer-badge-base"')
        return base + "\n" + block


def make_zip(src_dir: str, zip_path: str, exclude=("README.md",)):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(src_dir):
            for f in files:
                if f in exclude:
                    continue
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, src_dir))
    return zip_path


def convert(records, root_url, out_dir, log=print, extra_hide=None):
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    builder = SiteBuilder(records, root_url, out_dir, log=log, extra_hide=extra_hide)
    builder.build()
    return builder
