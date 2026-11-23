#!/usr/bin/env python3
"""EL OBSERVADOR: genera index.html con titulares de País, Finanzas y Derecho.

Solo usa la biblioteca estándar de Python. La IA es opcional: si no existe
ANTHROPIC_API_KEY, el diario sale igual con los titulares originales.
Uso:  python generate.py          (edición real)
      python generate.py --demo   (datos de ejemplo, sin red, para ver el diseño)
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; ElObservador/1.0; uso personal)"

try:
    from zoneinfo import ZoneInfo
    AR = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:  # sin base de zonas horarias
    AR = timezone(timedelta(hours=-3))

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
SECCIONES = [("pais", "País"), ("finanzas", "Finanzas"), ("derecho", "Derecho")]
FALLAS = []  # fuentes que no respondieron, se muestran en el pie


# ------------------------------------------------------------------ red
def http_get(url, timeout=20, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    raise last


def get_json(url, **kw):
    return json.loads(http_get(url, **kw).decode("utf-8"))


# ------------------------------------------------------------------ noticias
def parse_rss(data):
    """Devuelve [{title, link, dt, src}] de un RSS 2.0 (tolera basura antes del XML)."""
    i = data.find(b"<")
    if i > 0:
        data = data[i:]
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Algunos feeds llegan cortados: recorto hasta el último </item> y cierro.
        j = data.rfind(b"</item>")
        if j < 0:
            raise
        root = ET.fromstring(data[: j + 7] + b"</channel></rss>")
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        dt = None
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                dt = None
        src = it.find("source")
        out.append({"title": html.unescape(title), "link": link, "dt": dt,
                    "src": (src.text or "").strip() if src is not None else ""})
    return out


def fetch_feed(feed):
    try:
        items = parse_rss(http_get(feed["url"]))
    except Exception as e:  # noqa: BLE001
        FALLAS.append(f'{feed["name"]} ({feed["section"]})')
        print(f'  [falla] {feed["name"]} {feed["section"]}: {e}', file=sys.stderr)
        return []
    res = []
    for it in items[:60]:
        link = it["link"]
        if not link.startswith("http") or not it["title"] or it["dt"] is None:
            continue
        if feed.get("path_include") and not any(p in link for p in feed["path_include"]):
            continue
        title, src = it["title"], feed["name"]
        if feed.get("use_item_source"):
            # Google News: "Titular - Medio"
            if it["src"]:
                src = it["src"]
                if title.endswith(" - " + src):
                    title = title[: -len(src) - 3]
            elif " - " in title:
                title, src = title.rsplit(" - ", 1)
        res.append({"title": title.strip(), "link": link, "dt": it["dt"], "src": src,
                    "section": feed["section"], "legal_only": feed.get("legal_only", False)})
    return res


GENERICAS = {"gobierno", "argentina", "argentino", "argentinos", "nacional", "millones", "según",
             "semana", "presidente", "ministro", "después", "tras", "hasta", "sobre", "desde",
             "cómo", "cuáles", "claves", "qué", "porque", "mientras", "nueva", "nuevo", "hoy"}


def norm_tokens(t):
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return {w for w in t.split() if len(w) > 3}


def same_story(a, b):
    """Dos titulares cuentan el mismo hecho si comparten buena parte de sus palabras
    o al menos dos palabras distintivas (nombres propios, temas)."""
    if not a or not b:
        return False
    if len(a & b) / min(len(a), len(b)) >= 0.6:
        return True
    fuertes = {w for w in (a & b) if len(w) > 5 and w not in GENERICAS}
    return len(fuertes) >= 2


def dedupe(items):
    kept, toks = [], []
    for it in items:
        tk = norm_tokens(it["title"])
        if not any(same_story(tk, o) for o in toks):
            kept.append(it)
            toks.append(tk)
    return kept


def collect(cfg, now):
    window = 72 if now.weekday() == 0 else 36  # los lunes, también el fin de semana
    since = now - timedelta(hours=window)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch_feed, cfg["feeds"]))
    by_sec = {k: [] for k, _ in SECCIONES}
    seen_links = set()
    for items in results:
        for it in items:
            if it["dt"].astimezone(AR) < since or it["link"] in seen_links:
                continue
            if it["legal_only"] and not is_legal(it["title"], cfg):
                continue
            seen_links.add(it["link"])
            by_sec[it["section"]].append(it)
    # Una misma noticia no puede estar en dos secciones: se asigna primero a
    # Derecho, luego a Finanzas y por último a País.
    toks = []
    for k in ("derecho", "finanzas", "pais"):
        by_sec[k].sort(key=lambda x: x["dt"], reverse=True)
        kept = []
        for it in by_sec[k]:
            tk = norm_tokens(it["title"])
            if any(same_story(tk, o) for o in toks):
                continue
            kept.append(it)
            toks.append(tk)
        by_sec[k] = kept[:25]
    return by_sec


def is_legal(title, cfg):
    """Coincidencia por palabra completa (evita que 'marca' active 'ARCA')."""
    pat = r"(?<!\w)(" + "|".join(re.escape(k.strip()) for k in cfg["legal_keywords"]) + r")"
    return re.search(pat, title.lower()) is not None


# ------------------------------------------------------------------ indicadores
def arnum(x, dec=0):
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def ind_dolares():
    out = []
    try:
        data = get_json("https://dolarapi.com/v1/dolares")
        names = {"oficial": "Dólar oficial", "blue": "Dólar blue", "bolsa": "Dólar MEP",
                 "contadoconliqui": "Contado c/ liqui"}
        for d in data:
            if d.get("casa") in names and d.get("venta"):
                f = (d.get("fechaActualizacion") or "")[:10]
                out.append({"label": names[d["casa"]], "value": "$ " + arnum(d["venta"], 0),
                            "chg": None, "asof": fmt_short(f)})
        order = list(names.values())
        out.sort(key=lambda x: order.index(x["label"]))
    except Exception as e:  # noqa: BLE001
        FALLAS.append("Dólar (dolarapi)")
        print("  [falla] dolares:", e, file=sys.stderr)
    return out


def ind_riesgo():
    try:
        serie = get_json("https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais")
        a, b = serie[-1], serie[-2]
        return [{"label": "Riesgo país", "value": arnum(a["valor"], 0) + " pb",
                 "chg": (a["valor"] / b["valor"] - 1) * 100, "asof": fmt_short(a["fecha"])}]
    except Exception as e:  # noqa: BLE001
        FALLAS.append("Riesgo país")
        print("  [falla] riesgo país:", e, file=sys.stderr)
        return []


def ind_reservas():
    try:
        hasta = date.today()
        desde = hasta - timedelta(days=12)
        d = get_json(f"https://api.bcra.gob.ar/estadisticas/v3.0/monetarias/1?desde={desde}&hasta={hasta}")
        det = d["results"][0]["detalle"] if "detalle" in d["results"][0] else d["results"]
        det = sorted(det, key=lambda x: x["fecha"])
        a, b = det[-1], det[-2]
        return [{"label": "Reservas BCRA", "value": "US$ " + arnum(a["valor"] / 1000, 1) + " mil M",
                 "chg": (a["valor"] / b["valor"] - 1) * 100, "asof": fmt_short(a["fecha"])}]
    except Exception as e:  # noqa: BLE001
        FALLAS.append("Reservas (BCRA)")
        print("  [falla] reservas:", e, file=sys.stderr)
        return []


def px_yahoo(sym):
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym)}?interval=1d&range=7d")
    r = d["chart"]["result"][0]
    pts = [(t, c) for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None]
    (t1, c1), (_, c0) = pts[-1], pts[-2]
    return c1, (c1 / c0 - 1) * 100, datetime.fromtimestamp(t1, timezone.utc).date().isoformat()


def px_stooq(sym):
    rows = http_get(f"https://stooq.com/q/d/l/?s={quote(sym)}&i=d").decode().strip().splitlines()
    a, b = rows[-1].split(","), rows[-2].split(",")
    return float(a[4]), (float(a[4]) / float(b[4]) - 1) * 100, a[0]


def px_coingecko(cid):
    d = get_json(f"https://api.coingecko.com/api/v3/simple/price?ids={cid}&vs_currencies=usd"
                 "&include_24hr_change=true&include_last_updated_at=true")[cid]
    return d["usd"], d["usd_24h_change"], datetime.fromtimestamp(d["last_updated_at"], timezone.utc).date().isoformat()


def ind_market(m):
    for prov, fn in (("coingecko", px_coingecko), ("yahoo", px_yahoo), ("stooq", px_stooq)):
        if prov not in m:
            continue
        try:
            v, chg, asof = fn(m[prov])
            return {"label": m["label"], "value": m.get("prefix", "") + arnum(v, m.get("decimals", 2)),
                    "chg": chg, "asof": fmt_short(asof)}
        except Exception as e:  # noqa: BLE001
            print(f'  [falla] {m["label"]} ({prov}): {e}', file=sys.stderr)
    FALLAS.append(m["label"])
    return None


def fmt_short(iso):
    try:
        y, mo, d = iso[:10].split("-")
        return f"{int(d)}/{int(mo)}"
    except Exception:  # noqa: BLE001
        return ""


def collect_indicators(cfg):
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_dol, f_rp, f_res = ex.submit(ind_dolares), ex.submit(ind_riesgo), ex.submit(ind_reservas)
        f_mk = [ex.submit(ind_market, m) for m in cfg["markets"]]
        ars = f_dol.result() + f_rp.result() + f_res.result()
        mk = [f.result() for f in f_mk]
    return ars, [m for m in mk if m]


# ------------------------------------------------------------------ IA (opcional)
def ai_edit(cfg, by_sec):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("  (sin ANTHROPIC_API_KEY: titulares originales)")
        return None
    flat, lines = {}, []
    n = 0
    for sec, label in SECCIONES:
        lines.append(f"## {label.upper()}")
        for it in by_sec[sec]:
            n += 1
            flat[n] = it
            lines.append(f'{n}|{it["src"]}|{it["title"]}')
    prompt = (
        "Sos el editor de un diario económico argentino para un abogado comercialista de Entre Ríos. "
        "Abajo hay titulares candidatos (id|medio|titular), agrupados por la sección de donde vienen; podés "
        "asignar cada uno a la sección que corresponda (PAÍS: política, gobierno, Congreso, decisiones de "
        "política económica, sociedad; FINANZAS: dólar, mercados, inflación, empresas, mundo financiero; "
        "DERECHO: ver abajo). Descartá deportes, espectáculos y policiales sin trascendencia. Elegí solo lo "
        "importante, sin repetir el mismo hecho. Para cada elegido escribí un título nuevo de 4 a 6 palabras, claro y sin "
        "sensacionalismo, solo con lo que dice el titular original. En DERECHO elegí únicamente novedades "
        "nacionales relevantes (leyes, decretos, fallos de la Corte o cámaras, normativa tributaria, societaria, "
        "concursal, laboral, procesal); si no hay nada realmente relevante, devolvé menos o una lista vacía. "
        f'Máximos: país {cfg["max_items"]["pais"]}, finanzas {cfg["max_items"]["finanzas"]}, '
        f'derecho {cfg["max_items"]["derecho"]}. '
        '"portada": ids de las 3 noticias más importantes del día entre todas las secciones. '
        '"jornada": 2 oraciones (máx. 45 palabras) que resuman el día usando SOLO información de los titulares, '
        "sin agregar datos. Respondé únicamente con JSON: "
        '{"jornada":"","portada":[1,2,3],"pais":[{"id":1,"t":""}],"finanzas":[],"derecho":[]}\n\n'
        + "\n".join(lines)
    )
    body = json.dumps({"model": cfg["ai_model"], "max_tokens": 1200,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        txt = resp["content"][0]["text"]
        data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        u = resp.get("usage", {})
        print(f'  IA: {u.get("input_tokens")} tokens de entrada, {u.get("output_tokens")} de salida')
    except Exception as e:  # noqa: BLE001
        print("  [falla IA, sigo sin IA]:", e, file=sys.stderr)
        return None

    used = set()

    def pick(lst, cap):
        out = []
        for x in lst:
            it = flat.get(x.get("id")) if isinstance(x, dict) else None
            if it and it["link"] not in used and len(out) < cap:
                used.add(it["link"])
                out.append(dict(it, short=str(x.get("t", "")).strip()[:80] or it["title"]))
        return out

    res = {sec: pick(data.get(sec, []), cfg["max_items"][sec]) for sec, _ in SECCIONES}
    res["portada"] = [flat[i] for i in data.get("portada", []) if isinstance(i, int) and i in flat][:3]
    res["jornada"] = str(data.get("jornada", "")).strip()
    return res


def plain_edit(cfg, by_sec):
    """Sin IA: lo más reciente de cada sección, alternando medios."""
    res = {}
    for sec, _ in SECCIONES:
        items = by_sec[sec]
        if sec == "derecho":
            items = [i for i in items if is_legal(i["title"], cfg)]
        res[sec] = [dict(i, short=i["title"]) for i in items[: min(cfg["max_items"][sec], 4 if sec == "derecho" else 99)]]
    res["portada"] = [x for sec in ("pais", "finanzas", "derecho") for x in res[sec][:1]][:3]
    res["jornada"] = ""
    return res


# ------------------------------------------------------------------ HTML
CSS = """
:root{--paper:#f7f3ea;--ink:#16140f;--grey:#6b665c;--rule:#cfc7b5;--wine:#6e1423;--up:#2f5d3a}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0 auto;max-width:1120px;padding:20px 22px 40px;color:var(--ink);
 font-family:"Helvetica Neue",Arial,sans-serif;font-size:15px;line-height:1.45}
