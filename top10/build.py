#!/usr/bin/env python3
"""Build a static Stremio/Nuvio add-on with ranked Top 10 rows.

Writes two add-ons into OUT_DIR:
  netflix/manifest.json  square tiles: outlined rank number tucked behind the poster
  apple/manifest.json    tall posters with a white rank number in the corner

Data: TMDB trending (today) when TMDB_API_KEY is set, otherwise Cinemeta's
popular list. Needs only Pillow. Usage: python build.py OUT_DIR
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "InterDisplay-Black.otf")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
TMDB_KEY = os.environ.get("TMDB_API_KEY", "").strip()
TOP = 10
TYPES = ("movie", "series")
UA = {"User-Agent": "top10-addon/1.0"}


def get(url, headers=None, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:  # network hiccup: retry
            pass
        time.sleep(2 * (attempt + 1))
    return None


def get_json(url, headers=None):
    body = get(url, headers)
    return json.loads(body) if body else None


def get_image(url):
    if not url:
        return None
    body = get(url)
    if not body:
        return None
    try:
        img = Image.open(io.BytesIO(body))
        img.load()
        return img.convert("RGBA")
    except Exception:
        return None


def metahub(kind, imdb):
    return f"https://images.metahub.space/{kind}/{'large' if kind != 'logo' else 'medium'}/{imdb}/img"


# ---------- data sources ----------

def cinemeta(kind):
    data = get_json(f"https://v3-cinemeta.strem.io/catalog/{kind}/top.json") or {}
    out = []
    for m in data.get("metas", []):
        imdb = m.get("imdb_id") or m.get("id")
        if not (imdb or "").startswith("tt") or not m.get("name"):
            continue
        out.append({
            "id": imdb, "type": kind, "name": m["name"],
            "poster": metahub("poster", imdb), "poster_alt": m.get("poster"),
            "background": metahub("background", imdb), "background_alt": m.get("background"),
            "logo": m.get("logo") or metahub("logo", imdb),
            "description": m.get("description"), "releaseInfo": m.get("releaseInfo"),
            "imdbRating": m.get("imdbRating"), "genres": m.get("genres") or [],
        })
    return out


def tmdb(kind):
    t = "movie" if kind == "movie" else "tv"
    if len(TMDB_KEY) > 40:  # v4 read access token
        auth, q = {"Authorization": f"Bearer {TMDB_KEY}"}, ""
    else:
        auth, q = None, f"&api_key={urllib.parse.quote(TMDB_KEY)}"
    api = "https://api.themoviedb.org/3"
    data = get_json(f"{api}/trending/{t}/day?language=en-US{q}", auth) or {}
    genres = {g["id"]: g["name"] for g in (get_json(f"{api}/genre/{t}/list?language=en-US{q}", auth) or {}).get("genres", [])}
    out = []
    for m in data.get("results", [])[: TOP + 6]:
        ext = get_json(f"{api}/{t}/{m['id']}/external_ids?x=1{q}", auth) or {}
        imdb = ext.get("imdb_id") or ""
        if not imdb.startswith("tt"):
            continue
        img = "https://image.tmdb.org/t/p/"
        date = m.get("release_date") or m.get("first_air_date") or ""
        out.append({
            "id": imdb, "type": kind, "name": m.get("title") or m.get("name"),
            "poster": m.get("poster_path") and img + "w780" + m["poster_path"], "poster_alt": metahub("poster", imdb),
            "background": m.get("backdrop_path") and img + "w1280" + m["backdrop_path"], "background_alt": metahub("background", imdb),
            "logo": metahub("logo", imdb),
            "description": m.get("overview"), "releaseInfo": date[:4] or None,
            "imdbRating": None, "genres": [genres[g] for g in m.get("genre_ids", []) if g in genres],
        })
    return out


# ---------- drawing ----------

def font(size):
    return ImageFont.truetype(FONT, size)


def cover(img, w, h):
    """Scale and centre-crop img to exactly w x h."""
    s = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * s)), max(h, round(img.height * s))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def gradient(w, h, stops, horizontal=False):
    """Alpha mask from a list of (position 0..1, alpha 0..255)."""
    n = w if horizontal else h
    line = Image.new("L", (n, 1) if horizontal else (1, n))
    px = line.load()
    for i in range(n):
        t = i / max(1, n - 1)
        for (p0, a0), (p1, a1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                a = a0 + (a1 - a0) * ((t - p0) / max(1e-6, p1 - p0))
                break
        else:
            a = stops[-1][1]
        px[(i, 0) if horizontal else (0, i)] = int(a)
    return line.resize((w, h))


def fit_font(text, max_w, max_h):
    """Largest font whose glyph box fits max_w x max_h."""
    lo, hi = 10, 2000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        l, t, r, b = font(mid).getbbox(text)
        if r - l <= max_w and b - t <= max_h:
            lo = mid
        else:
            hi = mid - 1
    return font(lo)


def draw_text_box(layer, text, f, left, bottom, **kw):
    """Draw text so its glyph box's left edge and baseline-bottom sit at (left, bottom)."""
    l, t, r, b = f.getbbox(text)
    ImageDraw.Draw(layer).text((left - l, bottom - b), text, font=f, **kw)
    return r - l, b - t


