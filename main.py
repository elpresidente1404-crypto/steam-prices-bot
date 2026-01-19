import os
import re
import time
import asyncio
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

# =======================
# Secrets
# =======================
TOKEN = os.getenv("DISCORD_TOKEN")
PRICE_CHANNEL_ID_RAW = os.getenv("PRICE_CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in Replit Secrets.")
if not PRICE_CHANNEL_ID_RAW or not PRICE_CHANNEL_ID_RAW.isdigit():
    raise RuntimeError("Missing PRICE_CHANNEL_ID in Replit Secrets (must be numeric).")

PRICE_CHANNEL_ID = int(PRICE_CHANNEL_ID_RAW)

# =======================
# Config
# =======================
COOLDOWN_SECONDS = 5
MEMORY_TTL_SECONDS = 15 * 60
CHOICE_TTL_SECONDS = 60
MAX_SUGGESTIONS = 5

DEFAULT_ALL_CCS = ["TR", "UA", "SA", "BR", "RU", "IN", "AR", "US", "CN"]

# =======================
# Countries (Arabic + English + shorthand)
# =======================
COUNTRY_ALIASES = {
    # Saudi Arabia
    "sa": "SA", "ksa": "SA", "saudi": "SA", "saudi arabia": "SA", "saudiarabia": "SA",
    "السعودية": "SA", "سعودية": "SA",

    # Turkey
    "tr": "TR", "turkey": "TR", "turkiye": "TR", "türkiye": "TR", "تركيا": "TR",

    # Ukraine
    "ua": "UA", "ukraine": "UA", "اوكرانيا": "UA", "أوكرانيا": "UA",

    # Brazil
    "br": "BR", "brazil": "BR", "brasil": "BR", "البرازيل": "BR",

    # Russia
    "ru": "RU", "russia": "RU", "روسيا": "RU",

    # India
    "in": "IN", "india": "IN", "الهند": "IN",

    # Argentina
    "ar": "AR", "argentina": "AR", "الأرجنتين": "AR", "ارجنتين": "AR",

    # USA
    "us": "US", "usa": "US", "america": "US", "united states": "US",
    "امريكا": "US", "أمريكا": "US",

    # China
    "cn": "CN", "china": "CN", "الصين": "CN",
}

FLAGS = {
    "SA": "🇸🇦", "TR": "🇹🇷", "UA": "🇺🇦", "BR": "🇧🇷", "RU": "🇷🇺",
    "IN": "🇮🇳", "AR": "🇦🇷", "US": "🇺🇸", "CN": "🇨🇳",
}

# =======================
# USD conversion (approx, not live)
# =======================
USD_RATES = {
    "SAR": 0.266,
    "TRY": 0.031,
    "UAH": 0.027,
    "BRL": 0.20,
    "RUB": 0.011,
    "INR": 0.012,
    "ARS": 0.0011,
    "USD": 1.0,
    "CNY": 0.14,
}

def to_usd(amount: float, currency: str):
    rate = USD_RATES.get(currency)
    if not rate:
        return None
    return round(amount * rate, 2)

# =======================
# State
# =======================
last_user_time = {}   # user_id -> last request time
memory = {}           # user_id -> {"appid": int, "name": str, "t": float}
pending_choice = {}   # user_id -> {"items": [(appid, title)], "t": float}

