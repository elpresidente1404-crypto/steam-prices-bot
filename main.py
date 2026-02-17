import os
import re
import time
import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks

# =======================
# Secrets
# =======================
TOKEN = os.getenv("DISCORD_TOKEN")
PRICE_CHANNEL_ID_RAW = os.getenv("PRICE_CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in Secrets.")
if not PRICE_CHANNEL_ID_RAW or not PRICE_CHANNEL_ID_RAW.isdigit():
    raise RuntimeError("Missing PRICE_CHANNEL_ID in Secrets.")

PRICE_CHANNEL_ID = int(PRICE_CHANNEL_ID_RAW)

# =======================
# Config
# =======================
COOLDOWN_SECONDS = 5
MEMORY_TTL_SECONDS = 15 * 60
CHOICE_TTL_SECONDS = 60
MAX_SUGGESTIONS = 5
EMBED_COLOR = 0x2ecc71

DEFAULT_ALL_CCS = ["TR", "UA", "SA", "BR", "RU", "IN", "AR", "US", "CN"]

# =======================
# Countries
# =======================
COUNTRY_ALIASES = {
    "sa": "SA", "ksa": "SA", "saudi": "SA", "السعودية": "SA",
    "tr": "TR", "turkey": "TR", "تركيا": "TR",
    "ua": "UA", "ukraine": "UA", "أوكرانيا": "UA",
    "br": "BR", "brazil": "BR", "البرازيل": "BR",
    "ru": "RU", "russia": "RU", "روسيا": "RU",
    "in": "IN", "india": "IN", "الهند": "IN",
    "ar": "AR", "argentina": "AR", "الأرجنتين": "AR",
    "us": "US", "usa": "US", "america": "US", "أمريكا": "US",
    "cn": "CN", "china": "CN", "الصين": "CN",
}

FLAGS = {
    "SA": "🇸🇦","TR": "🇹🇷","UA": "🇺🇦","BR": "🇧🇷","RU": "🇷🇺",
    "IN": "🇮🇳","AR": "🇦🇷","US": "🇺🇸","CN": "🇨🇳",
}

USD_RATES = {
    "SAR": 0.266,"TRY": 0.031,"UAH": 0.027,"BRL": 0.20,
    "RUB": 0.011,"INR": 0.012,"ARS": 0.0011,"USD": 1.0,"CNY": 0.14,
}

# =======================
# State
# =======================
last_user_time = {}
memory = {}
pending_choice = {}

# =======================
# Helpers
# =======================
def norm(s): return re.sub(r"\s+"," ",s.strip().lower())

def to_usd(a,c):
    r = USD_RATES.get(c)
    return round(a*r,2) if r else None

def should_cooldown(uid):
    now=time.time()
    last=last_user_time.get(uid,0)
    if now-last<COOLDOWN_SECONDS:
        return int(COOLDOWN_SECONDS-(now-last))
    last_user_time[uid]=now
    return 0

def get_memory(uid):
    m=memory.get(uid)
    if m and time.time()-m["t"]<MEMORY_TTL_SECONDS: return m
    memory.pop(uid,None); return None

def set_memory(uid,appid,name):
    memory[uid]={"appid":appid,"name":name,"t":time.time()}

def set_pending(uid,items):
    pending_choice[uid]={"items":items,"t":time.time()}

def get_pending(uid):
    p=pending_choice.get(uid)
    if p and time.time()-p["t"]<CHOICE_TTL_SECONDS: return p
    pending_choice.pop(uid,None); return None

# =======================
# Cleaning Task
# =======================
def cleanup_dict(d,ttl):
    now=time.time()
    for k,v in list(d.items()):
        if now-v["t"]>ttl: d.pop(k,None)

# =======================
# Steam
# =======================
async def safe_get(session,url,**kw):
    try:
        async with session.get(url,timeout=20,**kw) as r:
            if r.status!=200: return None
            return await r.text()
    except:
        return None

async def steam_search(session,q):
    html=await safe_get(session,"https://store.steampowered.com/search/",params={"term":q})
    if not html: return []
    appids=re.findall(r'data-ds-appid="(\d+)"',html)
    titles=re.findall(r'<span class="title">(.*?)</span>',html)
    items=[]
    for i,a in enumerate(appids[:MAX_SUGGESTIONS]):
        t=titles[i] if i<len(titles) else q
        items.append((int(a),re.sub("<.*?>","",t)))
    return items

async def steam_price(session,appid,cc):
    try:
        async with session.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids":appid,"cc":cc,"filters":"price_overview"},
            timeout=20) as r:
            if r.status!=200: return None
            data=await r.json()
    except:
        return None

    entry=data.get(str(appid),{})
    if not entry.get("success"): return None
    po=entry["data"].get("price_overview")
    if not po: return {"available":False}

    final=po["final"]/100
    cur=po["currency"]
    return {
        "available":True,
        "final":final,
        "currency":cur,
        "usd":to_usd(final,cur),
        "discount":po.get("discount_percent",0)
    }

# =======================
# Embed
# =======================
def build_embed(name,appid,results):
    lines=[]
    for cc,data in results:
        f=FLAGS.get(cc,"")
        if not data:
            lines.append(f"{f} {cc}: خطأ")
            continue
        if not data["available"]:
            lines.append(f"{f} {cc}: لا يوجد سعر")
            continue
        usd=f'≈ {data["usd"]}$' if data["usd"] else ""
        lines.append(f'{f} {cc}: {data["final"]} {data["currency"]} {usd}')

    return discord.Embed(
        title=name,
        description="\n".join(lines),
        color=EMBED_COLOR
    )

# =======================
# Discord
# =======================
intents=discord.Intents.default()
intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)

@tasks.loop(minutes=10)
async def cleaner():
    cleanup_dict(memory,MEMORY_TTL_SECONDS)
    cleanup_dict(pending_choice,CHOICE_TTL_SECONDS)

@bot.event
async def on_ready():
    cleaner.start()
    print("Bot Ready")

@bot.event
async def on_message(msg):
    if msg.author.bot: return
    await bot.process_commands(msg)
    if msg.channel.id!=PRICE_CHANNEL_ID: return

    cd=should_cooldown(msg.author.id)
    if cd>0:
        await msg.reply(f"استنى {cd} ثواني 😅")
        return

    text=norm(msg.content)
    if not text: return

    await msg.channel.typing()
    async with aiohttp.ClientSession(headers={"User-Agent":"Mozilla"}) as s:
        items=await steam_search(s,text)
        if not items:
            await msg.reply("ما لقيت شي ❌")
            return

        appid,title=items[0]
        set_memory(msg.author.id,appid,title)

        results=[]
        for cc in ["TR","SA"]:
            data=await steam_price(s,appid,cc)
            results.append((cc,data))

        await msg.reply(embed=build_embed(title,appid,res_
