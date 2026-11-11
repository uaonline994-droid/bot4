"""
Telegram-бот пошуку деталей (OLX, Пром/Bigl, веб-пошук) для Vercel (serverless).

Як це працює на Vercel:
  * Telegram шле оновлення на webhook  POST /api/webhook
  * стан (власник, пошуки, знайдене, діалог) зберігається в Redis (Upstash / Vercel KV)
  * періодична перевірка пошуків - GET /api/cron (Vercel Cron або зовнішній пінгер)
  * одноразове підключення webhook - відкрий в браузері /api/setup?key=<CRON_SECRET>

Змінні оточення (Vercel -> Settings -> Environment Variables):
  BOT_TOKEN                  токен від @BotFather
  CRON_SECRET                будь-який довгий випадковий рядок (захист /api/cron і /api/setup)
  KV_REST_API_URL + KV_REST_API_TOKEN   (або UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN)
  OWNER_ID                   (необов'язково) Telegram chat id власника; інакше власник - перший, хто напише /start
"""
import re, json, time, html, hashlib, os, urllib.parse
import requests
from bs4 import BeautifulSoup
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CRON_SECRET = os.getenv("CRON_SECRET", "").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()
KV_URL = (os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN") or ""
STATE_KEY = "parts_bot_state"
LOCAL_STATE = "/tmp/parts_bot_state.json"   # тільки як запасний варіант для локальних тестів

TOLERANCE = 0.15          # "майже підходить": до +15% над ціною йде в кошик
MAX_PAGES = 2             # сторінок на джерело за один прохід (на Vercel час обмежений)
TIME_BUDGET = 45          # секунд на один запит (maxDuration у vercel.json = 60)
HTTP_TIMEOUT = 8

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "uk-UA,uk;q=0.9"}
SHOPS = ("prom.ua", "bigl.ua", "olx.ua", "rozetka.com.ua", "aliexpress", "besplatka.ua", "hotline.ua", "allo.ua")

_deadline = [0.0]

def begin(budget=TIME_BUDGET):
    _deadline[0] = time.time() + budget

def time_left():
    return _deadline[0] - time.time()

# ================= розпізнавання iCloud (різні формулювання укр/рус/англ) =================
def norm(t):
    t = t.lower().replace("ё", "е")
    for q in "’`ʼ´":
        t = t.replace(q, "'")
    return re.sub(r"\s+", " ", t)

W = r"(?<![а-яіїєґa-z])"  # початок слова
IC = r"(?:icloud|i-cloud|айклауд\w*|айклоуд\w*|айклаут\w*|аикл\w+|apple\s?id|эпл\s?айди|епл\s?айді|аккаунт\w*|акаунт\w*|\bід\b|\bid\b)"
NEG = r"(?:без|не|нет|ні|нема\w*|немає|відсутн\w*|отсутств\w*)"
LOCK_STRONG = re.compile(W + r"(?:за?блок\w*|заблокир\w*|залоч\w*|залок\w*|заклоч\w*|блокиров\w*|блокован\w*|"
                         r"lock\w*|mdm|залип\w*|привяз\w*|прив'яз\w*|icloud\s*замок|замок\s*icloud)")
LOCK_WEAK = re.compile(rf"(?:{IC}.{{0,20}}{W}блок|{W}блок\w*.{{0,12}}{IC})")
CLEAN = re.compile(rf"(?:{W}{NEG}\s+(?:за?блок\w*|заблокир\w*|залоч\w*|залок\w*|lock\w*|привяз\w*|прив'яз\w*)|{W}{NEG}\s+{IC}|{W}чист\w*\s+{IC}|{IC}\s+(?:чист\w*|clean|free|свобод\w*|вільн\w*|відв'яз\w*|отвяз\w*)|"
                   rf"{W}(?:відв'язан\w*|отвязан\w*|розблок\w*|разблок\w*)|{W}без\s+блок\w*)")

def negated(t, m):
    return re.search(rf"{W}{NEG}\W+$", t[max(0, m.start() - 14):m.start()]) is not None

def icloud_status(text):
    t = norm(text)
    locked = any(not negated(t, m) for rx in (LOCK_STRONG, LOCK_WEAK) for m in rx.finditer(t))
    clean = CLEAN.search(t) is not None
    if locked and clean:
        return "unknown"   # суперечливий текст -> перевіряй вручну
    return "locked" if locked else "clean" if clean else "unknown"

# ================= джерела =================
def get(url, **kw):
    if time_left() <= 2:
        return None
    try:
        r = requests.get(url, headers=UA, timeout=min(HTTP_TIMEOUT, max(2, time_left())), **kw)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None