# =======================
# Helpers
# =======================
def norm(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def is_all_token(tok: str) -> bool:
    tok = tok.strip().lower()
    return tok in {"all", "الكل", "كله", "كلهم"}

def should_cooldown(user_id: int) -> int:
    now = time.time()
    last = last_user_time.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return int(COOLDOWN_SECONDS - (now - last))
    last_user_time[user_id] = now
    return 0

def get_memory(user_id: int):
    m = memory.get(user_id)
    if not m:
        return None
    if time.time() - m["t"] > MEMORY_TTL_SECONDS:
        memory.pop(user_id, None)
        return None
    return m

def set_memory(user_id: int, appid: int, name: str):
    memory[user_id] = {"appid": appid, "name": name, "t": time.time()}

def get_pending_choice(user_id: int):
    p = pending_choice.get(user_id)
    if not p:
        return None
    if time.time() - p["t"] > CHOICE_TTL_SECONDS:
        pending_choice.pop(user_id, None)
        return None
    return p

def set_pending_choice(user_id: int, items):
    pending_choice[user_id] = {"items": items, "t": time.time()}

def format_countries_list():
    items = [
        ("السعودية", "SA", ["ksa", "sa", "saudi arabia", "السعودية"]),
        ("تركيا", "TR", ["tr", "turkey", "تركيا"]),
        ("أوكرانيا", "UA", ["ua", "ukraine", "أوكرانيا"]),
        ("البرازيل", "BR", ["br", "brazil", "البرازيل"]),
        ("روسيا", "RU", ["ru", "russia", "روسيا"]),
        ("الهند", "IN", ["in", "india", "الهند"]),
        ("الأرجنتين", "AR", ["ar", "argentina", "الأرجنتين"]),
        ("أمريكا", "US", ["us", "usa", "america", "أمريكا"]),
        ("الصين", "CN", ["cn", "china", "الصين"]),
    ]
    lines = []
    for name, cc, aliases in items:
        flag = FLAGS.get(cc, "")
        lines.append(f"{flag} **{name}** — `{cc}` — أمثلة: " + ", ".join(f"`{a}`" for a in aliases))
    lines.append("\n💡 `all` / `الكل` = عرض كل الدول الافتراضية.")
    return "\n".join(lines)

def parse_game_and_countries(text: str):
    """
    Returns: (game, ccs, used_all_only)
    used_all_only True when message is exactly "all/الكل" without game.
    """
    text = norm(text)
    if not text:
        return None, None, False

    words = text.split(" ")

    # Only "all/الكل"
    if len(words) == 1 and is_all_token(words[0]):
        return None, None, True

    # If ends with all -> game + DEFAULT_ALL_CCS
    if words and is_all_token(words[-1]):
        game = " ".join(words[:-1]).strip()
        if not game:
            return None, None, True
        return game, DEFAULT_ALL_CCS[:], False

    ccs = []
    i = len(words)

    while i > 0:
        matched = False
        for n in (3, 2, 1):
            if i - n < 0:
                continue
            chunk = " ".join(words[i - n:i])
            if chunk in COUNTRY_ALIASES:
                ccs.append(COUNTRY_ALIASES[chunk])
                i -= n
                matched = True
                break
        if not matched:
            break

    game = " ".join(words[:i]).strip()
    if not game:
        return None, None, False

    if not ccs:
        return game, [], False

    ccs = list(dict.fromkeys(reversed(ccs)))
    return game, ccs, False

# =======================
# Steam
# =======================
async def steam_search_suggestions_html(session: aiohttp.ClientSession, query: str, cc: str):
    url = "https://store.steampowered.com/search/"
    params = {"term": query, "cc": cc, "l": "english"}

    async with session.get(url, params=params, timeout=30) as r:
        html = await r.text()

    appids = re.findall(r'data-ds-appid="(\d+)"', html)
    titles = re.findall(r'<span class="title">(.*?)</span>', html)

    items = []
    for idx, aid in enumerate(appids[:MAX_SUGGESTIONS]):
        title = titles[idx] if idx < len(titles) else f"App {aid}"
        title = re.sub(r"<.*?>", "", title).strip()
        items.append((int(aid), title))

    if not items:
        m = re.search(r"/app/(\d+)/", html)
        if m:
            items.append((int(m.group(1)), query))

    return items

async def steam_get_price(session: aiohttp.ClientSession, appid: int, cc: str):
    url = "https://store.steampowered.com/api/appdetails"
    params = {"appids": str(appid), "cc": cc, "filters": "basic,price_overview"}

    async with session.get(url, params=params, timeout=30) as r:
        if r.status != 200:
            return None
        data = await r.json()

    entry = data.get(str(appid), {})
    if not entry.get("success"):
        return None

    info = entry.get("data", {})
    name = info.get("name") or f"App {appid}"
    po = info.get("price_overview")

    if not po:
        return {"name": name, "available": False}

    final = po.get("final", 0) / 100
    currency = po.get("currency")
    usd = to_usd(final, currency) if currency else None

    return {
        "name": name,
        "available": True,
        "final": final,
        "currency": currency,
        "usd": usd,
        "discount": po.get("discount_percent", 0),
    }

def build_embed(title_name: str, appid: int | None, results: list[tuple[str, dict | None]]):
    lines = []
    usd_values = []

    for cc, data in results:
        flag = FLAGS.get(cc, "")
        if not data:
            lines.append(f"{flag} **{cc}:** لا يمكن جلب البيانات الآن")
            continue

        if not data.get("available"):
            lines.append(f"{flag} **{cc}:** لا يوجد سعر (Free/غير متاحة/حزمة)")
            continue

        final = data["final"]
        currency = data["currency"]
        usd = data.get("usd")
        discount = data.get("discount", 0)

        if usd is not None:
            usd_values.append((cc, usd))

        if usd is None:
            lines.append(f"{flag} **{cc}:** {final} {currency} (خصم {discount}%)")
        else:
            lines.append(f"{flag} **{cc}:** {final} {currency} ≈ **{usd} USD** (خصم {discount}%)")

    extra = []
    if usd_values:
        usd_values_sorted = sorted(usd_values, key=lambda x: x[1])
        cheapest_cc, cheapest_usd = usd_values_sorted[0]
        extra.append(f"🔥 **الأرخص:** {FLAGS.get(cheapest_cc,'')} {cheapest_cc} (**{cheapest_usd} USD**)")

        sa = next((v for v in usd_values if v[0] == "SA"), None)
        if sa:
            diff = round(sa[1] - cheapest_usd, 2)
            extra.append(f"💸 **الفرق مع السعودية:** {diff} USD")
        else:
            max_cc, max_usd = usd_values_sorted[-1]
            diff = round(max_usd - cheapest_usd, 2)
            extra.append(f"💸 **الفرق (أغلى - أرخص):** {diff} USD")

    desc = ""
    if appid:
        desc += f"🔗 https://store.steampowered.com/app/{appid}/\n\n"
    desc += "\n".join(lines)
    if extra:
        desc += "\n\n" + "\n".join(extra)

    return discord.Embed(title=title_name, description=desc)

# =======================
# Discord bot
# =======================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.command(name="countries")
async def countries_cmd(ctx):
    if ctx.channel.id != PRICE_CHANNEL_ID:
        return
    embed = discord.Embed(
        title="الدول المدعومة (Countries Supported)",
        description=format_countries_list()
    )
    await ctx.reply(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.channel.id != PRICE_CHANNEL_ID:
        return

    text = message.content.strip()
    if not text or text.startswith(("!", "/")):
        return

    # anti-spam
    if should_cooldown(message.author.id) > 0:
        return

    # If pending choice and user sent a number
    p = get_pending_choice(message.author.id)
    if p and re.fullmatch(r"\d+", text.strip()):
        choice = int(text.strip())
        items = p["items"]
        if 1 <= choice <= len(items):
            appid, title = items[choice - 1]
            set_memory(message.author.id, appid, title)
            pending_choice.pop(message.author.id, None)
            await message.reply(
                f"✅ تم اختيار: **{title}**\n\n"
                f"✍️ اكتب `الكل` لعرض كل الدول\n"
                f"أو اكتب دول مثل:\n`turkey ukraine ksa`"
            )
        else:
            await message.reply(f"اكتب رقم من 1 إلى {len(items)}.")
        return

    game, ccs, all_only = parse_game_and_countries(text)

    # ✅ FIX 1: if user typed only all/الكل, use memory
    if all_only:
        mem = get_memory(message.author.id)
        if not mem:
            await message.reply("ما عندي لعبة سابقة لك 😅 اكتب اسم لعبة مثل: `resident evil` ثم اختر رقم.")
            return

        appid = mem["appid"]
        title_name = mem["name"]
        ccs = DEFAULT_ALL_CCS[:]

        await message.channel.typing()
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            results = []
            for cc in ccs:
                data = await steam_get_price(session, appid, cc)
                results.append((cc, data))

        await message.reply(embed=build_embed(title_name, appid, results))
        return

    # ✅ FIX 2: if user typed only a country (like "turkey") after choosing, use memory
    # Detect if message is countries-only
    def parse_as_countries_only(txt: str):
        w = norm(txt).split()
        i = len(w)
        found_any = False
        ccs_local = []
        while i > 0:
            matched = False
            for n in (3,2,1):
                if i-n < 0:
                    continue
                chunk = " ".join(w[i-n:i])
                if is_all_token(chunk):
                    return DEFAULT_ALL_CCS[:], True
                if chunk in COUNTRY_ALIASES:
                    ccs_local.append(COUNTRY_ALIASES[chunk])
                    i -= n
                    matched = True
                    found_any = True
                    break
            if not matched:
                return None, False
        if not found_any:
            return None, False
        ccs_local = list(dict.fromkeys(reversed(ccs_local)))
        return ccs_local, False

    ccs_only, _all = parse_as_countries_only(text)
    if ccs_only:
        mem = get_memory(message.author.id)
        if not mem:
            await message.reply("ما عندي لعبة سابقة لك 😅 اكتب اسم لعبة + بلد مثل: `elden ring turkey`")
            return

        appid = mem["appid"]
        title_name = mem["name"]
        ccs = ccs_only

        await message.channel.typing()
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            results = []
            for cc in ccs:
                data = await steam_get_price(session, appid, cc)
                results.append((cc, data))

        await message.reply(embed=build_embed(title_name, appid, results))
        return

    # If user provided a game but no countries -> show suggestions
    if game and not ccs:
        await message.channel.typing()
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            items = await steam_search_suggestions_html(session, game, "SA")

        if not items:
            await message.reply("❌ ما لقيت نتائج. جرّب تكتب الاسم بالإنجليزي وبشكل أوضح.")
            return

        set_pending_choice(message.author.id, items)
        lines = []
        for idx, (_, title) in enumerate(items, start=1):
            lines.append(f"**{idx})** {title}")

        embed = discord.Embed(
            title="اختر اللعبة (Choice)",
            description="اكتب رقم الاختيار فقط خلال 60 ثانية:\n\n" + "\n".join(lines)
        )
        await message.reply(embed=embed)
        return

    # If game + countries -> normal flow
    if not game or not ccs:
        await message.reply("اكتب مثل: `elden ring turkey` أو `forza horizon 5 all`")
        return

    await message.channel.typing()
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        items = await steam_search_suggestions_html(session, game, ccs[0])
        if not items:
            await message.reply("❌ ما لقيت اللعبة. جرّب تكتب الاسم بالإنجليزي وبشكل أوضح.")
            return

        appid, title_name = items[0]
        set_memory(message.author.id, appid, title_name)

        results = []
        for cc in ccs:
            data = await steam_get_price(session, appid, cc)
            results.append((cc, data))

    await message.reply(embed=build_embed(title_name, appid, results))

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
  

bot.run(TOKEN)
