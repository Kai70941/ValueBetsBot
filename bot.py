import os
import discord
import aiohttp
import asyncio
from discord.ext import tasks, commands
from datetime import datetime

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

# -------------------------------
# Fetch odds from TheOddsAPI
# -------------------------------
async def fetch_odds():
    params = {
        "apiKey": THEODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,totals",
        "oddsFormat": "decimal"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_BASE}/upcoming/odds", params=params) as resp:
            if resp.status != 200:
                print("API error:", resp.status)
                return []
            return await resp.json()

# -------------------------------
# Format bet as embed card
# -------------------------------
def create_bet_embed(event, bookmaker, pick, price, consensus, implied, edge, bet_type):
    # Color + Title per bet type
    colors = {
        "quick": discord.Color.orange(),
        "long": discord.Color.blue(),
        "best": discord.Color.gold(),
    }
    titles = {
        "quick": "⏱ Quick Return Bet",
        "long": "📊 Longer Play Bet",
        "best": "⭐ Best Value Bet",
    }

    embed = discord.Embed(
        title=titles.get(bet_type, "Value Bet"),
        color=colors.get(bet_type, discord.Color.green())
    )

    teams = event.get("teams", ["Unknown", "Unknown"])
    commence_time = event.get("commence_time", "Unknown")
    sport = event.get("sport_title", "Unknown Sport")

    embed.add_field(name="Match", value=f"{teams[0]} vs {teams[1]}", inline=False)
    embed.add_field(name="Pick", value=f"{pick} @ {price}", inline=False)
    embed.add_field(name="Bookmaker", value=bookmaker, inline=True)
    embed.add_field(name="Consensus %", value=f"{consensus:.2f}%", inline=True)
    embed.add_field(name="Implied %", value=f"{implied:.2f}%", inline=True)
    embed.add_field(name="Edge", value=f"{edge:.2f}%", inline=True)
    embed.add_field(name="Time", value=commence_time, inline=False)

    # Example bankroll-based staking
    bankroll = 1000
    cons_stake = round(bankroll * 0.015, 2)
    aggr_stake = round(bankroll * 0.15, 2)

    cons_payout = round(cons_stake * float(price), 2)
    aggr_payout = round(aggr_stake * float(price), 2)

    cons_profit = round(cons_payout - cons_stake, 2)
    aggr_profit = round(aggr_payout - aggr_stake, 2)

    embed.add_field(
        name="💵 Conservative Stake",
        value=f"${cons_stake} → Payout: ${cons_payout} | Exp. Profit: ${cons_profit}",
        inline=False
    )
    embed.add_field(
        name="🔥 Aggressive Stake",
        value=f"${aggr_stake} → Payout: ${aggr_payout} | Exp. Profit: ${aggr_profit}",
        inline=False
    )

    return embed

# -------------------------------
# Decide bet type (classification)
# -------------------------------
def classify_bet(event, edge, days_to_game):
    if edge >= 8.0:  # Best bets = strong edge
        return "best"
    elif days_to_game <= 2:  # Quick return = soon games
        return "quick"
    else:  # Longer plays
        return "long"

# -------------------------------
# Send bet to correct channel
# -------------------------------
async def send_bet(event, bet_type):
    if bet_type == "quick":
        channel_id = CHANNEL_ID_QUICK
    elif bet_type == "best":
        channel_id = CHANNEL_ID_BEST
    else:
        channel_id = CHANNEL_ID_LONG

    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"⚠ Channel {channel_id} not found.")
        return

    bookmaker = event["bookmakers"][0]["title"] if event.get("bookmakers") else "Unknown"
    pick = "Sample Pick"
    price = 1.90
    consensus = 60.0
    implied = 52.0
    edge = consensus - implied

    try:
        game_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        days_to_game = (game_time - datetime.utcnow()).days
    except:
        days_to_game = 5

    bet_type = classify_bet(event, edge, days_to_game)
    embed = create_bet_embed(event, bookmaker, pick, price, consensus, implied, edge, bet_type)
    await channel.send(embed=embed)

# -------------------------------
# Background task
# -------------------------------
@tasks.loop(seconds=30)
async def fetch_and_post():
    events = await fetch_odds()
    if not events:
        return

    for event in events[:10]:  # Limit for testing
        bookmaker = event["bookmakers"][0]["title"] if event.get("bookmakers") else "Unknown"
        pick = "Sample Pick"
        price = 1.90
        consensus = 60.0
        implied = 52.0
        edge = consensus - implied

        try:
            game_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            days_to_game = (game_time - datetime.utcnow()).days
        except:
            days_to_game = 5

        bet_type = classify_bet(event, edge, days_to_game)
        await send_bet(event, bet_type)

# -------------------------------
# Bot events
# -------------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    fetch_and_post.start()

bot.run(TOKEN)