a{color:inherit;text-decoration:none}
a:hover .h{color:var(--wine);text-decoration:underline;text-decoration-thickness:1px}
.serif{font-family:"Playfair Display",Georgia,"Times New Roman",serif}
.topline{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:11.5px;
 letter-spacing:.08em;text-transform:uppercase;color:var(--grey);padding-bottom:8px;border-bottom:1px solid var(--ink)}
.mast{text-align:center;padding:18px 0 12px;border-bottom:3px double var(--ink)}
.mast h1{margin:0;font-family:"Playfair Display",Georgia,serif;font-weight:900;
 font-size:clamp(40px,8.5vw,84px);letter-spacing:.04em;line-height:1}
.lema{margin:8px 0 0;font-family:Georgia,serif;font-style:italic;color:var(--wine);font-size:15px}
.ticker{display:grid;grid-template-columns:repeat(7,1fr);border-bottom:1px solid var(--ink)}
.cell{padding:9px 10px;border-right:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.cell .l{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--grey)}
.cell .v{font-family:Georgia,serif;font-size:17px;font-weight:700;margin-top:1px}
.cell .c{font-size:12px;font-weight:600}
.up{color:var(--up)}.down{color:var(--wine)}
.cell .d{font-size:10.5px;color:var(--grey);font-weight:400;margin-left:4px}
.lead{display:grid;grid-template-columns:1.25fr 1fr;gap:0;border-bottom:1px solid var(--ink)}
.persp{padding:22px 26px 22px 0;border-right:1px solid var(--rule)}
.kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--wine);font-weight:700;margin:0 0 8px}
.persp p.t{font-family:Georgia,serif;font-size:21px;line-height:1.38;margin:0}
.top3{padding:22px 0 22px 26px}
.top3 ol{margin:0;padding:0;list-style:none;counter-reset:n}
.top3 li{counter-increment:n;padding:0 0 12px 34px;position:relative;margin-bottom:12px;border-bottom:1px solid var(--rule)}
.top3 li:last-child{border:0;margin:0;padding-bottom:0}
.top3 li:before{content:counter(n);position:absolute;left:0;top:-4px;font-family:Georgia,serif;
 font-size:28px;font-weight:700;color:var(--wine)}
