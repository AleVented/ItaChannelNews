"""
Bot notizie -> carosello Instagram, tutto gratis.

Uso:
  python main.py demo       # genera un carosello di prova, senza rete
  python main.py generate   # RSS -> LLM -> immagini in out/<cartella>/
  python main.py publish    # pubblica su Instagram l'ultimo carosello generato

Variabili d'ambiente:
  GEMINI_API_KEY    chiave gratuita da Google AI Studio
  GEMINI_MODEL      (opzionale) se vuoto sceglie da solo il modello flash piu' recente
  IG_USER_ID        ID dell'account Instagram Business/Creator
  IG_ACCESS_TOKEN   token di accesso della Pagina Facebook collegata a Instagram
  IG_API_HOST       (opzionale) default graph.facebook.com; usa graph.instagram.com
                    solo se hai un token di "Instagram Login"
  IG_HANDLE         (opzionale) es. @miopagina, mostrato nelle slide
  DRY_RUN=1         (opzionale) in publish non pubblica, stampa soltanto
  GITHUB_REPOSITORY, GITHUB_REF_NAME   impostate in automatico da GitHub Actions
"""
import datetime
import html
import json
import os
import pathlib
import random
import re
import shutil
import sys
import time

import feedparser
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
KEEP_DAYS = 5  # cartelle piu' vecchie vengono cancellate

# I feed possono cambiare indirizzo: quelli che falliscono vengono saltati.
FEEDS = {
    "ANSA": "https://www.ansa.it/sito/ansait_rss.xml",
    "Il Post": "https://www.ilpost.it/feed/",
    "Corriere": "https://xml2.corriereobjects.it/rss/homepage.xml",
    "Repubblica": "https://www.repubblica.it/rss/homepage/rss2.0.xml",
    "Sky TG24": "https://tg24.sky.it/rss/tg24.xml",
    "Google News": "https://news.google.com/rss?hl=it&gl=IT&ceid=IT:it",
}

BRIGHT = [("#FF512F", "#F09819"), ("#7F00FF", "#E100FF"), ("#00C6FF", "#0072FF"),
          ("#11998E", "#38EF7D"), ("#F857A6", "#FF5858"), ("#FC466B", "#3F5EFB")]
SERIOUS = [("#232526", "#414345"), ("#0F2027", "#2C5364"),
           ("#141E30", "#243B55"), ("#2C3E50", "#4CA1AF")]

W, H = 1080, 1350
FONT_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
FONT_REG = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]


