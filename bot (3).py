import os
import discord
from discord.ext import tasks
import requests
import asyncio

intents = discord.Intents.default()
bot = discord.Client(intents=intents)

# Load environment variables
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID_QUICK = os.getenv("DISCORD_CHANNEL_ID_QUICK")
DISCORD_CHANNEL_ID_LONG = os.getenv("DISCORD_CHANNEL_ID_LONG")
DISCORD_CHANNEL_ID_BEST = os.getenv("DISCORD_CHANNEL_ID_BEST")
THEODDS_API_KEY = os.getenv("THEODDS_API_KEY")

# OddsAPI endpoint
API_URL = "https://api.the-odds-api.com/v4/sports/upcoming/odds"

async def fetch_odds():
    """Fetch odds from TheOddsAPI"""
    try:
        response = requests.get(
            API_URL,
            params={"apiKey": THEODDS_API_KEY, "regions": "au,us,uk", "markets": "h2h"},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
        else:
            print(f"⚠️ API error: {response.status_code} {response.text}")
            return []
    except Exception as e:
        print(f"⚠️ Request failed: {e}")
        return []

async def post_bets():
    """Post bets into Discord channels"""
    odds_data = await fetch_odds()
    if not odds_data:
        return

    for game in odds_data[:5]:  # limit for testing
        home = game["home_team"]
        away = game["away_team"]
        commence = game["commence_time"]

        # Format nicely in a code block
        message = f"""**Value Bet Found!**
🏟️ {home} vs {away}
🕒 {commence}
"""

        # Send to channels (choose based on rules)
        if DISCORD_CHANNEL_ID_QUICK:
            channel = bot.get_channel(int(DISCORD_CHANNEL_ID_QUICK))
            if channel:
                await channel.send(message)

        if DISCORD_CHANNEL_ID_LONG:
            channel = bot.get_channel(int(DISCORD_CHANNEL_ID_LONG))
            if channel:
                await channel.send(message)

        if DISCORD_CHANNEL_ID_BEST:
            channel = bot.get_channel(int(DISCORD_CHANNEL_ID_BEST))
            if channel:
                await channel.send(message)

@tasks.loop(seconds=30)
async def betting_loop():
    await post_bets()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    betting_loop.start()

bot.run(DISCORD_TOKEN)