.h{font-family:Georgia,"Times New Roman",serif;font-weight:700;font-size:18px;line-height:1.25}
.meta{font-size:11.5px;color:var(--grey);margin-top:3px}
.orig{font-size:12.5px;color:var(--grey);margin-top:2px;line-height:1.35}
.cols{display:grid;grid-template-columns:repeat(3,1fr)}
.col{padding:20px 20px 10px;border-right:1px solid var(--rule)}
.col:first-child{padding-left:0}.col:last-child{border-right:0;padding-right:0}
.col h2{margin:0 0 14px;font-family:"Playfair Display",Georgia,serif;font-size:24px;font-weight:900;
 letter-spacing:.06em;text-transform:uppercase;border-bottom:2px solid var(--wine);padding-bottom:6px}
.item{padding:0 0 13px;margin-bottom:13px;border-bottom:1px solid var(--rule)}
.item:last-child{border-bottom:0}
.vacio{color:var(--grey);font-style:italic;font-family:Georgia,serif}
footer{margin-top:22px;padding-top:12px;border-top:1px solid var(--ink);font-size:11.5px;color:var(--grey)}
@media(max-width:860px){
 .ticker{grid-template-columns:repeat(2,1fr)}
 .lead{grid-template-columns:1fr}.persp{border-right:0;padding-right:0;border-bottom:1px solid var(--rule)}
 .top3{padding-left:0}.cols{grid-template-columns:1fr}
 .col{border-right:0;padding:18px 0 4px;border-bottom:1px solid var(--ink)}
 .persp p.t{font-size:19px}}