# ----------------------------------------------------------------- notizie
def clean(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def fetch_items(per_feed=15):
    items = []
    for name, url in FEEDS.items():
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (news-bot)")
            for e in feed.entries[:per_feed]:
                items.append({"source": name, "title": clean(e.get("title")),
                              "summary": clean(e.get("summary"))[:200],
                              "link": e.get("link", "")})
            print(f"[feed] {name}: {len(feed.entries[:per_feed])} notizie")
        except Exception as ex:  # noqa: BLE001
            print(f"[feed] {name} saltato: {ex}")
    return [i for i in items if i["title"]]


def recent_titles():
    titles = []
    for p in OUT.glob("*/post.json"):
        try:
            titles.append(json.loads(p.read_text())["cover_title"])
        except Exception:  # noqa: BLE001
            pass
    return titles


def cleanup_old():
    if not OUT.exists():
        return
    limit = time.time() - KEEP_DAYS * 86400
    for d in OUT.iterdir():
        if d.is_dir() and d.stat().st_mtime < limit:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------- LLM
def ask_llm(items, already):
    lines = [f"[{i}] ({it['source']}) {it['title']} - {it['summary']}"
             for i, it in enumerate(items)]
    prompt = f"""Sei il redattore di una pagina Instagram di notizie italiane.
Ecco i titoli di oggi da piu' testate:

{chr(10).join(lines)}

Notizie gia' pubblicate di recente (NON ripeterle):
{json.dumps(already, ensure_ascii=False)}

Compito:
1. Scegli UNA storia: la piu' importante e "virale" del momento, cioe' quella
   coperta da piu' testate o che interessa di piu' al pubblico generale.
2. Scrivi un carosello in italiano usando SOLO fatti presenti nei testi sopra.
   Non inventare numeri, nomi, cause o dettagli. Se un dato non e' certo, omettilo.
3. Tono sobrio e non sensazionalistico. Se la storia riguarda tragedie, incidenti
   o vittime: tono "serio", nessun dettaglio morboso, nessuna speculazione.
4. Emoji: una sola per slide, semplice (niente sequenze combinate), pertinente al
   tema (es. treno, sciopero, soldi, scuola, meteo). Niente facce, risate, fuoco,
   teschi o cuori. Per notizie serie usa emoji neutre. "highlight" solo se il dato
   e' scritto davvero nelle fonti, altrimenti stringa vuota.

Rispondi SOLO con JSON in questo formato:
{{"indices": [numeri delle voci usate],
  "tone": "serio" oppure "leggero",
  "cover_title": "titolo forte, max 70 caratteri",
  "cover_emoji": "UNA sola emoji semplice che rappresenta la storia",
  "slides": [{{"title": "max 40 caratteri", "text": "max 220 caratteri",
              "emoji": "UNA sola emoji semplice pertinente",
              "highlight": "dato chiave breve preso dal testo (es. '48 ore', '3 giorni', '15%'), max 14 caratteri, oppure stringa vuota"}}, ... da 3 a 5 slide],
  "caption": "didascalia di 2-4 frasi, max 400 caratteri",
  "hashtags": ["#notizie", ... 5-8 hashtag pertinenti]}}"""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    errors = []
    for model in gemini_candidates(headers):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(url, json=body, timeout=60, headers=headers)
            except requests.RequestException as ex:  # timeout o rete: prossimo modello
                errors.append(f"{model}: {type(ex).__name__}")
                print(f"[llm] {model} non ha risposto ({type(ex).__name__}), provo il prossimo")
                break
            if r.status_code in (429, 500, 503):
                errors.append(f"{model}: HTTP {r.status_code}")
                time.sleep(10 * (attempt + 1))
                continue
            if not r.ok:
                errors.append(f"{model}: HTTP {r.status_code} {r.text[:200]}")
                break  # es. 404: modello non disponibile, passa al prossimo
            try:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(text)
                data["slides"] = data["slides"][:5]
                assert data["slides"] and data["cover_title"]
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{model}: risposta non valida ({ex})")
                break
            print(f"[llm] modello usato: {model}")
            return data
    raise RuntimeError("Nessun modello Gemini ha risposto:\n" + "\n".join(errors))


def gemini_candidates(headers):
    """Sceglie da solo i modelli 'flash' disponibili, dal piu' recente."""
    forced = os.getenv("GEMINI_MODEL")
    if forced:
        return [forced]
    found = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers=headers, params={"pageSize": 200}, timeout=30)
        r.raise_for_status()
        for m in r.json().get("models", []):
            name = m["name"].split("/")[-1]
            if "generateContent" in m.get("supportedGenerationMethods", []) and \
                    re.fullmatch(r"gemini-\d+(?:\.\d+)?-flash(?:-lite)?", name):
                found.append(name)
    except Exception as ex:  # noqa: BLE001
        print("[llm] elenco modelli non disponibile:", ex)

    def rank(n):
        return (float(re.search(r"gemini-(\d+(?:\.\d+)?)", n).group(1)), "lite" not in n)

    found = sorted(set(found), key=rank, reverse=True)[:4]
    print("[llm] modelli candidati:", found)
    return found or ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


# ----------------------------------------------------------------- grafica
EMOJI_FONTS = ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
               "/usr/share/fonts/noto/NotoColorEmoji.ttf"]
ACCENTS = ["#FFE66D", "#FF6B6B", "#4ECDC4", "#FFFFFF", "#A29BFE", "#FD79A8", "#55EFC4", "#74B9FF"]
SOFT = ["#FF6B6B", "#4ECDC4", "#A29BFE", "#FD79A8", "#55EFC4", "#74B9FF", "#FDCB6E"]
INK, INK2, YELLOW = "#1B1B3A", "#2D2D44", "#FFE66D"


def load_font(paths, size):
    for p in paths:
        if os.path.exists(p):
            # layout BASIC: evita gli errori del motore raqm su alcuni caratteri
            return ImageFont.truetype(p, size, layout_engine=ImageFont.Layout.BASIC)
    return ImageFont.load_default(size)


