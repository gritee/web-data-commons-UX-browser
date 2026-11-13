#!/usr/bin/env python3
"""
Web Data Commons schema.org subset browser - local server.

  python3 wdc_browser.py            # starts http://127.0.0.1:8765 and opens it
  python3 wdc_browser.py --port 9000 --no-browser

Serves wdc_browser.html (the single-page app) and a small JSON API that the
page cannot do by itself from a browser sandbox:
  - fetch + parse the per-year "schema_org_subsets.html" tables
  - fetch + parse the file listings on data.dws.informatik.uni-mannheim.de
  - download a .gz N-Quads file, gunzip it, and convert the entities of a
    schema.org class to CSV or JSON at a path on this machine

No third-party packages needed (Python 3.8+).
"""
import argparse, csv, json, os, platform, re, secrets, ssl, subprocess, sys
import threading, time, urllib.error, urllib.parse, urllib.request, webbrowser, zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "wdc_browser.html")
TOKEN = secrets.token_urlsafe(24)

SITE = "https://webdatacommons.org/structureddata/"
SUBSETS_PAGE = SITE + "{year}/stats/schema_org_subsets.html"
ALLOWED_HOSTS = {"webdatacommons.org", "www.webdatacommons.org",
                 "data.dws.informatik.uni-mannheim.de"}
# Releases known to have a subsets page; the site index is also scanned for new ones.
KNOWN_YEARS = ["2013-11", "2014-12", "2015-11", "2016-10", "2017-12", "2018-12",
               "2019-12", "2020-12", "2021-12", "2022-12", "2023-12", "2024-12"]
UA = "wdc-subset-browser/1.0 (python-urllib)"


# ----------------------------------------------------------------- remote fetch

def _ssl_context():
    ctx = ssl.create_default_context()
    try:  # python.org builds on macOS may lack system roots
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx

SSL_CTX = _ssl_context()