"""


def hhmm(dt, now):
    d = dt.astimezone(AR)
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.date() == (now - timedelta(days=1)).date():
        return "ayer " + d.strftime("%H:%M")
    return f"{DIAS[d.weekday()][:3]} {d.strftime('%H:%M')}"


def e(x):
    return html.escape(str(x), quote=True)


def story(it, now, big=False):
    short, title = it.get("short", it["title"]), it["title"]
    orig = ""
    if short.strip().lower() != title.strip().lower():
        orig = f'<div class="orig">{e(title[:140] + ("…" if len(title) > 140 else ""))}</div>'
    return (f'<a href="{e(it["link"])}" target="_blank" rel="noopener"><div class="h">{e(short)}</div>{orig}'
            f'<div class="meta">{e(it["src"])} · {e(hhmm(it["dt"], now))}</div></a>')


def render_indicators(ars, mk):
    cells = []
    for x in ars + mk:
        chg = ""
        if x["chg"] is not None:
            cls = "up" if x["chg"] >= 0 else "down"
            sign = "▲" if x["chg"] >= 0 else "▼"
            chg = f'<div class="c {cls}">{sign} {arnum(abs(x["chg"]), 2)}%<span class="d">{e(x["asof"])}</span></div>'
        else:
            chg = f'<div class="c"><span class="d" style="margin:0">{e(x["asof"])}</span></div>'
        cells.append(f'<div class="cell"><div class="l">{e(x["label"])}</div><div class="v">{e(x["value"])}</div>{chg}</div>')
    return '<section class="ticker">' + "".join(cells) + "</section>" if cells else ""


def render(cfg, ed, ars, mk, now, demo=False):
    start = date.fromisoformat(cfg["start_date"])
    nro = max(1, (now.date() - start).days + 1)
    fecha = f"{DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]} de {now.year}"
    persp = ""
    if ed.get("jornada"):
        persp = f'<div class="persp"><p class="kicker">La jornada en perspectiva</p><p class="t">{e(ed["jornada"])}</p></div>'
    top = ""
    if ed.get("portada"):
        lis = "".join(f"<li>{story(i, now)}</li>" for i in ed["portada"])
        top = f'<div class="top3"><p class="kicker">Lo más importante</p><ol>{lis}</ol></div>'
    lead = f'<section class="lead">{persp}{top}</section>' if (persp or top) else ""
    if persp and not top:
        lead = lead.replace('class="lead"', 'class="lead" style="grid-template-columns:1fr"')
    if top and not persp:
        lead = lead.replace('class="lead"', 'class="lead" style="grid-template-columns:1fr"').replace(
            'class="top3"', 'class="top3" style="padding-left:0"')
    cols = []
    for sec, label in SECCIONES:
        items = ed.get(sec, [])
        if items:
            body = "".join(f'<div class="item">{story(i, now)}</div>' for i in items)
        else:
            body = '<p class="vacio">Sin novedades relevantes hoy.</p>'
        cols.append(f'<section class="col"><h2>{label}</h2>{body}</section>')
    demo_note = '<p style="color:#6e1423;font-weight:700;text-align:center;margin:10px 0 0">DATOS DE PRUEBA · no son noticias reales</p>' if demo else ""
    fallas = f'<br>Fuentes sin respuesta hoy: {e(", ".join(sorted(set(FALLAS))))}.' if FALLAS and not demo else ""
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>EL OBSERVADOR · {e(fecha)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="topline"><span>Edición N.º {nro}</span><span>{e(fecha)}</span><span>Cierre {now.strftime('%H:%M')} h · Buenos Aires</span></div>
<header class="mast"><h1>EL OBSERVADOR</h1><p class="lema">El país, los mercados y el derecho bajo la lupa</p></header>
{demo_note}
{render_indicators(ars, mk)}
{lead}
<main class="cols">{''.join(cols)}</main>
<footer>Titulares de medios y fuentes oficiales; cada nota enlaza a su publicación original. Cotizaciones: DolarApi, ArgentinaDatos, BCRA y Yahoo Finance/CoinGecko, con la fecha de cada dato (los mercados del exterior muestran el último cierre disponible).
Contenido informativo: no constituye recomendación de inversión ni asesoramiento jurídico; verificar en la fuente primaria antes de usarlo.{fallas}</footer>
</body></html>"""