def wrap(text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if font.getlength(test) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit(text, paths, max_w, max_h, start, minimum=30, spacing=1.25):
    size = start
    while size >= minimum:
        font = load_font(paths, size)
        lines = wrap(text, font, max_w)
        if len(lines) * size * spacing <= max_h:
            return font, lines, size * spacing
        size -= 4
    font = load_font(paths, minimum)
    return font, wrap(text, font, max_w), minimum * spacing


def hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def gradient(c1, c2):
    a = Image.new("RGB", (1, 2))
    a.putpixel((0, 0), hex_rgb(c1))
    a.putpixel((0, 1), hex_rgb(c2))
    return a.resize((W, H), Image.BILINEAR).convert("RGBA")


def draw_lines(d, lines, font, x, y, step, fill, anchor="la"):
    for ln in lines:
        d.text((x, y), ln, font=font, fill=fill, anchor=anchor)
        y += step
    return y


def emoji_image(ch, size):
    """Emoji a colori (font Noto) ridimensionata: il 'disegno' di ogni slide."""
    ch = (ch or "").strip()[:1]
    if not ch or ord(ch) < 0x2190:
        ch = "\U0001F4F0"  # giornale
    for path in EMOJI_FONTS:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, 109)  # il font a bitmap accetta solo 109
                tmp = Image.new("RGBA", (180, 180), (0, 0, 0, 0))
                ImageDraw.Draw(tmp).text((20, 20), ch, font=font, embedded_color=True)
                box = tmp.getbbox()
                if not box:
                    return None
                tmp = tmp.crop(box)
                k = size / max(tmp.size)
                return tmp.resize((max(1, int(tmp.width * k)), max(1, int(tmp.height * k))),
                                  Image.LANCZOS)
            except Exception as ex:  # noqa: BLE001
                print("[emoji] errore:", ex)
                return None
    return None


def put_emoji(img, ch, cx, cy, size):
    em = emoji_image(ch, size)
    if em:
        img.alpha_composite(em, (int(cx - em.width / 2), int(cy - em.height / 2)))
    else:  # senza font emoji: una stella
        ImageDraw.Draw(img).text((cx, cy), "\u2605", font=load_font(FONT_BOLD, size),
                                 fill=INK, anchor="mm")


def make_shapes(rng, n):
    span = W + (n - 1) * int(W * 0.45)
    shapes = []
    for _ in range(6 + n):
        shapes.append(("circle", rng.randint(0, span), rng.randint(-80, H + 80),
                       rng.randint(110, 300), rng.choice(ACCENTS), rng.randint(60, 120)))
    for _ in range(3):
        shapes.append(("ring", rng.randint(0, span), rng.randint(0, H),
                       rng.randint(90, 220), "#FFFFFF", 130))
    for _ in range(3):
        shapes.append(("dots", rng.randint(0, span), rng.randint(60, H - 300), 0, "#FFFFFF", 150))
    return shapes


def draw_shapes(img, shapes, idx):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    shift = idx * int(W * 0.45)  # le forme "scorrono" da una slide all'altra
    for kind, x, y, r, col, a in shapes:
        x -= shift
        rgba = hex_rgb(col) + (a,)
        if kind == "circle":
            d.ellipse((x - r, y - r, x + r, y + r), fill=rgba)
        elif kind == "ring":
            d.ellipse((x - r, y - r, x + r, y + r), outline=rgba, width=18)
        else:
            for i in range(5):
                for j in range(4):
                    cx, cy = x + i * 46, y + j * 46
                    d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=rgba)
    return Image.alpha_composite(img, layer)


def card(img, box, radius=56):
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(shadow).rounded_rectangle((x0, y0 + 16, x1, y1 + 16), radius=radius,
                                             fill=(0, 0, 0, 90))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(20)))
    ImageDraw.Draw(img).rounded_rectangle(box, radius=radius, fill="white")
    return img


def pill(d, x, y, w, h, fill):
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=fill)