def parse_price(text):
    m = re.search(r"(\d[\d\s ]{1,9})\s*(грн|₴|uah)", text.lower())
    return int(re.sub(r"\D", "", m.group(1))) if m else None

def src_olx(q, pmax, page):
    r = get("https://www.olx.ua/api/v1/offers/", params={"offset": page * 40, "limit": 40, "query": q,
                                                         "filter_float_price:to": int(pmax * (1 + TOLERANCE))})
    out = []
    try:
        data = r.json().get("data", []) if r else []
    except ValueError:
        data = []
    for o in data:
        price = next((p["value"].get("value") for p in o.get("params", []) if p.get("key") == "price"), None)
        out.append(dict(title=o.get("title", ""), url=o.get("url", ""), price=price, source="OLX",
                        text=o.get("title", "") + " " + re.sub("<[^>]+>", " ", o.get("description", ""))))
    return out

def src_prom(q, pmax, page):
    r = get("https://prom.ua/ua/search", params={"search_term": q, "page": page + 1})
    out = []
    if not r:
        return out
    for blk in BeautifulSoup(r.text, "html.parser").select('[data-qaid="product_block"]'):
        a = blk.select_one('[data-qaid="product_link"]') or blk.find("a", href=True)
        pr = blk.select_one('[data-qaid="product_price"]')
        if a:
            out.append(dict(title=a.get_text(" ", strip=True) or blk.get_text(" ", strip=True)[:120],
                            url=urllib.parse.urljoin("https://prom.ua", a.get("href", "")), source="Prom/Bigl",
                            price=parse_price((pr or blk).get_text(" ")), text=blk.get_text(" ", strip=True)))
    return out

def page_price(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            o = d.get("offers") if isinstance(d, dict) else None
            o = o[0] if isinstance(o, list) and o else o
            if isinstance(o, dict) and o.get("price"):
                try:
                    return int(float(o["price"]))
                except ValueError:
                    pass
    return parse_price(soup.get_text(" "))

def src_web(q, pmax, page):
    out = []
    r = get("https://html.duckduckgo.com/html/", params={"q": f"купити {q}", "s": page * 30})
    if not r:
        return out
    for a in BeautifulSoup(r.text, "html.parser").select("a.result__a"):
        if time_left() <= 5:
            break
        link = a.get("href", "")
        m = re.search(r"uddg=([^&]+)", link)
        link = urllib.parse.unquote(m.group(1)) if m else link
        if not any(d in link for d in SHOPS):
            continue
        pg = get(link)
        if pg:
            soup = BeautifulSoup(pg.text, "html.parser")
            out.append(dict(title=a.get_text(" ", strip=True), url=link, price=page_price(soup), source="Веб-пошук",
                            text=a.get_text(" ", strip=True) + " " + soup.get_text(" ")[:3000]))
    return out

SOURCES = (src_olx, src_prom, src_web)

def run_search(s, seen):
    must = norm(s["q"]).split()
    bad = [norm(w) for w in s.get("exclude", "").split(",") if w.strip()]
    main, cart, checked = [], [], 0
    local = set()
    for page in range(MAX_PAGES):
        for src in SOURCES:
            if time_left() <= 5:          # не вилазимо за ліміт часу serverless-функції
                break
            try:
                items = src(s["q"], s["price"], page)
            except Exception as e:
                print("source error", src.__name__, e)
                continue
            for it in items:
                if it["url"] in seen or it["url"] in local or it["price"] is None:
                    continue
                local.add(it["url"]); checked += 1
                t = norm(it["text"])
                if not all(w in t for w in must) or any(b in t for b in bad):
                    continue
                it["icloud"] = icloud_status(it["text"])
                price_ok = it["price"] <= s["price"]
                ic_ok = s["icloud"] == "any" or it["icloud"] == s["icloud"]
                if price_ok and ic_ok:
                    main.append(it)
                elif it["price"] <= s["price"] * (1 + TOLERANCE):
                    it["reason"] = "трохи дорожче за ліміт" if not price_ok else "iCloud не збігається / не вказано"
                    cart.append(it)
    main.sort(key=lambda x: x["price"]); cart.sort(key=lambda x: x["price"])
    return main, cart, checked

# ================= стан (Redis REST, бо диск на Vercel не зберігається) =================
S = {}

def _fresh():
    return {"owner": None, "next_id": 1, "searches": {}, "found": [], "cart": [], "step": None, "last_update": -1}

def _kv(*cmd):
    r = requests.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=list(cmd), timeout=10)
    r.raise_for_status()
    return r.json().get("result")