# ------------------------------------------------------------------ demo
def demo_data(now):
    def it(sec, short, title, src, h):
        return {"title": title, "short": short, "link": "https://example.com/", "src": src,
                "dt": now - timedelta(hours=h), "section": sec}
    ed = {
        "pais": [
            it("pais", "Caputo anunció baja de retenciones", "Caputo anunció una nueva baja de retenciones para el agro y la industria", "Infobae", 2),
            it("pais", "Reservas del BCRA se estancan", "Las reservas del BCRA se estancan mientras crece la brecha cambiaria", "Ámbito", 4),
            it("pais", "Diputados debate presupuesto 2027", "Diputados inicia el debate del Presupuesto 2027 con cambios en Ganancias", "La Nación", 5),
            it("pais", "Pobreza bajó en el tercer trimestre", "El INDEC informó que la pobreza bajó en el tercer trimestre", "Clarín", 7),
        ],
        "finanzas": [
            it("finanzas", "Riesgo país vuelve a subir", "El riesgo país vuelve a subir y los bonos globales caen", "Ámbito", 3),
            it("finanzas", "Wall Street cierra con subas", "Wall Street cierra con subas impulsadas por tecnológicas", "Reuters", 9),
            it("finanzas", "Bitcoin recupera los 85.000 dólares", "Bitcoin recupera los US$ 85.000 tras la decisión de la Fed", "Infobae", 6),
            it("finanzas", "Nvidia presenta balance récord", "Nvidia presenta un balance récord y supera las previsiones", "El Cronista", 10),
        ],
        "derecho": [
            it("derecho", "Corte: límites a embargos de cuentas", "La Corte Suprema fijó límites a los embargos sobre cuentas bancarias", "Diario Judicial", 8),
            it("derecho", "ARCA modifica régimen de retenciones", "ARCA modificó el régimen de retenciones del impuesto a las Ganancias", "Ámbito", 12),
        ],
    }
    ed["portada"] = [ed["pais"][0], ed["finanzas"][0], ed["derecho"][0]]
    ed["jornada"] = ("Baja de retenciones y tensión cambiaria dominan la agenda, con el riesgo país otra vez al alza. "
                     "En Derecho, la Corte puso límites a los embargos bancarios.")
    ars = [{"label": "Dólar oficial", "value": "$ 1.465", "chg": None, "asof": "23/11"},
           {"label": "Dólar blue", "value": "$ 1.490", "chg": None, "asof": "23/11"},
           {"label": "Dólar MEP", "value": "$ 1.512", "chg": None, "asof": "23/11"},
           {"label": "Riesgo país", "value": "1.070 pb", "chg": 1.9, "asof": "20/11"},
           {"label": "Reservas BCRA", "value": "US$ 41,2 mil M", "chg": -0.3, "asof": "20/11"}]
    mk = [{"label": "S&P 500", "value": "6.602", "chg": 0.9, "asof": "21/11"},
          {"label": "Nvidia", "value": "US$ 178,88", "chg": -1.4, "asof": "21/11"},
          {"label": "Oro", "value": "US$ 4.065", "chg": 0.4, "asof": "21/11"},
          {"label": "Bitcoin", "value": "US$ 85.200", "chg": 2.1, "asof": "23/11"}]
    return ed, ars, mk


# ------------------------------------------------------------------ main
def main():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    now = datetime.now(AR)
    out = os.path.join(HERE, "demo.html" if "--demo" in sys.argv else "index.html")
    if "--demo" in sys.argv:
        ed, ars, mk = demo_data(now)
        open(out, "w", encoding="utf-8").write(render(cfg, ed, ars, mk, now, demo=True))
        print("demo escrita en", out)
        return
    print("Recopilando titulares…")
    by_sec = collect(cfg, now)
    total = sum(len(v) for v in by_sec.values())
    print("  candidatos:", {k: len(v) for k, v in by_sec.items()})
    if total == 0:
        print("Sin titulares: no se publica (queda la última edición válida).", file=sys.stderr)
        sys.exit(1)
    print("Recopilando indicadores…")
    ars, mk = collect_indicators(cfg)
    print("Edición editorial…")
    ed = ai_edit(cfg, by_sec) or plain_edit(cfg, by_sec)
    if not any(ed.get(s) for s, _ in SECCIONES):
        print("Edición vacía: no se publica.", file=sys.stderr)
        sys.exit(1)
    open(out, "w", encoding="utf-8").write(render(cfg, ed, ars, mk, now))
    print("Listo:", out)


if __name__ == "__main__":
    main()
