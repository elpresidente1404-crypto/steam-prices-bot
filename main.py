import os
import time
import asyncio
import aiohttp
import discord
from discord.ext import commands
from discord.ui import View, Button

TOKEN = os.getenv("DISCORD_TOKEN")
PRICE_CHANNEL_ID = int(os.getenv("PRICE_CHANNEL_ID"))

COOLDOWN = 30
CACHE_TTL = 300

last_user = {}
cache = {}

FLAGS = {
    "TR":"🇹🇷","SA":"🇸🇦","US":"🇺🇸","AR":"🇦🇷",
    "IN":"🇮🇳","BR":"🇧🇷","UA":"🇺🇦","RU":"🇷🇺","CN":"🇨🇳"
}

COUNTRIES = ["TR","SA","US","AR","IN","BR","UA","RU","CN"]

RATES = {
    "USD":1.0,"TRY":0.031,"SAR":0.266,"ARS":0.0011,
    "INR":0.012,"BRL":0.20,"UAH":0.027,"RUB":0.011,"CNY":0.14
}

def to_usd(v,c):
    r = RATES.get(c)
    if not r:
        return None
    return round(v*r,2)

def cooldown(uid):
    now=time.time()
    last=last_user.get(uid,0)
    if now-last<COOLDOWN:
        return int(COOLDOWN-(now-last))
    last_user[uid]=now
    return 0

def cache_get(key):
    if key in cache:
        data,ts=cache[key]
        if time.time()-ts<CACHE_TTL:
            return data
        del cache[key]
    return None

def cache_set(key,value):
    cache[key]=(value,time.time())

# ---------------- Search Game ----------------
async def search_game(session,query):
    cached=cache_get("search_"+query)
    if cached:
        return cached

    url="https://store.steampowered.com/api/storesearch/"
    async with session.get(url,params={
        "term":query,
        "l":"english",
        "cc":"US"
    }) as r:
        data=await r.json()

    for item in data.get("items",[]):
        if item.get("type")=="app":
            cache_set("search_"+query,(item["id"],item["name"]))
            return item["id"],item["name"]

    return None,None

# ---------------- Get Editions ----------------
async def get_editions(session,appid):
    url="https://store.steampowered.com/api/appdetails"
    async with session.get(url,params={
        "appids":appid,
        "cc":"US",
        "l":"english"
    }) as r:
        data=await r.json()

    e=data.get(str(appid))
    if not e or not e.get("success"):
        return []

    d=e["data"]
    base_name=d.get("name")

    editions=[(appid,base_name,"app")]

    for group in d.get("package_groups",[]):
        for sub in group.get("subs",[]):
            name=sub.get("option_text","")
            subid=sub.get("packageid")

            if not subid:
                continue

            # إزالة السعر من النص
            if " - " in name:
                name=name.split(" - ")[0].strip()

            lower=name.lower()

            if any(k in lower for k in ["deluxe","premium","ultimate","standard"]):
                editions.append((subid,name,"package"))

    unique=list(dict.fromkeys(editions))
    return unique

# ---------------- Fetch Price ----------------
async def fetch_price(session,item_id,cc,item_type):

    if item_type=="app":
        url="https://store.steampowered.com/api/appdetails"
        params={
            "appids":item_id,
            "cc":cc,
            "filters":"price_overview"
        }
    else:
        url="https://store.steampowered.com/api/packagedetails"
        params={
            "packageids":item_id,
            "cc":cc
        }

    async with session.get(url,params=params) as r:
        data=await r.json()

    e=data.get(str(item_id))
    if not e or not e.get("success"):
        return None

    d=e.get("data")

    if item_type=="app":
        po=d.get("price_overview") if d else None
    else:
        po=d.get("price") if d else None

    if not po:
        return None

    final=po["final"]/100
    cur=po["currency"]
    usd=to_usd(final,cur)
    discount=po.get("discount_percent",0)

    if not usd:
        return None

    return (cc,usd,final,cur,discount)

# ---------------- Get Prices ----------------
async def get_prices(session,item_id,item_type):
    tasks=[
        fetch_price(session,item_id,cc,item_type)
        for cc in COUNTRIES
    ]
    results=await asyncio.gather(*tasks)

    pairs=[p for p in results if p]
    pairs.sort(key=lambda x:x[1])
    return pairs

# ---------------- Embed ----------------
def make_embed(title,prices):
    lines=[]
    cheapest=None

    for cc,usd,final,cur,discount in prices:
        flag=FLAGS.get(cc,"")
        disc=f" (-{discount}%)" if discount else ""
        lines.append(f"{flag} **{cc}** {final} {cur} → 🟢 {usd}$ {disc}")

        if not cheapest or usd<cheapest[1]:
            cheapest=(cc,usd)

    cheapest_line=""
    if cheapest:
        flag=FLAGS.get(cheapest[0],"")
        cheapest_line=f"\n\n🔥 **أرخص دولة:** {flag} {cheapest[0]} بسعر {cheapest[1]}$"

    desc="**💵 الأسعار مرتبة من الأرخص للأغلى:**\n\n"+"\n".join(lines)+cheapest_line

    return discord.Embed(
        title=f"💰 {title}",
        description=desc,
        color=0x1E88E5
    )

# ---------------- Discord ----------------
intents=discord.Intents.default()
intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)

class EditionView(View):
    def __init__(self,user_id,items):
        super().__init__(timeout=60)
        self.user_id=user_id

        for item_id,title,item_type in items:
            btn=Button(label=title[:80],style=discord.ButtonStyle.primary)
            btn.callback=self.make_callback(item_id,title,item_type)
            self.add_item(btn)

    def make_callback(self,item_id,title,item_type):
        async def callback(interaction:discord.Interaction):
            if interaction.user.id!=self.user_id:
                await interaction.response.send_message("❌ هذا مو لك",ephemeral=True)
                return

            await interaction.response.defer()

            async with aiohttp.ClientSession() as s:
                prices=await get_prices(s,item_id,item_type)

            if not prices:
                await interaction.followup.send("❌ لا يوجد أسعار متاحة لهذا الإصدار")
                return

            embed=make_embed(title,prices)
            await interaction.followup.send(embed=embed)

        return callback

@bot.event
async def on_message(msg):
    if msg.author.bot or msg.channel.id!=PRICE_CHANNEL_ID:
        return

    asyncio.create_task(auto_delete(msg,20))

    cd=cooldown(msg.author.id)
    if cd>0:
        m=await msg.reply(f"⏳ انتظر {cd} ثانية")
        asyncio.create_task(auto_delete(m,15))
        return

    async with aiohttp.ClientSession() as s:
        appid,title=await search_game(s,msg.content.strip())

        if not appid:
            m=await msg.reply("❌ ما لقيت لعبة")
            asyncio.create_task(auto_delete(m,15))
            return

        editions=await get_editions(s,appid)

    if not editions:
        m=await msg.reply("❌ لا يوجد إصدارات متاحة")
        asyncio.create_task(auto_delete(m,15))
        return

    view=EditionView(msg.author.id,editions)
    m=await msg.reply("اختر الإصدار:",view=view)
    asyncio.create_task(auto_delete(m,60))

async def auto_delete(message,seconds):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except:
        pass

bot.run(TOKEN)
