#!/usr/bin/env python3
"""Such-Start für den research-agent (nur stdlib).

Eine Suche, mehrere Suchmaschinen (Tavily, Exa, Serper, DonSeTch, Hound, Jina): fällt eine aus (kein Key, 401, 429,
Kontingent leer), geht es automatisch zur nächsten. Mit --fan fragt es alle
verfügbaren gleichzeitig und führt die Treffer zusammen (mehr Ergebnisse).
Spart den geteilten WebSearch-Topf der Harness.

Beispiele:
  search.py "Förderprogramm Schülerfirma" "student startup grant Germany"
  search.py "hackathon berlin" --since 60d --lang de --n 15
  search.py "LRS App Schule" --site bildungsserver.de --site berlin.de
  search.py "accessibility hackathon" --fan --seen research/slug/seen.txt
  search.py --status

Optionen:
  mehrere Queries   jede läuft einzeln (z.B. DE + EN + Synonym), Treffer werden
                    nach URL zusammengeführt
  --since 30d|2026-09-01   nur neuere Seiten (Tavily, Serper, Exa-API echt;
                    Exa-MCP und Jina nur als Hinweis im Suchtext)
  --site / --exclude       Domains ein- bzw. ausschliessen (wiederholbar)
  --lang de|en|...  Sprache/Land der Ergebnisse, wo die Suchmaschine es kann
  --news            Nachrichten statt allgemeiner Web-Suche (Tavily, Serper)
  --engine NAME     nur diese: tavily, exa, exa-api, serper, donsetch, hound, jina
  --fan             alle verfügbaren parallel statt Fallback-Kette
  --seen DATEI      gemeinsame Fundliste einer Agent-Welle: markiert [neu] oder
                    [bekannt] und hängt neue URLs an (dateigesperrt, parallel sicher)
  --json            maschinenlesbar statt Text

Keys: Umgebung, dann <skill>/.env. Keys werden nie ausgegeben.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_FILES = [
    os.path.join(SKILL_DIR, ".env"),
]
KEY_NAMES = ["TAVILY_API_KEY", "EXA_API_KEY", "SERPER_API_KEY", "JINA_API_KEY"]
# Reihenfolge der Fallback-Kette: erst die mit Filtern und Kontingent, dann keyless.
CHAIN = ["tavily", "exa-api", "serper", "exa", "donsetch", "hound", "jina"]
LANG_COUNTRY = {"de": "germany", "en": "united states", "fr": "france",
                "es": "spain", "it": "italy", "nl": "netherlands"}
TIMEOUT = 40


class EngineDown(Exception):
    """Suchmaschine nicht nutzbar (kein Key, Limit, Fehler) -> nächste."""


def load_keys():
    keys = {k: os.environ[k] for k in KEY_NAMES if os.environ.get(k)}
    for path in KEY_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*)", line)
                    if not m or m.group(1) not in KEY_NAMES or m.group(1) in keys:
                        continue
                    val = re.split(r"(?:^|\s)#", m.group(2))[0].strip().strip("'\"")
                    if val:
                        keys[m.group(1)] = val
        except OSError:
            pass
    return keys


def since_date(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([dwmy])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"d": 1, "w": 7, "m": 31, "y": 366}[unit]
        return date.today() - timedelta(days=days)
    return date.fromisoformat(s)


def http_json(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "research-agent/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise EngineDown(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise EngineDown(type(e).__name__)


def hit(title, url, snippet, published, engine):
    return {"title": (title or "").strip()[:120], "url": url,
            "snippet": re.sub(r"\s+", " ", snippet or "").strip()[:400],
            "published": (published or "")[:10], "engine": engine}


# ---- Suchmaschinen: (query, opts, keys) -> Liste von Treffern ----

def s_tavily(q, o, keys):
    key = keys.get("TAVILY_API_KEY") or _missing("TAVILY_API_KEY")
    body = {"query": q, "max_results": min(o.n, 20), "search_depth": "advanced",
            "topic": "news" if o.news else "general"}
    if o.site:
        body["include_domains"] = o.site
    if o.exclude:
        body["exclude_domains"] = o.exclude
    if o.since_d:
        body["start_date"] = o.since_d.isoformat()
    if o.lang in LANG_COUNTRY and not o.news:
        body["country"] = LANG_COUNTRY[o.lang]
    r = http_json("https://api.tavily.com/search", body,
                  {"Authorization": f"Bearer {key}"})
    return [hit(x.get("title"), x.get("url"), x.get("content"),
                x.get("published_date"), "tavily") for x in r.get("results", [])]


def s_exa_api(q, o, keys):
    key = keys.get("EXA_API_KEY") or _missing("EXA_API_KEY")
    body = {"query": q, "numResults": o.n, "type": "auto",
            "contents": {"highlights": True}}
    if o.site:
        body["includeDomains"] = o.site
    if o.exclude:
        body["excludeDomains"] = o.exclude
    if o.since_d:
        body["startPublishedDate"] = o.since_d.isoformat() + "T00:00:00.000Z"
    r = http_json("https://api.exa.ai/search", body, {"x-api-key": key})
    return [hit(x.get("title"), x.get("url"), " ".join(x.get("highlights") or []),
                x.get("publishedDate"), "exa-api") for x in r.get("results", [])]


def s_serper(q, o, keys):
    key = keys.get("SERPER_API_KEY") or _missing("SERPER_API_KEY")
    qq = q + "".join(f" site:{d}" if i == 0 else f" OR site:{d}"
                     for i, d in enumerate(o.site))
    qq += "".join(f" -site:{d}" for d in o.exclude)
    body = {"q": qq, "num": min(o.n, 100)}
    if o.lang:
        body["hl"] = o.lang
        body["gl"] = {"en": "us"}.get(o.lang, o.lang)
    if o.since_d:
        body["tbs"] = f"cdr:1,cd_min:{o.since_d.strftime('%m/%d/%Y')}"
    url = "https://google.serper.dev/" + ("news" if o.news else "search")
    r = http_json(url, body, {"X-API-KEY": key})
    rows = r.get("news" if o.news else "organic", [])
    return [hit(x.get("title"), x.get("link"), x.get("snippet"), x.get("date"),
                "serper") for x in rows]


def s_exa_mcp(q, o, keys):
    """Keyless Exa über mcporter. Kann keine echten Filter, darum als Text."""
    objective = "Relevanteste Primärquellen zuerst, keine SEO-Listen."
    if o.site:
        objective += " Nur Seiten von: " + ", ".join(o.site) + "."
    if o.exclude:
        objective += " Keine Seiten von: " + ", ".join(o.exclude) + "."
    if o.since_d:
        objective += f" Nur Inhalte ab {o.since_d.isoformat()}."
    if o.lang:
        objective += f" Sprache bevorzugt: {o.lang}."
    args = json.dumps({"query": q, "objective": objective, "numResults": o.n})
    try:
        p = subprocess.run(["mcporter", "call", "exa.web_search_exa", "--args", args],
                           cwd=os.path.expanduser("~"), capture_output=True,
                           text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise EngineDown(type(e).__name__)
    if p.returncode != 0 or "Title:" not in p.stdout:
        raise EngineDown((p.stderr or p.stdout).strip()[:120] or "leer")
    hits = []
    for block in p.stdout.split("\n---\n"):
        f = dict(re.findall(r"^(Title|URL|Published):\s*(.*)$", block, re.M))
        if not f.get("URL"):
            continue
        snip = block.split("Highlights:", 1)[-1] if "Highlights:" in block else ""
        pub = f.get("Published", "")
        hits.append(hit(f.get("Title"), f["URL"], snip,
                        "" if pub == "N/A" else pub, "exa"))
    return hits


HOUND_ENGINES = ["duckduckgo", "brave", "mojeek", "yahoo", "startpage", "google",
                 "qwant", "bing"]  # ohne yandex (Auflage aus dem Security-Vetting)


def s_hound(q, o, keys):
    """Keyless Metasuche über den lokalen Hound-MCP (mcporter, stdio)."""
    opts = {"max_results": min(o.n, 50), "engines": HOUND_ENGINES}
    if o.site:
        opts["site"] = o.site[0]
    if o.exclude:
        opts["exclude_sites"] = o.exclude
    if o.lang:
        opts["language"] = o.lang
    if o.since_d:
        days = (date.today() - o.since_d).days
        opts["freshness"] = ("day" if days <= 1 else "week" if days <= 7
                             else "month" if days <= 31 else "year")
    args = json.dumps({"query": q, "options": opts})
    try:
        p = subprocess.run(["mcporter", "call", "hound.mcp_smart_search", "--args", args,
                            "--output", "json"], cwd=os.path.expanduser("~"),
                           capture_output=True, text=True, timeout=120)
        r = json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise EngineDown(type(e).__name__)
    if r.get("error"):
        raise EngineDown(str(r["error"])[:120])
    return [hit(x.get("title"), x.get("url"), x.get("snippet"), x.get("date"),
                "hound") for x in r.get("results", [])]


DONSETCH_BIN = os.environ.get(
    "DONSETCH_BIN", os.path.expanduser("~/.local/share/donsetch/4.4.1/donsetch"))


def s_donsetch(q, o, keys):
    """Keyless Metasuche über DonSeTch (gepinnt 4.4.1, Binary direkt).
    Kann keine Domain-/Zeitfilter, darum stehen site:/Jahr im Suchtext und
    host_ok filtert danach. Ein einziger `search --json`-Aufruf liefert Titel,
    URL und Snippet zusammen; zwei getrennte MCP-Aufrufe (Text + JSON) waren
    unterschiedlich sortiert und haben Titel falschen URLs zugeordnet."""
    if not os.path.exists(DONSETCH_BIN):
        raise EngineDown("nicht installiert")
    qq = q + "".join(f" site:{d}" for d in o.site[:1])
    cmd = [DONSETCH_BIN, "search", "--json", "-q", "--max-results", str(min(o.n, 12)),
           "--intent", "news" if o.news else "auto", qq]
    env = dict(os.environ, DONSETCH_NO_UPDATE_CHECK="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        results = json.loads(p.stdout)["meta"]["results"]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError) as e:
        raise EngineDown(type(e).__name__)
    return [hit(r.get("title", ""), r["url"], r.get("snippet", ""), "", "donsetch")
            for r in results if r.get("url")]


def s_jina(q, o, keys):
    key = keys.get("JINA_API_KEY") or _missing("JINA_API_KEY")
    qq = q + "".join(f" site:{d}" for d in o.site[:1])
    headers = {"Authorization": f"Bearer {key}", "X-Respond-With": "no-content"}
    if len(o.site) > 1:
        headers.pop("X-Respond-With")
    r = http_json("https://s.jina.ai/?q=" + urllib.parse.quote(qq), None, headers)
    return [hit(x.get("title"), x.get("url"), x.get("description"),
                x.get("date"), "jina") for x in (r.get("data") or [])][:o.n]


def _missing(name):
    raise EngineDown(f"kein {name}")


ENGINES = {"tavily": s_tavily, "exa-api": s_exa_api, "serper": s_serper,
           "exa": s_exa_mcp, "donsetch": s_donsetch, "hound": s_hound,
           "jina": s_jina}


# ---- Ablauf ----

def norm_url(u):
    p = urllib.parse.urlsplit(u.strip())
    query = "&".join(kv for kv in p.query.split("&")
                     if kv and not kv.startswith(("utm_", "ref=", "fbclid", "gclid")))
    host = p.netloc.lower().removeprefix("www.")
    return urllib.parse.urlunsplit(("https", host, p.path.rstrip("/"), query, ""))


def run_query(q, o, keys, log):
    names = [o.engine] if o.engine else CHAIN
    if o.fan:
        def one(name):
            try:
                return ENGINES[name](q, o, keys)
            except EngineDown as e:
                log.append(f"{name}: {e}")
                return []
        with ThreadPoolExecutor(len(names)) as ex:
            return [h for part in ex.map(one, names) for h in part]
    for name in names:
        try:
            hits = ENGINES[name](q, o, keys)
            if hits:
                return hits
            log.append(f"{name}: 0 Treffer")
        except EngineDown as e:
            log.append(f"{name}: {e}")
    return []


def host_ok(h, o):
    """Domainfilter nachziehen, weil nicht jede Suchmaschine echt filtert."""
    host = urllib.parse.urlsplit(h["url"]).netloc.lower().removeprefix("www.")
    match = lambda d: host == d or host.endswith("." + d)
    if o.site and not any(match(d) for d in o.site):
        return False
    return not any(match(d) for d in o.exclude)


def merge(hits):
    out = {}
    for h in hits:
        k = norm_url(h["url"])
        if k in out:
            prev = out[k]
            if h["engine"] not in prev["engine"].split("+"):
                prev["engine"] += "+" + h["engine"]  # von mehreren gefunden = stärker
            prev["snippet"] = prev["snippet"] or h["snippet"]
            prev["published"] = prev["published"] or h["published"]
        else:
            out[k] = dict(h, key=k)
    return sorted(out.values(), key=lambda h: -h["engine"].count("+"))


def mark_seen(hits, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        known = {line.split("\t", 1)[0] for line in f if line.strip()}
        stamp = time.strftime("%Y-%m-%d %H:%M")
        for h in hits:
            h["new"] = h["key"] not in known
            if h["new"]:
                f.write(f"{h['key']}\t{h['title'][:80]}\t{stamp}\n")
                known.add(h["key"])
        f.flush()  # vor dem Entsperren schreiben, sonst liest der nächste Agent den alten Stand
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)


def status(keys):
    for name in CHAIN:
        need = {"tavily": "TAVILY_API_KEY", "exa-api": "EXA_API_KEY",
                "serper": "SERPER_API_KEY", "jina": "JINA_API_KEY"}.get(name)
        ok = "keyless (mcporter)" if not need else ("Key da" if need in keys else "kein Key")
        if name == "donsetch":
            ok = "keyless (binary)" if os.path.exists(DONSETCH_BIN) else "nicht installiert"
        print(f"{name:8} {ok}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queries", nargs="*")
    ap.add_argument("--since")
    ap.add_argument("--site", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--lang")
    ap.add_argument("--news", action="store_true")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--engine", choices=list(ENGINES))
    ap.add_argument("--fan", action="store_true")
    ap.add_argument("--seen")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--status", action="store_true")
    o = ap.parse_args()
    keys = load_keys()
    if o.status:
        return status(keys)
    if not o.queries:
        ap.error("mindestens eine Query")
    o.since_d = since_date(o.since)

    log, hits = [], []
    for q in o.queries:
        hits += run_query(q, o, keys, log)
    hits = merge([h for h in hits if h.get("url") and host_ok(h, o)])
    log = list(dict.fromkeys(log))
    if o.seen:
        mark_seen(hits, o.seen)

    if o.json:
        print(json.dumps({"hits": hits, "log": log}, ensure_ascii=False, indent=1))
    else:
        for i, h in enumerate(hits, 1):
            tag = "" if "new" not in h else ("[neu] " if h["new"] else "[bekannt] ")
            date_s = f" ({h['published']})" if h["published"] else ""
            print(f"{i}. {tag}{h['title']}{date_s} | {h['engine']}\n   {h['url']}")
            if h["snippet"]:
                print(f"   {h['snippet'][:220]}")
        if log:
            print("\n# ausgefallen: " + "; ".join(log), file=sys.stderr)
    if not hits:
        sys.exit(1)


if __name__ == "__main__":
    main()
