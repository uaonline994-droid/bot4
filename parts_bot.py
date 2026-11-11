"""
Персональний Telegram-бот для пошуку деталей (OLX, Пром/Bigl, веб-пошук).
Встанови:  pip install requests beautifulsoup4 flask
Токен (від @BotFather) задай ОДНИМ зі способів:
  1) змінна середовища BOT_TOKEN
  2) файл bot_token.txt поруч зі скриптом
Необов'язково: TG_PROXY=http://host:port (або socks5h://...) - якщо потрібен свій проксі.
Запуск:  python parts_bot.py
Перший, хто напише боту /start, стає власником, усі інші ігноруються.
"""
import re, json, time, threading, html, urllib.parse, os
import requests
from bs4 import BeautifulSoup
from flask import Flask

def _load_token():
    t = os.getenv("BOT_TOKEN", "9241435587:AAGZjuoRu1u-czs-Xlatg8CgIguGpXhzeog").strip()
    if not t and os.path.exists("bot_token.txt"):
        t = open("bot_token.txt", encoding="utf-8").read().strip()
    return t

BOT_TOKEN = _load_token()
if not BOT_TOKEN:
    raise RuntimeError("Задай BOT_TOKEN (змінна середовища) або створи файл bot_token.txt з токеном.")
TG_PROXY = os.getenv("TG_PROXY", "").strip()
CHECK_EVERY_MIN = 15      # як часто перевіряти активні пошуки
TOLERANCE = 0.15          # "майже підходить": до +15% над ціною йде в кошик
MAX_PAGES = 3             # сторінок на джерело за один прохід
STATE_FILE = "state.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "uk-UA,uk;q=0.9"}
SHOPS = ("prom.ua", "bigl.ua", "olx.ua", "rozetka.com.ua", "aliexpress", "besplatka.ua", "hotline.ua", "allo.ua")

# ================= мережа: стійка до зламаного системного проксі =================
_net = {"mode": None}  # режим, який востаннє спрацював: "custom" | "env" | "direct"

def _modes():
    order = (["custom"] if TG_PROXY else []) + ["env", "direct"]
    if _net["mode"] in order:                       # спочатку пробуємо робочий режим
        order.remove(_net["mode"])
        order.insert(0, _net["mode"])
    return order

def _mask(e):
    return str(e).replace(BOT_TOKEN, "***")

def http(method, url, **kw):
    """HTTP-запит з автоматичним фолбеком: свій проксі -> системний проксі -> напряму."""
    kw.setdefault("timeout", 15)
    last = None
    for mode in _modes():
        s = requests.Session()
        s.trust_env = (mode == "env")
        if mode == "custom":
            s.proxies = {"http": TG_PROXY, "https": TG_PROXY}
        try:
            r = s.request(method, url, **kw)
            if _net["mode"] != mode:
                print(f"[net] режим з'єднання: {mode}")
                _net["mode"] = mode
            return r
        except requests.RequestException as e:
            last = e
        finally:
            s.close()
    raise last

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
    try:
        r = http("get", url, headers=UA, **kw)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None

def parse_price(text):
    m = re.search(r"(\d[\d\s\u00a0]{1,9})\s*(грн|₴|uah)", text.lower())
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

# ================= стан =================
LOCK = threading.Lock()
if os.path.exists(STATE_FILE):
    S = json.load(open(STATE_FILE, encoding="utf-8"))
else:
    S = {"owner": None, "next_id": 1, "searches": {}, "found": [], "cart": []}
step = {}  # поточний діалог: {"step": ..., "draft": {...}}

def save():
    with LOCK:
        json.dump(S, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)

# ================= Telegram =================
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

def tg(method, **p):
    t = 45 if method == "getUpdates" else 20
    try:
        r = http("post", API + method, json=p, timeout=t)
        try:
            data = r.json()
        except ValueError:
            print(f"telegram {method}: HTTP {r.status_code}, не JSON")
            return {}
        if not data.get("ok"):
            print(f"telegram {method}: {data.get('error_code')} {data.get('description')}")
        return data
    except Exception as e:
        print("telegram error:", _mask(e))
        return {}

def send(text, kb=None):
    if S["owner"] is None:
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
    with LOCK:
        seen = set(s["seen"])
    main, cart, checked = run_search(s, seen)
    sent = 0
    for kind, items, cap in (("found", main, 10), ("cart", cart, 5)):
        for it in items[:cap]:
            send_item(it, kind)
            S[kind].append({k: it[k] for k in ("title", "url", "price", "icloud", "source")})
            s["seen"].append(it["url"]); sent += 1
    save()
    if verbose and not sent:
        send(f"🔍 Перший прохід по «{html.escape(s['q'])}»: перевірено {checked}, підходящого поки немає. Продовжу шукати кожні {CHECK_EVERY_MIN} хв.")