def render(post, sources, outdir, handle=""):
    handle = "".join(ch for ch in (handle or "") if ch.isprintable()).strip()
    rng = random.Random(post.get("cover_title", "x"))
    c1, c2 = rng.choice(SERIOUS if post.get("tone") == "serio" else BRIGHT)
    n = len(post["slides"]) + 2
    shapes = make_shapes(rng, n)
    ring = rng.choice(SOFT)
    tag, bold, reg = load_font(FONT_BOLD, 34), FONT_BOLD, FONT_REG
    pages = []
    for idx in range(n):
        img = draw_shapes(gradient(c1, c2), shapes, idx)

        # ---- corpo della slide
        if idx == 0:  # copertina
            img = card(img, (70, 660, 1010, 1100))
            d = ImageDraw.Draw(img)
            cx, cy = W // 2, 400
            d.ellipse((cx - 215, cy - 215, cx + 215, cy + 215), fill="white")
            d.ellipse((cx - 190, cy - 190, cx + 190, cy + 190), fill=ring)
            put_emoji(img, post.get("cover_emoji"), cx, cy, 250)
            d.rounded_rectangle((W // 2 - 80, 705, W // 2 + 80, 719), radius=7, fill=ring)
            f, lines, step = fit(post["cover_title"], bold, 800, 330, 84, 44, 1.18)
            draw_lines(d, lines, f, W // 2, 890 - len(lines) * step / 2, step, INK, "ma")
            pill(d, W // 2 - 160, 1122, 320, 76, YELLOW)
            d.text((W // 2, 1160), "Scorri  \u2192", font=load_font(bold, 40), fill=INK, anchor="mm")

        elif idx < n - 1:  # contenuto
            s = post["slides"][idx - 1]
            img = card(img, (70, 200, 1010, 1120))
            d = ImageDraw.Draw(img)
            d.ellipse((90, 150, 210, 270), fill=YELLOW, outline="white", width=8)
            d.text((150, 210), str(idx), font=load_font(bold, 66), fill=INK, anchor="mm")
            ecol = SOFT[idx % len(SOFT)]
            d.ellipse((800, 140, 1000, 340), fill="white")
            d.ellipse((812, 152, 988, 328), fill=ecol)
            put_emoji(img, s.get("emoji"), 900, 240, 112)
            ft, tl, ts = fit(s["title"], bold, 660, 230, 66, 40, 1.15)
            y = draw_lines(d, tl, ft, 120, 350, ts, INK)
            d.rounded_rectangle((120, y + 6, 280, y + 20), radius=7, fill=ecol)
            y += 50
            hl = (s.get("highlight") or "").strip()
            if hl:  # il dato chiave in evidenza, stile infografica
                fh, hlines, _ = fit(hl, bold, 760, 100, 92, 40, 1.0)
                d.rounded_rectangle((120, y, 960, y + 150), radius=40, fill=INK)
                d.text((540, y + 75), " ".join(hlines), font=fh, fill=YELLOW, anchor="mm")
                y += 190
            fb, bl, bs = fit(s["text"], reg, 840, 1085 - y, 56, 32, 1.35)
            draw_lines(d, bl, fb, 120, y, bs, INK2)

        else:  # chiusura
            img = card(img, (70, 200, 1010, 1120))
            d = ImageDraw.Draw(img)
            d.ellipse((800, 140, 1000, 340), fill="white")
            d.ellipse((812, 152, 988, 328), fill=ring)
            put_emoji(img, "\U0001F514", 900, 240, 112)
            d.text((120, 380), "Fonti", font=load_font(bold, 70), fill=INK)
            fc, x, y = load_font(bold, 42), 120, 500
            for k, src in enumerate(sources or ["varie testate"]):
                w = int(fc.getlength(src)) + 64
                if x + w > 960:
                    x, y = 120, y + 100
                pill(d, x, y, w, 76, SOFT[k % len(SOFT)])
                d.text((x + w // 2, y + 38), src, font=fc, fill=INK, anchor="mm")
                x += w + 20
            y += 140
            d.rounded_rectangle((120, y, 960, y + 130), radius=40, fill=INK)
            fcta, cl, _ = fit("Seguici per le notizie di ogni giorno", bold, 760, 90, 46, 30, 1.1)
            d.text((540, y + 65), " ".join(cl), font=fcta, fill=YELLOW, anchor="mm")
            fn = load_font(reg, 34)
            draw_lines(d, wrap("Contenuto riassunto con l'aiuto dell'IA: verifica sempre "
                               "le fonti originali.", fn, 840), fn, 120, y + 175, 48, "#666666")

        # ---- cornice comune: etichetta, numero, handle, pallini di avanzamento
        d = ImageDraw.Draw(img)
        label = "ULTIME NOTIZIE"
        pill(d, 70, 52, int(tag.getlength(label)) + 56, 64, "white")
        d.text((98, 84), label, font=tag, fill=INK, anchor="lm")
        d.text((W - 70, 84), f"{idx + 1}/{n}", font=tag, fill="white", anchor="rm")
        if handle:
            d.text((W // 2, 1248), handle, font=tag, fill="white", anchor="mm")
        total = n * 34
        for i in range(n):
            x = W // 2 - total // 2 + i * 34 + 17
            r = 11 if i == idx else 6
            d.ellipse((x - r, 1302 - r, x + r, 1302 + r), fill="white" if i == idx else "#DDDDDD")
        pages.append(img.convert("RGB"))

    outdir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, im in enumerate(pages, 1):
        name = f"slide{i}.jpg"  # Instagram API vuole JPEG
        im.save(outdir / name, "JPEG", quality=92)
        names.append(name)
    return names


# -------------------------------------------------------------------- modi
def cmd_generate():
    cleanup_old()
    items = fetch_items()
    if len(items) < 5:
        sys.exit("Troppo poche notizie recuperate: controlla i feed.")
    post = ask_llm(items, recent_titles())
    used = [items[i] for i in post.get("indices", []) if isinstance(i, int) and 0 <= i < len(items)]
    sources = sorted({u["source"] for u in used if u["source"] != "Google News"})
    folder = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
    outdir = OUT / folder
    names = render(post, sources, outdir, os.getenv("IG_HANDLE", ""))
    caption = f"{post['caption']}\n\nFonti: {', '.join(sources) or 'varie testate'}\n\n" \
              f"{' '.join(post.get('hashtags', []))}"
    (outdir / "post.json").write_text(json.dumps(
        {"folder": folder, "images": names, "cover_title": post["cover_title"],
         "caption": caption[:2150]}, ensure_ascii=False, indent=2))
    print("Generato:", folder, "-", post["cover_title"])


def cmd_demo():
    post = {"tone": "leggero", "cover_title": "Sciopero dei treni: cosa cambia da domani",
            "cover_emoji": "\U0001F686",
            "slides": [{"title": "Cosa succede", "emoji": "\U0001F6A8", "highlight": "24 ore",
                        "text": "Testo di prova per vedere come viene il carosello con piu' "
                                "righe di testo dentro la slide."},
                       {"title": "Chi e' coinvolto", "emoji": "\U0001F465", "highlight": "",
                        "text": "Un secondo punto di esempio, senza dato in evidenza."},
                       {"title": "E adesso?", "emoji": "\U0001F4C5", "highlight": "dal 10",
                        "text": "Un terzo punto, breve."}]}
    names = render(post, ["ANSA", "Il Post"], OUT / "demo", "@tuapagina")
    print("Demo creata in out/demo:", names)


def wait_url(url, tries=12):
    for _ in range(tries):
        if requests.head(url, timeout=20).status_code == 200:
            return
        time.sleep(5)
    raise RuntimeError(f"URL non raggiungibile: {url}")


def cmd_publish():
    latest = sorted(OUT.glob("*/post.json"))[-1]
    post = json.loads(latest.read_text())
    repo, branch = os.environ["GITHUB_REPOSITORY"], os.getenv("GITHUB_REF_NAME", "main")
    urls = [f"https://raw.githubusercontent.com/{repo}/{branch}/out/{post['folder']}/{n}"
            for n in post["images"]]
    if os.getenv("DRY_RUN") == "1":
        print("DRY_RUN\n", "\n".join(urls), "\n", post["caption"])
        return
    for u in urls:
        wait_url(u)
    host = os.getenv("IG_API_HOST") or "graph.facebook.com"
    g = f"https://{host}/{os.getenv('IG_API_VERSION') or 'v25.0'}"
    uid, token = os.environ["IG_USER_ID"], os.environ["IG_ACCESS_TOKEN"]

    def call(method, path, **params):
        params["access_token"] = token
        r = requests.request(method, f"{g}/{path}", timeout=60,
                             **({"data": params} if method == "POST" else {"params": params}))
        if not r.ok:
            raise RuntimeError(f"Instagram API: {r.text}")
        return r.json()

    children = [call("POST", f"{uid}/media", image_url=u, is_carousel_item="true")["id"]
                for u in urls]
    parent = call("POST", f"{uid}/media", media_type="CAROUSEL",
                  children=",".join(children), caption=post["caption"])["id"]
    for _ in range(20):
        status = call("GET", parent, fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError("Instagram ha rifiutato il carosello")
        time.sleep(5)
    print("Pubblicato:", call("POST", f"{uid}/media_publish", creation_id=parent))


if __name__ == "__main__":
    {"demo": cmd_demo, "generate": cmd_generate, "publish": cmd_publish}[sys.argv[1]]()