def check_remote(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise ValueError("URL is not on webdatacommons.org / data.dws.informatik.uni-mannheim.de")
    return url


def remote_open(url, timeout=60):
    check_remote(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    check_remote(resp.geturl())  # refuse redirects that leave the allowed hosts
    return resp


def remote_text(url):
    with remote_open(url) as r:
        return r.read().decode("utf-8", errors="replace")


# ------------------------------------------------------- subsets page parsing

class _Tables(HTMLParser):
    """Collect table rows as lists of cells: {th, text, links[]} + nearest <h2>."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell, self.link = [], None, None, None
        self.heading, self._in_h2 = "", False

    def _end_link(self):
        if self.link is not None and self.cell is not None:
            self.link["text"] = " ".join("".join(self.link["text"]).split())
            self.cell["links"].append(self.link)
        self.link = None

    def _end_cell(self):
        self._end_link()
        if self.cell is not None and self.row is not None:
            self.cell["text"] = "".join(self.cell["text"])
            self.row["cells"].append(self.cell)
        self.cell = None

    def _end_row(self):
        self._end_cell()
        if self.row and self.row["cells"]:
            self.rows.append(self.row)
        self.row = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h2":
            self._in_h2, self.heading = True, ""
        elif tag == "tr":
            self._end_row()
            self.row = {"heading": self.heading, "cells": []}
        elif tag in ("td", "th") and self.row is not None:
            self._end_cell()
            self.cell = {"th": tag == "th", "text": [], "links": []}
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append("\n")
        elif tag == "a" and self.cell is not None:
            self._end_link()
            self.link = {"href": a.get("href") or "", "onclick": a.get("onclick") or "", "text": []}

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_h2 = False
        elif tag == "a":
            self._end_link()
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag in ("tr", "table"):
            self._end_row()
        elif tag == "br" and self.cell is not None:   # the pages use </br>
            self.cell["text"].append("\n")

    def handle_data(self, data):
        if self._in_h2:
            self.heading += data
        if self.cell is not None:
            self.cell["text"].append(data)
        if self.link is not None:
            self.link["text"].append(data)


SCHEMA_URL = re.compile(r'^https?://schema\.org/([^/#?\s]+)/?$')
BASE_DIR = re.compile(r'base_dir\s*=\s*["\']([^"\']+)["\']')
ONCLICK = re.compile(r"base_dir\s*\+\s*'([^']*)'")
RELATED = re.compile(r'(https?://\S+)\s*\(([\d,]+)\)')
SIZE = re.compile(r'([\d.,]+)\s*([KMGT]?B)', re.I)
UNIT = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def link_kind(url):
    leaf = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    if url.endswith("/") or "." not in leaf:
        return "dir"
    if leaf.endswith(".gz") and not leaf.endswith((".csv.gz", ".txt.gz")):
        return "quads"
    return "file"


def parse_subsets(html, page_url):
    p = _Tables()
    p.feed(html)
    p.close()
    m = BASE_DIR.search(html)
    base_dir = m.group(1) if m else ""
    out = []
    for row in p.rows:
        cells = row["cells"]
        if len(cells) < 4 or not cells[0]["th"] or not cells[0]["links"]:
            continue
        sm = SCHEMA_URL.match(cells[0]["links"][0]["href"].strip())
        if not sm:
            continue
        stats = {}
        for line in cells[1]["text"].split("\n"):
            k, _, v = line.partition(":")
            digits = re.sub(r"[^\d]", "", v)
            if digits:
                stats[re.sub(r"^Number of\s+", "", k.strip())] = int(digits)
        related = [{"cls": c, "count": int(n.replace(",", ""))}
                   for c, n in RELATED.findall(cells[2]["text"])]
        size_text = " ".join(cells[3]["text"].split())
        zm = SIZE.search(size_text)
        size_bytes = 0
        if zm:
            try:
                size_bytes = float(zm.group(1).replace(",", "")) * UNIT[zm.group(2).upper()]
            except ValueError:
                pass
        fm = re.search(r"\((\d+)", size_text)
        links = []
        for c in cells[4:]:
            for l in c["links"]:
                oc = ONCLICK.search(l["onclick"])
                if l["href"].strip():
                    url = urllib.parse.urljoin(page_url, l["href"].strip())
                elif oc and base_dir:
                    url = base_dir + oc.group(1)
                else:
                    continue
                try:
                    check_remote(url)
                except ValueError:
                    continue
                kind = link_kind(url)
                if kind == "dir" and not url.endswith("/"):
                    url += "/"
                links.append({"label": l["text"].strip("() ") or "link", "url": url, "kind": kind})
        h = row["heading"]
        variant = "Microdata" if "Microdata" in h else "JSON-LD" if "JSON-LD" in h else ""
        out.append({"name": sm.group(1), "variant": variant, "stats": stats, "related": related,
                    "size": zm.group(0) if zm else size_text, "size_bytes": size_bytes,
                    "files": int(fm.group(1)) if fm else None, "links": links})
    return out


_cache = {"years": None, "subsets": {}}
_cache_lock = threading.Lock()


def get_subsets(year, refresh=False):
    if not re.fullmatch(r"\d{4}-\d{2}", year):
        raise ValueError("bad year")
    with _cache_lock:
        if not refresh and year in _cache["subsets"]:
            return _cache["subsets"][year]
    url = SUBSETS_PAGE.format(year=year)
    rows = parse_subsets(remote_text(url), url)
    with _cache_lock:
        _cache["subsets"][year] = rows
    return rows


def get_years(refresh=False):
    with _cache_lock:
        if not refresh and _cache["years"] is not None:
            return _cache["years"]
    cands = set(KNOWN_YEARS)
    try:
        cands.update(re.findall(r"structureddata/(\d{4}-\d{2})/", remote_text(SITE)))
    except Exception:
        pass

    def probe(y):
        try:
            return y, len(get_subsets(y, refresh)), None
        except Exception as e:
            return y, 0, str(e)

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(probe, sorted(cands)))
    years = [{"year": y, "subsets": n, "page": SUBSETS_PAGE.format(year=y)}
             for y, n, _ in sorted(res, reverse=True) if n]
    if not years:
        raise RuntimeError("Could not load any release: " + "; ".join(sorted({e for _, _, e in res if e})))
    with _cache_lock:
        _cache["years"] = years
    return years


LISTING = re.compile(r'<a href="([^"]+)">[^<]*</a>([^\n<]*)', re.I)


def list_dir(url):
    if not url.endswith("/"):
        url += "/"
    html = remote_text(url)
    entries = []
    for href, tail in LISTING.findall(html):
        if href.startswith(("?", "/", "../", "http")):
            continue
        parts = tail.split()
        full = urllib.parse.urljoin(url, href)
        entries.append({"name": urllib.parse.unquote(href), "url": full, "kind": link_kind(full),
                        "date": " ".join(parts[:-1]), "size": parts[-1] if parts else ""})
    return {"url": url, "entries": entries}


# ---------------------------------------------------------- quads -> entities

QUAD = re.compile(
    r'^(<[^>]*>|_:\S+)\s+'                                   # subject
    r'<([^>]*)>\s+'                                          # predicate
    r'(<[^>]*>|_:\S+|"(?:[^"\\]|\\.)*"(?:@[\w-]+|\^\^<[^>]*>)?)\s+'  # object
    r'<([^>]*)>\s*\.\s*$'                                    # graph = page URL
)
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SCHEMA = re.compile(r'^https?://schema\.org/')
LIT = re.compile(r'^"((?:[^"\\]|\\.)*)"')
BIG_U = re.compile(r'\\U([0-9A-Fa-f]{8})')
KEEP_LIST = {"recipeIngredient", "recipeInstructions", "itemListElement", "image"}


def unescape(s):
    if "\\" not in s:
        return s
    try:
        s2 = BIG_U.sub(lambda m: chr(int(m.group(1), 16)), s)
        return json.loads('"' + s2 + '"')
    except Exception:
        return s.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t').replace('\\\\', '\\')


def short(iri):
    return SCHEMA.sub('', iri)


def parse_obj(o):
    if o.startswith('"'):
        m = LIT.match(o)
        return ('lit', unescape(m.group(1)) if m else o)
    if o.startswith('_:'):
        return ('bnode', o)
    return ('iri', o[1:-1])


def build(node_id, triples, depth=0, seen=frozenset()):
    """Rebuild one node (and its blank-node children) as a dict."""
    if depth > 6 or node_id in seen:
        return None
    seen = seen | {node_id}
    out = {}
    for pred, (kind, val) in triples.get(node_id, []):
        key = "@type" if pred == RDF_TYPE else short(pred)
        if kind == 'bnode':
            val = build(val, triples, depth + 1, seen) if val in triples else None
            if val is None:
                continue
        elif kind == 'iri' and key == "@type":
            val = short(val)
        out.setdefault(key, []).append(val)
    for k, v in list(out.items()):
        if len(v) == 1 and k not in KEEP_LIST:
            out[k] = v[0]
    if not node_id.startswith('_:'):
        out["@id"] = node_id[1:-1]
    return out


def text_of(x):
    """Best-effort plain text from a value that may be str/dict/list."""
    if x is None:
        return ""
    if isinstance(x, list):
        return " | ".join(t for t in (text_of(i) for i in x) if t)
    if isinstance(x, dict):
        for k in ("text", "name", "url", "contentUrl", "@id"):
            if k in x:
                return text_of(x[k])
        if "itemListElement" in x:
            return text_of(x["itemListElement"])
        return ""
    return str(x).strip()


def flatten(item):
    """One flat CSV row. Nested nodes become 'prop' (best text) + 'prop.sub' columns."""
    row = {}
    text = lambda x: " ".join(text_of(x).split())   # Microdata text is full of layout whitespace
    for k, v in item.items():
        vals = v if isinstance(v, list) else [v]
        t = text(vals)
        if t:
            row[k] = t
        sub = {}
        for d in vals:
            if isinstance(d, dict):
                for k2, v2 in d.items():
                    if k2 == "@type":
                        continue
                    t2 = text(v2)
                    if t2:
                        sub.setdefault(k2, []).append(t2)
        for k2, ts in sub.items():
            row[k + "." + k2] = " | ".join(ts)
    return row


def page_items(triples, type_name):
    """Entities of the wanted class on one page ('' = every top-level typed node)."""
    if type_name:
        ids = [s for s, ps in triples.items()
               if any(p == RDF_TYPE and k == 'iri' and short(v) == type_name for p, (k, v) in ps)]
    else:
        # top-level = typed and not nested inside another *typed* node (Microdata pages
        # point at every item from an untyped page node via microdata#item)
        typed = {s for s, ps in triples.items() if any(p == RDF_TYPE for p, _ in ps)}
        referenced = {v for s in typed for p, (k, v) in triples[s] if k == 'bnode'}
        ids = [s for s in triples if s in typed and s not in referenced]
    seen = set()
    for i in ids:
        r = build(i, triples)
        if not r:
            continue
        key = text_of(r.get("name")).lower() or r.get("@id")
        if key:   # the same entity often appears twice (Microdata + JSON-LD)
            if key in seen:
                yield None
                continue
            seen.add(key)
        yield r


def convert_lines(lines, type_name, limit, emit, stats):
    """Stream quads grouped by page; call emit(page_url, item). Stops at limit."""
    def flush(page, triples):
        stats["pages"] += 1
        for r in page_items(triples, type_name):
            if r is None:
                stats["dupes"] += 1
                continue
            emit(page, r)
            stats["items"] += 1
            if limit and stats["items"] >= limit:
                return True
        return False

    cur, triples = None, {}
    for line in lines:
        stats["lines"] += 1
        m = QUAD.match(line)
        if not m:
            if line.strip():
                stats["bad_lines"] += 1
            continue
        s, p, o, g = m.groups()
        if g != cur:
            if cur is not None and flush(cur, triples):
                return
            cur, triples = g, {}
        triples.setdefault(s, []).append((p, parse_obj(o)))
    if cur is not None:
        flush(cur, triples)


# ------------------------------------------------------------- byte pipeline

class Cancelled(Exception):
    pass


def iter_gunzip(chunks):
    d, members = zlib.decompressobj(31), 0
    for chunk in chunks:
        while chunk:
            try:
                out = d.decompress(chunk)
            except zlib.error:
                if members:      # trailing padding after the last gzip member
                    return
                raise
            if out:
                yield out
            if d.eof:
                members += 1
                chunk, d = d.unused_data, zlib.decompressobj(31)
            else:
                chunk = b""


def iter_lines(chunks):
    tail = b""
    for c in chunks:
        parts = (tail + c).split(b"\n")
        tail = parts.pop()
        for p in parts:
            yield p.decode("utf-8", "replace")
    if tail:
        yield tail.decode("utf-8", "replace")


def tee(chunks, fh):
    for c in chunks:
        fh.write(c)
        yield c


def downloads_dir():
    home = os.path.expanduser("~")
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
                return winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
        except Exception:
            pass
    elif sys.platform != "darwin":
        try:
            d = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=3).stdout.strip()
            if d and d != home:
                return d
        except Exception:
            pass
    return os.path.join(home, "Downloads")


# ----------------------------------------------------------------------- jobs

JOBS, JOBS_LOCK = {}, threading.Lock()
ONE_AT_A_TIME = threading.Lock()   # the data server answers parallel downloads with HTTP 429


class Job:
    def __init__(self, spec):
        self.id = secrets.token_hex(6)
        self.spec = spec
        self.cancel = threading.Event()
        self.state = "queued"
        self.phase = "Starting"
        self.error = None
        self.started = time.time()
        self.ended = None
        self.outputs = []
        self.columns = 0
        self.stats = {"lines": 0, "bad_lines": 0, "pages": 0, "items": 0, "dupes": 0,
                      "bytes_in": 0, "bytes_total": 0}

    def public(self):
        return {"id": self.id, "state": self.state, "phase": self.phase, "error": self.error,
                "elapsed": round((self.ended or time.time()) - self.started, 1),
                "outputs": self.outputs, "columns": self.columns, "stats": self.stats,
                "url": self.spec["url"], "format": self.spec["format"], "limit": self.spec["limit"],
                "type": self.spec["type"], "out": self.spec["out"]}

    # -- source of compressed bytes: local copy if present, else the network
    def _source(self, gz_path):
        st, spec = self.stats, self.spec
        for attempt in range(4):
            try:
                resp = remote_open(spec["url"], timeout=120)
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 3:
                    raise
                self.phase = "Server is rate limiting - retrying"
                if self.cancel.wait(5 * (attempt + 1)):
                    raise Cancelled()
        total = int(resp.headers.get("Content-Length") or 0)
        st["bytes_total"] = total
        local = os.path.exists(gz_path) and total and os.path.getsize(gz_path) == total
        if local:
            resp.close()
            fh, self.reused_gz = open(gz_path, "rb"), True
        else:
            fh, self.reused_gz = resp, False

        def gen():
            try:
                while True:
                    if self.cancel.is_set():
                        raise Cancelled()
                    c = fh.read(1 << 18)
                    if not c:
                        return
                    st["bytes_in"] += len(c)
                    yield c
            finally:
                fh.close()
        return gen()

    def run(self):
        self.phase = "Queued"
        while not ONE_AT_A_TIME.acquire(timeout=0.3):
            if self.cancel.is_set():
                self.phase, self.state, self.ended = "Cancelled", "cancelled", time.time()
                return
        self.started = time.time()
        try:
            self._run()
        finally:
            ONE_AT_A_TIME.release()

    def _run(self):
        spec, st = self.spec, self.stats
        out = spec["out"]
        stem = os.path.splitext(out)[0]
        gz_path, nq_path = stem + ".nq.gz", stem + ".nq"
        tmp = [out + ".part"]
        opened = []
        try:
            self.state = "running"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            raw = self._source(gz_path)
            self.phase = "Reading local .gz copy" if self.reused_gz else "Downloading + extracting + converting"
            keep_gz = spec["keep_gz"] and not self.reused_gz
            if keep_gz:
                tmp.append(gz_path + ".part")
                f = open(gz_path + ".part", "wb"); opened.append(f)
                raw = tee(raw, f)
            plain = iter_gunzip(raw)
            if spec["keep_nq"]:
                tmp.append(nq_path + ".part")
                f = open(nq_path + ".part", "wb"); opened.append(f)
                plain = tee(plain, f)

            fmt, counts = spec["format"], Counter()
            with open(out + ".part", "w", encoding="utf-8", newline="") as of:
                if fmt == "json":
                    of.write("[")

                    def emit(page, r):
                        of.write((",\n" if st["items"] else "\n") +
                                 json.dumps({"page_url": page, **r}, ensure_ascii=False))
                else:  # csv: columns are only known at the end -> spool items, then write
                    def emit(page, r):
                        counts.update(flatten(r).keys())
                        of.write(json.dumps([page, r], ensure_ascii=False) + "\n")
                convert_lines(iter_lines(plain), spec["type"], spec["limit"], emit, st)
                if fmt == "json":
                    of.write("\n]\n")

            # item limit reached early: finish any file the user asked to keep
            if spec["keep_nq"]:
                self.phase = "Finishing extraction of full .nq"
                for _ in plain:
                    pass
            elif keep_gz:
                self.phase = "Finishing download of full .gz"
                for _ in raw:
                    pass
            raw.close()
            for f in opened:
                f.close()

            if fmt == "csv":
                self.phase = "Writing CSV"
                spool = out + ".items.part"
                tmp.append(spool)
                os.replace(out + ".part", spool)
                lead = [c for c in ("@id", "@type", "name") if c in counts]
                rest = [c for c, _ in counts.most_common() if c not in lead][:spec["max_columns"]]
                fields = ["page_url"] + lead + rest + (["json"] if spec["json_column"] else [])
                self.columns = len(fields)
                with open(spool, encoding="utf-8") as sf, \
                     open(out + ".part", "w", encoding="utf-8", newline="") as cf:
                    w = csv.DictWriter(cf, fieldnames=fields, extrasaction="ignore")
                    w.writeheader()
                    for line in sf:
                        if self.cancel.is_set():
                            raise Cancelled()
                        page, r = json.loads(line)
                        row = flatten(r)
                        row["page_url"] = page
                        if spec["json_column"]:
                            row["json"] = json.dumps(r, ensure_ascii=False)
                        w.writerow(row)
                os.remove(spool)

            os.replace(out + ".part", out)
            self.outputs.append(out)
            if keep_gz:
                os.replace(gz_path + ".part", gz_path); self.outputs.append(gz_path)
            if spec["keep_nq"]:
                os.replace(nq_path + ".part", nq_path); self.outputs.append(nq_path)
            self.phase, self.state = "Done", "done"
        except Cancelled:
            self.phase, self.state = "Cancelled", "cancelled"
        except Exception as e:
            self.phase, self.state = "Failed", "error"
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.ended = time.time()
            for f in opened:
                try: f.close()
                except Exception: pass
            if self.state != "done":
                for t in tmp:
                    try: os.remove(t)
                    except OSError: pass


def start_job(body):
    url = check_remote(str(body.get("url", "")))
    if link_kind(url) != "quads":
        raise ValueError("Not a .gz quads file")
    fmt = body.get("format")
    if fmt not in ("csv", "json"):
        raise ValueError("format must be csv or json")
    limit = int(body.get("limit") or 0)
    if limit < 0:
        raise ValueError("limit must be >= 0")
    out = os.path.expanduser(str(body.get("out") or "").strip())
    default_name = os.path.basename(str(body.get("default_name") or "wdc_export")) + "." + fmt
    if not out:
        out = os.path.join(downloads_dir(), default_name)
    if not os.path.isabs(out):
        out = os.path.join(downloads_dir(), out)
    if os.path.isdir(out) or out.endswith(("/", os.sep)):
        out = os.path.join(out, default_name)
    out = os.path.abspath(out)
    if os.path.exists(out) and not body.get("overwrite"):
        return None, out
    spec = {"url": url, "format": fmt, "limit": limit, "out": out,
            "type": str(body.get("type") or "").strip(),
            "keep_gz": bool(body.get("keep_gz")), "keep_nq": bool(body.get("keep_nq")),
            "json_column": bool(body.get("json_column", True)),
            "max_columns": max(1, min(int(body.get("max_columns") or 250), 2000))}
    job = Job(spec)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return job, out


def reveal(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


# ----------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "wdc-browser"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, api):
        """Local-only: reject other Host headers (DNS rebinding) and API calls without the page token."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "forbidden host"})
            return False
        if api and not secrets.compare_digest(self.headers.get("X-WDC-Token") or "", TOKEN):
            self._send(403, {"error": "bad token - reload the page"})
            return False
        return True

    def _route(self, method):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        path = u.path
        if not self._guard(path.startswith("/api/")):
            return
        try:
            if method == "GET" and path in ("/", "/index.html"):
                with open(HTML_FILE, encoding="utf-8") as f:
                    html = f.read().replace("__WDC_TOKEN__", TOKEN)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if method == "GET" and path == "/api/years":
                return self._send(200, {"years": get_years(q.get("refresh") == "1")})
            if method == "GET" and path == "/api/subsets":
                y = q.get("year", "")
                return self._send(200, {"year": y, "page": SUBSETS_PAGE.format(year=y),
                                        "subsets": get_subsets(y, q.get("refresh") == "1")})
            if method == "GET" and path == "/api/files":
                return self._send(200, list_dir(q.get("url", "")))
            if method == "GET" and path == "/api/defaults":
                return self._send(200, {"downloads": downloads_dir(), "sep": os.sep})
            if method == "GET" and path == "/api/jobs":
                with JOBS_LOCK:
                    return self._send(200, {"jobs": [j.public() for j in JOBS.values()]})
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if path == "/api/convert":
                    job, out = start_job(body)
                    if job is None:
                        return self._send(409, {"error": "exists", "out": out})
                    return self._send(200, job.public())
                with JOBS_LOCK:
                    job = JOBS.get(str(body.get("id")))
                if path == "/api/cancel" and job:
                    job.cancel.set()
                    return self._send(200, {"ok": True})
                if path == "/api/reveal" and job and body.get("path") in job.outputs:
                    reveal(body["path"])
                    return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except urllib.error.HTTPError as e:
            self._send(502, {"error": f"Remote server returned HTTP {e.code} for {e.url}"})
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as e:
            self._send(502, {"error": f"Could not reach the Web Data Commons server: {e}"})
        except (ValueError, KeyError) as e:
            self._send(400, {"error": str(e)})
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"No free port in {a.port}-{a.port + 19}")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"WDC subset browser running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