def load_state():
    global S
    raw = None
    try:
        if KV_URL and KV_TOKEN:
            raw = _kv("GET", STATE_KEY)
        elif os.path.exists(LOCAL_STATE):
            raw = open(LOCAL_STATE, encoding="utf-8").read()
    except Exception as e:
        print("state load error:", e)
        raise
    S = {**_fresh(), **(json.loads(raw) if raw else {})}
    if OWNER_ID and S["owner"] is None:
        S["owner"] = int(OWNER_ID)

def save():
    for k in ("found", "cart"):
        S[k] = S[k][-100:]
    for s in S["searches"].values():
        s["seen"] = s["seen"][-500:]
    raw = json.dumps(S, ensure_ascii=False)
    if KV_URL and KV_TOKEN:
        _kv("SET", STATE_KEY, raw)
    else:
        open(LOCAL_STATE, "w", encoding="utf-8").write(raw)

# ================= Telegram =================
def tg(method, **p):
    if not BOT_TOKEN:
        print("BOT_TOKEN не заданий")
        return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=p, timeout=15)
        data = r.json()
        if not data.get("ok"):
            print(f"telegram {method}: {data.get('error_code')} {data.get('description')}")
        return data
    except Exception as e:
        print("telegram error:", str(e).replace(BOT_TOKEN, "***"))
        return {}

def send(text, kb=None):
    if S.get("owner") is None:
        return
    p = dict(chat_id=S["owner"], text=text, parse_mode="HTML", disable_web_page_preview=True)
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    tg("sendMessage", **p)

def menu(text="Що робимо?"):
    send(text, [[{"text": "➕ Новий пошук", "callback_data": "new"}],
                [{"text": "📋 Мої пошуки", "callback_data": "list"}],
                [{"text": "✅ Знайдене", "callback_data": "found"}, {"text": "🛒 Кошик", "callback_data": "cart"}]])

ICL = {"clean": "✅ без блокування", "locked": "🔒 заблокований", "unknown": "❓ не вказано", "any": "будь-який"}

def send_item(it, kind):
    title = html.escape(it["title"][:120])
    extra = f"\n<i>{it['reason']}</i>" if it.get("reason") else ""
    head = "✅ Знайдено" if kind == "found" else "🛒 У кошик"
    send(f"{head}\n<b>{title}</b>\n💰 {it['price']} грн · {it['source']}\niCloud: {ICL[it['icloud']]}{extra}",
         [[{"text": "🔗 Відкрити товар", "url": it["url"]}]])

def show_list(kind):
    items = S[kind]
    if not items:
        return menu("Поки порожньо.")
    lines = [f"{i+1}. <a href=\"{html.escape(x['url'])}\">{html.escape(x['title'][:70])}</a> - {x['price']} грн ({ICL[x['icloud']]})"
             for i, x in enumerate(items[-30:])]
    send("\n".join(lines), [[{"text": "🧹 Очистити", "callback_data": "clr:" + kind}]])

# ================= сканер =================
def scan(s, verbose=False):
    main, cart, checked = run_search(s, set(s["seen"]))
    sent = 0
    for kind, items, cap in (("found", main, 10), ("cart", cart, 5)):
        for it in items[:cap]:
            send_item(it, kind)
            S[kind].append({k: it[k] for k in ("title", "url", "price", "icloud", "source")})
            s["seen"].append(it["url"]); sent += 1
    if verbose and not sent:
        send(f"🔍 Перший прохід по «{html.escape(s['q'])}»: перевірено {checked}, підходящого поки немає. "
             f"Продовжу шукати за розкладом.")

def create_search(draft):
    sid = str(S["next_id"]); S["next_id"] += 1
    s = dict(id=sid, q=draft["q"], price=draft["price"], icloud=draft["icloud"],
             exclude=draft.get("exclude", ""), active=True, seen=[])
    S["searches"][sid] = s
    send(f"🚀 Пошук запущено: <b>{html.escape(s['q'])}</b> до {s['price']} грн, iCloud: {ICL[s['icloud']]}")
    save()                       # зберігаємо до першого проходу, щоб пошук не загубився при таймауті
    scan(s, True)

