"""
Bot notizie -> carosello Instagram, tutto gratis.

Uso:
  python main.py demo       # genera un carosello di prova, senza rete
  python main.py generate   # RSS -> LLM -> immagini in out/<cartella>/
  python main.py publish    # pubblica su Instagram l'ultimo carosello generato

Variabili d'ambiente:
  GEMINI_API_KEY    chiave gratuita da Google AI Studio
  GEMINI_MODEL      (opzionale) default gemini-2.0-flash, controlla il nome attuale
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
from PIL import Image, ImageDraw, ImageFont

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
                              "summary": clean(e.get("summary"))[:300],
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

Rispondi SOLO con JSON in questo formato:
{{"indices": [numeri delle voci usate],
  "tone": "serio" oppure "leggero",
  "cover_title": "titolo forte, max 70 caratteri",
  "slides": [{{"title": "max 40 caratteri", "text": "max 220 caratteri"}}, ... da 3 a 5 slide],
  "caption": "didascalia di 2-4 frasi, max 400 caratteri",
  "hashtags": ["#notizie", ... 5-8 hashtag pertinenti]}}"""
    model = os.getenv("GEMINI_MODEL") or "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    for attempt in range(4):
        r = requests.post(url, json=body, timeout=90,
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        if r.status_code in (429, 500, 503):
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        data["slides"] = data["slides"][:5]
        assert data["slides"] and data["cover_title"]
        return data
    raise RuntimeError("LLM non raggiungibile (limiti del piano gratuito?)")


# ----------------------------------------------------------------- grafica
def load_font(paths, size):
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
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


def gradient(c1, c2):
    a = Image.new("RGB", (1, 2))
    a.putpixel((0, 0), tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5)))
    a.putpixel((0, 1), tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5)))
    return a.resize((W, H), Image.BILINEAR).convert("RGBA")


def draw_lines(d, lines, font, x, y, step, fill, anchor="la"):
    for ln in lines:
        d.text((x, y), ln, font=font, fill=fill, anchor=anchor)
        y += step
    return y


def render(post, sources, outdir, handle=""):
    palette = random.choice(SERIOUS if post.get("tone") == "serio" else BRIGHT)
    n = len(post["slides"]) + 2
    pages = []
    for idx in range(n):
        img = gradient(*palette)
        panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle((60, 190, 1020, 1190), radius=48, fill=(0, 0, 0, 105))
        img = Image.alpha_composite(img, panel)
        d = ImageDraw.Draw(img)
        small = load_font(FONT_BOLD, 34)
        d.text((90, 90), "ULTIME NOTIZIE", font=small, fill="white")
        d.text((W - 90, 90), f"{idx + 1}/{n}", font=small, fill="white", anchor="ra")
        if handle:
            d.text((W // 2, H - 90), handle, font=small, fill="white", anchor="mm")

        if idx == 0:  # copertina
            f, lines, step = fit(post["cover_title"], FONT_BOLD, 840, 700, 92, 48, 1.2)
            y = 690 - len(lines) * step / 2
            draw_lines(d, lines, f, W // 2, y, step, "white", "ma")
            d.text((W // 2, 1120), "Scorri  →", font=load_font(FONT_BOLD, 40),
                   fill="#FFE66D", anchor="mm")
        elif idx < n - 1:  # contenuto
            s = post["slides"][idx - 1]
            ft, tl, ts = fit(s["title"], FONT_BOLD, 840, 260, 66, 40, 1.2)
            y = draw_lines(d, tl, ft, 120, 260, ts, "#FFE66D")
            fb, bl, bs = fit(s["text"], FONT_REG, 840, 1120 - y - 40, 54, 30, 1.35)
            draw_lines(d, bl, fb, 120, y + 40, bs, "white")
        else:  # chiusura
            d.text((120, 260), "Fonti", font=load_font(FONT_BOLD, 66), fill="#FFE66D")
            fs = load_font(FONT_REG, 52)
            y = draw_lines(d, wrap(", ".join(sources) or "Testate giornalistiche", fs, 840),
                           fs, 120, 380, 70, "white")
            fn = load_font(FONT_REG, 38)
            draw_lines(d, wrap("Seguici per le notizie piu' importanti, ogni giorno. "
                               "Contenuto riassunto con l'aiuto dell'IA: verifica sempre "
                               "le fonti originali.", fn, 840), fn, 120, y + 60, 56, "white")
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
    folder = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M")
    outdir = OUT / folder
    names = render(post, sources, outdir, os.getenv("IG_HANDLE", ""))
    caption = f"{post['caption']}\n\nFonti: {', '.join(sources) or 'varie testate'}\n\n" \
              f"{' '.join(post.get('hashtags', []))}"
    (outdir / "post.json").write_text(json.dumps(
        {"folder": folder, "images": names, "cover_title": post["cover_title"],
         "caption": caption[:2150]}, ensure_ascii=False, indent=2))
    print("Generato:", folder, "-", post["cover_title"])


def cmd_demo():
    post = {"tone": "leggero", "cover_title": "Scioperi, treni e targhe: le notizie di oggi",
            "slides": [{"title": "Cosa succede", "text": "Testo di prova per vedere come viene "
                        "il carosello con piu' righe di testo dentro la slide."},
                       {"title": "Perche' conta", "text": "Un secondo punto di esempio."},
                       {"title": "E adesso?", "text": "Un terzo punto, breve."}]}
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
