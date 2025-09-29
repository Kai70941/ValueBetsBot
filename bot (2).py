import os
import discord
from discord.ext import tasks, commands
import aiohttp
import asyncio
from datetime import datetime, timedelta

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID_QUICK = os.getenv("DISCORD_CHANNEL_ID_QUICK")
CHANNEL_ID_LONG = os.getenv("DISCORD_CHANNEL_ID_LONG")
CHANNEL_ID_BEST = os.getenv("DISCORD_CHANNEL_ID_BEST")
ODDS_API_KEY = os.getenv("THEODDS_API_KEY")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

BASE_URL = "https://api.the-odds-api.com/v4/sports/upcoming/odds"

# Helper to format bet messages safely
def format_bet_message(bet_type, team1, team2, bookmaker, odds, stake, est_return, start_time):
    return (
        f"📢 **{bet_type} Bet Alert!**\n"
        f"🏆 **{team1} vs {team2}**\n"
        f"📅 Start: {start_time}\n"
        f"💰 Bookmaker: {bookmaker}\n"
        f"📊 Odds: {odds}\n"
        f"💵 Stake: ${stake:.2f}\n"
        f"📈 Estimated Return: ${est_return:.2f}\n"
    )

async def fetch_odds():
    url = f"{BASE_URL}?apiKey={ODDS_API_KEY}&regions=au,us,uk&markets=h2h&oddsFormat=decimal"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"Error fetching odds: {resp.status}")
                return []
            return await resp.json()

@tasks.loop(seconds=30)
async def fetch_and_post_bets():
    print("🔄 Fetching bets...")
    events = await fetch_odds()
    if not events:
        return

    for event in events:
        try:
            team1, team2 = event["home_team"], event["away_team"]
            start_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            if start_time > datetime.utcnow() + timedelta(days=150):
                continue

            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        odds = outcome["price"]
                        team = outcome["name"]

                        # Example bankroll management
                        bankroll = 1000
                        stake_safe = bankroll * 0.015  # 1.5%
                        stake_aggressive = bankroll * 0.03  # 3%

                        est_return_safe = stake_safe * odds
                        est_return_aggressive = stake_aggressive * odds

                        # Channels
                        quick_channel = bot.get_channel(int(CHANNEL_ID_QUICK))
                        long_channel = bot.get_channel(int(CHANNEL_ID_LONG))
                        best_channel = bot.get_channel(int(CHANNEL_ID_BEST))

                        # Quick return = matches in 48h
                        if start_time < datetime.utcnow() + timedelta(hours=48):
                            msg = format_bet_message(
                                "Quick Return",
                                team1, team2, bookmaker["title"], odds,
                                stake_safe, est_return_safe, start_time
                            )
                            if quick_channel:
                                await quick_channel.send(f"```{msg}```")

                        # Longer plays = up to 150 days
                        else:
                            msg = format_bet_message(
                                "Longer Play",
                                team1, team2, bookmaker["title"], odds,
                                stake_aggressive, est_return_aggressive, start_time
                            )
                            if long_channel:
                                await long_channel.send(f"```{msg}```")

                        # Best bet = highlight highest odds
                        if odds >= 2.5:
                            msg = format_bet_message(
                                "⭐ Best Bet",
                                team1, team2, bookmaker["title"], odds,
                                stake_safe, est_return_safe, start_time
                            )
                            if best_channel:
                                await best_channel.send(f"```{msg}```")

        except Exception as e:
            print(f"Error processing event: {e}")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    fetch_and_post_bets.start()

bot.run(TOKEN)
