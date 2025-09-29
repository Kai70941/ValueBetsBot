import os
import discord
from discord.ext import tasks, commands
import aiohttp
import asyncio
from datetime import datetime, timedelta

# Load environment variables
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
CHANNEL_ID_LONG = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
CHANNEL_ID_BEST = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
THEODDS_API_KEY = os.getenv("THEODDS_API_KEY")

# Bot setup
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

API_BASE = "https://api.the-odds-api.com/v4/sports"

# Fetch odds from TheOddsAPI
async def fetch_odds():
    params = {
        "apiKey": THEODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h",
        "oddsFormat": "decimal"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_BASE}/upcoming/odds", params=params) as resp:
            if resp.status != 200:
                return []
            return await resp.json()

# Format bets into a Discord message
def format_bet(event, bookmaker, quick=False, best=False):
    teams = event.get("teams", ["Team A", "Team B"])
    home, away = teams if len(teams) == 2 else ("Unknown", "Unknown")
    commence_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
    odds = bookmaker["markets"][0]["outcomes"]
    odds_text = " vs ".join([f"{o['name']}: {o['price']}" for o in odds])

    labels = []
    if quick:
        labels.append("⏱️ Quick Return")
    if best:
        labels.append("⭐ Best Bet")

    labels_text = " ".join(labels) if labels else "🟢 Value Bet"

    return (
        f"```
"
        f"{labels_text}
"
        f"Match: {home} vs {away}
"
        f"Bookmaker: {bookmaker['title']}
"
        f"Start: {commence_time.strftime('%Y-%m-%d %H:%M:%S')}
"
        f"Odds: {odds_text}
"
        f"Suggested Stake: $15
"
        f"Estimated Return: ${(15 * max(o['price'] for o in odds)):.2f}
"
        f"```"
    )

# Background task
@tasks.loop(seconds=30)
async def post_bets():
    await bot.wait_until_ready()

    events = await fetch_odds()
    if not events:
        for cid in [CHANNEL_ID_QUICK, CHANNEL_ID_LONG, CHANNEL_ID_BEST]:
            channel = bot.get_channel(cid)
            if channel:
                await channel.send("```No bets right now.```")
        return

    for event in events:
        commence_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        days_to_match = (commence_time - datetime.utcnow()).days

        for bookmaker in event.get("bookmakers", []):
            # Quick return (within 2 days)
            if days_to_match <= 2:
                channel = bot.get_channel(CHANNEL_ID_QUICK)
                if channel:
                    await channel.send(format_bet(event, bookmaker, quick=True))

            # Longer plays (within 150 days)
            if days_to_match <= 150:
                channel = bot.get_channel(CHANNEL_ID_LONG)
                if channel:
                    await channel.send(format_bet(event, bookmaker))

            # Best bets (placeholder: odds above 2.0)
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome["price"] >= 2.0:
                        channel = bot.get_channel(CHANNEL_ID_BEST)
                        if channel:
                            await channel.send(format_bet(event, bookmaker, best=True))
                        break

# Bot ready
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    if not post_bets.is_running():
        post_bets.start()

# Run bot
bot.run(TOKEN)