# ================= обробка повідомлень (діалог зберігається в S["step"]) =================
def on_callback(d):
    st = S.get("step")
    if d == "new":
        S["step"] = {"step": "q", "draft": {}}
        send("Що шукаємо? Напиши назву, наприклад: <i>плата iPhone XR</i>")
    elif d.startswith("ic:"):
        if not st:
            return menu("Діалог застарів, почнемо спочатку.")
        st["draft"]["icloud"] = d[3:]; st["step"] = "excl"
        send("Слова-виключення (через кому), наприклад: <i>чохол, скло, корпус</i>",
             [[{"text": "Пропустити", "callback_data": "skip"}]])
    elif d == "skip":
        if not st:
            return menu("Діалог застарів, почнемо спочатку.")
        S["step"] = None
        create_search(st["draft"])
    elif d == "list":
        if not S["searches"]:
            return menu("Пошуків ще немає.")
        for s in S["searches"].values():
            state = "▶️ працює" if s["active"] else "⏸ на паузі"
            send(f"<b>{html.escape(s['q'])}</b> до {s['price']} грн · {ICL[s['icloud']]} · {state}",
                 [[{"text": "⏸/▶️", "callback_data": "tog:" + s["id"]}, {"text": "🗑 Видалити", "callback_data": "del:" + s["id"]}]])
    elif d.startswith("tog:") and d[4:] in S["searches"]:
        s = S["searches"][d[4:]]; s["active"] = not s["active"]
        send("▶️ Запущено" if s["active"] else "⏸ Пауза")
    elif d.startswith("del:"):
        S["searches"].pop(d[4:], None); send("🗑 Видалено")
    elif d in ("found", "cart"):
        show_list(d)
    elif d.startswith("clr:") and d[4:] in ("found", "cart"):
        S[d[4:]] = []; send("🧹 Очищено")

def on_text(t):
    st = S.get("step")
    if t in ("/start", "/menu") or not st:
        S["step"] = None
        return menu("Привіт! Я шукаю деталі на OLX, Промі та в інтернеті й шлю прямі посилання.")
    if st["step"] == "q":
        st["draft"]["q"] = t.strip(); st["step"] = "price"
        send("Максимальна ціна в грн? (тільки число)")
    elif st["step"] == "price":
        digits = re.sub(r"\D", "", t)
        if not digits:
            return send("Напиши число, наприклад 1700")
        st["draft"]["price"] = int(digits); st["step"] = "ic"
        send("Який iCloud потрібен?", [[{"text": "✅ Без блокування", "callback_data": "ic:clean"}],
                                       [{"text": "🔒 З блокуванням", "callback_data": "ic:locked"}],
                                       [{"text": "Не важливо", "callback_data": "ic:any"}]])
    elif st["step"] == "excl":
        st["draft"]["exclude"] = t.strip()
        S["step"] = None
        create_search(st["draft"])
    else:
        menu()

def handle_update(u):
    if "callback_query" in u:
        cq = u["callback_query"]; chat = cq["message"]["chat"]["id"]
        if S["owner"] is None or chat != S["owner"]:
            return
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        on_callback(cq["data"])
    elif "message" in u and "text" in u["message"]:
        chat = u["message"]["chat"]["id"]
        if S["owner"] is None:
            S["owner"] = chat
        if chat == S["owner"]:
            on_text(u["message"]["text"])

# ================= HTTP-маршрути =================
def webhook_secret():
    return hashlib.sha256(("wh:" + BOT_TOKEN).encode()).hexdigest()[:32]

def authorized():
    if not CRON_SECRET:
        return False
    return (request.headers.get("Authorization") == f"Bearer {CRON_SECRET}"
            or request.args.get("key") == CRON_SECRET)

@app.post("/api/webhook")
def webhook():
    if not BOT_TOKEN:
        return "BOT_TOKEN is not set", 500
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != webhook_secret():
        return "forbidden", 403
    u = request.get_json(silent=True) or {}
    begin()
    try:
        load_state()
        uid = u.get("update_id", -1)
        if uid <= S["last_update"]:      # Telegram повторює доставку - не обробляємо двічі
            return "OK"
        S["last_update"] = uid
        save()
        handle_update(u)
        save()
    except Exception as e:
        print("webhook error:", str(e).replace(BOT_TOKEN, "***"))
    return "OK"                          # завжди 200, щоб Telegram не зациклював повтори

@app.get("/api/cron")
def cron():
    if not authorized():
        return "forbidden", 403
    begin()
    load_state()
    done = 0
    if S["owner"]:
        for s in list(S["searches"].values()):
            if not s["active"] or time_left() <= 8:
                continue
            try:
                scan(s); done += 1
            except Exception as e:
                print("scan error", str(e).replace(BOT_TOKEN, "***"))
    save()
    return f"OK scanned={done}"

@app.get("/api/setup")
def setup():
    if not authorized():
        return "forbidden", 403
    if not BOT_TOKEN:
        return "BOT_TOKEN is not set", 500
    url = f"https://{request.headers.get('X-Forwarded-Host') or request.host}/api/webhook"
    res = tg("setWebhook", url=url, secret_token=webhook_secret(),
             allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    return {"webhook": url, "telegram": res, "storage": "redis" if KV_URL and KV_TOKEN else "NO REDIS (state will be lost!)"}

@app.route("/")
@app.route("/<path:_>")
def ok(_=None):
    return "OK"