def netflix_card(rank, poster, W, H):
    """Netflix Top 10 tile on black: outlined number tucked behind the poster.

    The number + poster group is H wide (square); on a wider canvas it is centred.
    """
    card = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    gx = (W - H) // 2  # group left edge
    pw = round(H * 0.62)
    px = gx + H - pw
    stroke = max(4, H // 50)
    tuck = round(H * 0.2)  # how far the number may run under the poster
    # Number exactly as tall as the poster; squeezed sideways only if it would not fit.
    text = str(rank)
    th = H - 2 * stroke
    probe = font(400)
    l, t, r, b = probe.getbbox(text)
    f = font(round(400 * th / (b - t)))
    l, t, r, b = f.getbbox(text)
    glyph = Image.new("L", (r - l, b - t), 0)
    ImageDraw.Draw(glyph).text((-l, -t), text, font=f, fill=255)
    room = px - gx + tuck - 2 * stroke - round(H * 0.06)  # clear of rounded corners
    if glyph.width > room:
        glyph = glyph.resize((room, th), Image.LANCZOS)
    gw = glyph.width
    mask = Image.new("L", (W, H), 0)
    mask.paste(glyph, (px + min(tuck, gw // 4) - stroke - gw, stroke))
    outline = mask.filter(ImageFilter.MaxFilter(2 * stroke + 1))
    card.paste((175, 175, 175, 255), (0, 0), outline)
    card.paste((10, 10, 10, 255), (0, 0), mask)
    edge = Image.new("RGBA", (round(H * 0.08), H), (0, 0, 0, 255))
    edge.putalpha(gradient(edge.width, H, [(0, 0), (1, 200)], horizontal=True))
    card.alpha_composite(edge, (px - edge.width, 0))
    card.alpha_composite(cover(poster, pw, H), (px, 0))
    return card


def apple_number(img, rank, size_ratio):
    """White rank number in the top-left corner with a soft shadow, like Apple TV."""
    W, H = img.size
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    shade.putalpha(gradient(W, H, [(0, 150), (0.3, 40), (0.45, 0)]))
    img.alpha_composite(shade)
    text = str(rank)
    f = font(int(min(W, H) * size_ratio))
    l, t, r, b = f.getbbox(text)
    x0, top = int(W * 0.05), int(min(W, H) * 0.045)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x0 - l, top - t), text, font=f, fill=(0, 0, 0, 170))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(max(4, W // 90))), (0, max(2, W // 160)))
    ImageDraw.Draw(img).text((x0 - l, top - t), text, font=f, fill=(255, 255, 255, 255))
    return img


def apple_poster(rank, poster):
    return apple_number(cover(poster, 500, 750), rank, 0.30)


def apple_wide(rank, backdrop, poster, logo, name):
    """16:9 version for Nuvio's Landscape Posters mode: backdrop, number, title logo."""
    W, H = 960, 540
    img = cover(backdrop or poster, W, H)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    shade.putalpha(gradient(W, H, [(0, 0), (0.55, 0), (1, 200)]))
    img.alpha_composite(shade)
    if logo and logo.getbbox():
        logo = logo.crop(logo.getbbox())
        s = min(W * 0.46 / logo.width, H * 0.22 / logo.height)
        logo = logo.resize((max(1, int(logo.width * s)), max(1, int(logo.height * s))), Image.LANCZOS)
        img.alpha_composite(logo, (int(W * 0.05), H - int(H * 0.07) - logo.height))
    else:
        f = fit_font(name, int(W * 0.8), int(H * 0.1))
        draw_text_box(img, name, f, int(W * 0.05), H - int(H * 0.08), fill=(255, 255, 255, 255))
    return apple_number(img, rank, 0.36)


def save(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.convert("RGB").save(path, "JPEG", quality=86, optimize=True, progressive=True)


# ---------- add-on ----------

def manifest(style):
    nice = {"netflix": "Netflix style", "apple": "Apple TV style"}[style]
    return {
        "id": f"community.top10.ranked.{style}",
        "version": "1.2.0",
        "name": f"Top 10 ({nice})",
        "description": "Today's Top 10 movies and series with big rank numbers. Updated daily.",
        "logo": f"{BASE_URL}/logo.png",
        "resources": ["catalog"],
        "types": list(TYPES),
        "catalogs": [{"type": t, "id": f"top10-{style}", "name": "Top 10"} for t in TYPES],
        "behaviorHints": {"configurable": False},
    }


def addon_logo(path):
    img = Image.new("RGBA", (256, 256), (16, 16, 16, 255))
    f = fit_font("10", 200, 170)
    l, t, r, b = f.getbbox("10")
    ImageDraw.Draw(img).text(((256 - (r - l)) // 2 - l, (256 - (b - t)) // 2 - t), "10", font=f,
                             fill=(12, 12, 12, 255), stroke_width=6, stroke_fill=(229, 9, 20, 255))
    save(img, path)


def build(out, fetch_list):
    if not BASE_URL:
        sys.exit("BASE_URL is not set")
    stamp = time.strftime("%Y%m%d")
    metas = {"netflix": {}, "apple": {}}
    for kind in TYPES:
        items = fetch_list(kind)
        rows = {"netflix": [], "apple": []}
        rank = 0
        for it in items:
            if rank == TOP:
                break
            poster = get_image(it["poster"]) or get_image(it.get("poster_alt"))
            if not poster:
                continue
            backdrop = get_image(it["background"]) or get_image(it.get("background_alt"))
            logo = get_image(it.get("logo"))
            rank += 1
            name = f"{kind}-{rank:02d}-{it['id']}-{stamp}.jpg"
            save(netflix_card(rank, poster, 600, 600), os.path.join(out, "img", "t2", name))
            save(netflix_card(rank, poster, 960, 540), os.path.join(out, "img", "t2w", name))
            save(apple_poster(rank, poster), os.path.join(out, "img", "a", name))
            save(apple_wide(rank, backdrop, poster, logo, it["name"]), os.path.join(out, "img", "w", name))
            base = {k: it[k] for k in ("id", "type", "name", "description", "releaseInfo", "imdbRating", "genres") if it.get(k)}
            clean_bg = it["background"] if backdrop else it.get("background_alt")
            if clean_bg:
                base["background"] = clean_bg  # hero and details keep the clean artwork
            if it.get("logo") and logo:
                base["logo"] = it["logo"]
            rows["netflix"].append({**base, "poster": f"{BASE_URL}/img/t2/{name}", "posterShape": "square",
                                    "landscapePoster": f"{BASE_URL}/img/t2w/{name}"})
            rows["apple"].append({**base, "poster": f"{BASE_URL}/img/a/{name}", "posterShape": "poster",
                                  "landscapePoster": f"{BASE_URL}/img/w/{name}"})
            print(f"{kind} #{rank} {it['name']} ({it['id']})")
        if rank < 5:
            sys.exit(f"only {rank} {kind} titles could be built; keeping the previous add-on")
        for style in rows:
            metas[style][kind] = rows[style]

    for style in metas:
        d = os.path.join(out, style)
        os.makedirs(os.path.join(d, "catalog"), exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as fh:
            json.dump(manifest(style), fh, indent=1)
        for kind in TYPES:
            os.makedirs(os.path.join(d, "catalog", kind), exist_ok=True)
            with open(os.path.join(d, "catalog", kind, f"top10-{style}.json"), "w") as fh:
                json.dump({"metas": metas[style][kind], "cacheMaxAge": 3600}, fh, indent=1)
    addon_logo(os.path.join(out, "logo.png"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(sys.argv[1], tmdb if TMDB_KEY else cinemeta)