def scanner():
    while True:
        if S["owner"]:
            for s in list(S["searches"].values()):
                if s["active"]:
                    try:
                        scan(s)
                    except Exception as e:
                        print("scan error", e)
        time.sleep(CHECK_EVERY_MIN * 60)

def create_search(draft):
    sid = str(S["next_id"]); S["next_id"] += 1
    s = dict(id=sid, q=draft["q"], price=draft["price"], icloud=draft["icloud"],
             exclude=draft.get("exclude", ""), active=True, seen=[])
    S["searches"][sid] = s; save()
    send(f"🚀 Пошук запущено: <b>{html.escape(s['q'])}</b> до {s['price']} грн, iCloud: {ICL[s['icloud']]}")
    threading.Thread(target=lambda: scan(s, True), daemon=True).start()

# ================= обробка повідомлень =================
def on_callback(d):
    if d == "new":
        step["s"] = {"step": "q", "draft": {}}
        send("Що шукаємо? Напиши назву, наприклад: <i>плата iPhone XR</i>")
    elif d.startswith("ic:"):
        if "s" not in step:
            return menu("Діалог застарів, почнемо спочатку.")
        step["s"]["draft"]["icloud"] = d[3:]; step["s"]["step"] = "excl"
        send("Слова-виключення (через кому), наприклад: <i>чохол, скло, корпус</i>",
             [[{"text": "Пропустити", "callback_data": "skip"}]])
    elif d == "skip":
        if "s" not in step:
            return menu("Діалог застарів, почнемо спочатку.")
        create_search(step.pop("s")["draft"])
    elif d == "list":
        if not S["searches"]:
            return menu("Пошуків ще немає.")
        for s in S["searches"].values():
            st = "▶️ працює" if s["active"] else "⏸ на паузі"
            send(f"<b>{html.escape(s['q'])}</b> до {s['price']} грн · {ICL[s['icloud']]} · {st}",
                 [[{"text": "⏸/▶️", "callback_data": "tog:" + s["id"]}, {"text": "🗑 Видалити", "callback_data": "del:" + s["id"]}]])
    elif d.startswith("tog:") and d[4:] in S["searches"]:
        s = S["searches"][d[4:]]; s["active"] = not s["active"]; save()
        send("▶️ Запущено" if s["active"] else "⏸ Пауза")
    elif d.startswith("del:"):
        S["searches"].pop(d[4:], None); save(); send("🗑 Видалено")
    elif d in ("found", "cart"):
        show_list(d)
    elif d.startswith("clr:"):
        S[d[4:]] = []; save(); send("🧹 Очищено")

def on_text(t):
    st = step.get("s")
    if t in ("/start", "/menu") or not st:
        step.pop("s", None)
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
        create_search(step.pop("s")["draft"])
    else:
        menu()

# ================= мінімальний веб-сервер (для health-check хостингу) =================
web = Flask(__name__)

@web.route("/")
@web.route("/<path:_>")
def ok(_=None):
    return "OK"

def run_web():
    port = int(os.getenv("PORT", "10000"))
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    web.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

def main():
    threading.Thread(target=run_web, daemon=True).start()
    # скидаємо можливий вебхук, інакше getUpdates не отримує повідомлень
    tg("deleteWebhook", drop_pending_updates=False)
    me = tg("getMe")
    if not me.get("ok"):
        print("⚠ Не вдалося підключитись до Telegram. Перевір токен/мережу/проксі. Буду пробувати знову...")
    else:
        print(f"Бот @{me['result'].get('username')} запущено")
    threading.Thread(target=scanner, daemon=True).start()
    offset = None
    fails = 0
    while True:
        res = tg("getUpdates", offset=offset, timeout=30)
        if not res.get("ok"):
            fails += 1
            time.sleep(min(60, 3 * fails))   # backoff при помилках мережі/409/401
            continue
        fails = 0
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    cq = u["callback_query"]; chat = cq["message"]["chat"]["id"]
                    if S["owner"] is None or chat != S["owner"]:
                        continue
                    tg("answerCallbackQuery", callback_query_id=cq["id"])
                    on_callback(cq["data"])
                elif "message" in u and "text" in u["message"]:
                    chat = u["message"]["chat"]["id"]
                    if S["owner"] is None:
                        S["owner"] = chat; save()
                    if chat == S["owner"]:
                        on_text(u["message"]["text"])
            except Exception as e:
                print("handler error", _mask(e))

if __name__ == "__main__":
    main()
