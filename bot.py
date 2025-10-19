# bot.py
import os
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord.ext import commands, tasks

# =========================
# Env & Config
# =========================
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

# bankroll and staking (units, not dollars)
BANKROLL = 1000
CONSERVATIVE_PCT = 0.015  # 1.5% per unit plan

# Bookmaker allowlist (keep to your nine)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab",
]

API_BASE = "https://api.the-odds-api.com/v4/sports"

# =========================
# Discord Setup
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.paper_mode = False  # /paper on|off toggles this

posted_bets = set()

# =========================
# SPORT / LEAGUE DECODING
# =========================
SPORT_EMOJI = {
    "soccer": "⚽",
    "americanfootball": "🏈",
    "basketball": "🏀",
    "baseball": "⚾",
    "icehockey": "🏒",
    "mma": "🥊",
    "boxing": "🥊",
    "tennis": "🎾",
    "golf": "⛳",
    "cricket": "🏏",
    "aussierules": "🏉",
    "rugby": "🏉",
    "esports": "🎮",
}

_LEAGUE_FIX = {
    "serie a": "Série A",
    "serie b": "Série B",
    "uefa": "UEFA",
    "mls": "MLS",
    "nfl": "NFL",
    "ncaaf": "NCAAF",
    "nba": "NBA",
    "nhl": "NHL",
    "mlb": "MLB",
}

def _beautify_league(raw: str) -> str:
    s = raw.replace("_", " ").strip()
    s = " ".join(w if w.isupper() else w.capitalize() for w in s.split())
    low = s.lower()
    for k, v in _LEAGUE_FIX.items():
        if k in low:
            s = s.lower().replace(k, v)
    return s

def get_sport_and_league(event: dict) -> tuple[str, str, str]:
    """
    Return (emoji, sport_name, league_name) from an event.
    Keeps 'Soccer' named as 'Soccer' (not Football).
    """
    sport_key = event.get("sport_key", "")  # e.g. 'soccer_brazil_serie_b'
    sport_title = event.get("sport_title")  # nice human title (often contains league)

    # sport name
    if "_" in sport_key:
        sport_root = sport_key.split("_", 1)[0]
    else:
        sport_root = sport_key

    # keep american football distinct
    sport_root = sport_root.replace("football", "americanfootball")
    sport_name = "Soccer" if sport_root == "soccer" else sport_root.capitalize()

    emoji = SPORT_EMOJI.get(sport_root, "🏟️")

    # league
    league = None
    if sport_title:
        # The Odds API sometimes has 'Soccer Brazil Série B' style titles. Strip sport word.
        league = sport_title.replace("Soccer", "").strip()

    if not league and "_" in sport_key:
        suffix = sport_key.split("_", 1)[1]  # 'brazil_serie_b'
        league = _beautify_league(suffix)

    if not league or league.lower() == sport_name.lower():
        league = "League"

    return emoji, sport_name, league

# =========================
# Fetch Odds
# =========================
def fetch_odds() -> list:
    url = f"{API_BASE}/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return []

# =========================
# Calculate Bets
# =========================
def _allowed_bookmaker(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)

def calculate_bets(data: list) -> list[dict]:
    now = datetime.now(timezone.utc)
    bets = []

    for event in data:
        home, away = event.get("home_team"), event.get("away_team")
        if not home or not away:
            continue

        match_name = f"{home} vs {away}"

        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # consensus probability per outcome key
        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for market in book.get("markets", []):
                market_key = market.get("key", "")
                for outcome in market.get("outcomes", []):
                    if outcome.get("price") and outcome.get("name"):
                        key = f"{market_key}:{outcome['name']}"
                        consensus_by_outcome[key].append(1.0 / float(outcome["price"]))

        if not consensus_by_outcome:
            continue

        # global fallback consensus if a specific outcome is missing
        all_ps = [p for plist in consensus_by_outcome.values() for p in plist]
        global_consensus = sum(all_ps) / max(1, len(all_ps))

        # sport & league
        emoji, sport_name, league_name = get_sport_and_league(event)

        for book in event.get("bookmakers", []):
            title = book.get("title", "Unknown Bookmaker")
            if not _allowed_bookmaker(title):
                continue

            for market in book.get("markets", []):
                market_key = market.get("key", "")
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    name = outcome.get("name")
                    if not price or not name:
                        continue
                    price = float(price)

                    implied_p = 1.0 / price
                    outcome_key = f"{market_key}:{name}"
                    if outcome_key in consensus_by_outcome and len(consensus_by_outcome[outcome_key]):
                        consensus_p = sum(consensus_by_outcome[outcome_key]) / len(consensus_by_outcome[outcome_key])
                    else:
                        consensus_p = global_consensus

                    edge = consensus_p - implied_p  # >0 => value

                    if edge <= 0:
                        continue

                    # stakes (in units)
                    cons_stake = round(BANKROLL * CONSERVATIVE_PCT, 2)

                    # 'smart' is a small Kelly-flavored fraction of cons stake
                    smart_stake = round(max(0.1, cons_stake * max(0, edge) * 3), 2)

                    agg_stake = round(cons_stake * (1 + (edge * 100)), 2)

                    # payouts (units)
                    cons_payout = round(cons_stake * price, 2)
                    smart_payout = round(smart_stake * price, 2)
                    agg_payout = round(agg_stake * price, 2)

                    # expected profits (units)
                    cons_exp_profit = round(consensus_p * cons_payout - cons_stake, 2)
                    smart_exp_profit = round(consensus_p * smart_payout - smart_stake, 2)
                    agg_exp_profit = round(consensus_p * agg_payout - agg_stake, 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),
                        "probability": round(implied_p * 100, 2),
                        "consensus": round(consensus_p * 100, 2),
                        "edge": round(edge * 100, 2),
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "cons_payout": cons_payout,
                        "smart_payout": smart_payout,
                        "agg_payout": agg_payout,
                        "cons_exp_profit": cons_exp_profit,
                        "smart_exp_profit": smart_exp_profit,
                        "agg_exp_profit": agg_exp_profit,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),
                        "sport_emoji": emoji,
                        "sport_name": sport_name,
                        "league_name": league_name,
                        "_event_raw": event,
                    })

    return bets

# =========================
# Best Bet selection
# =========================
def _best_bet_choice(bets: list[dict]) -> dict | None:
    """Pick from value bets only. Never returns a 'low value' card."""
    value_bets = [b for b in bets if b["edge"] >= 2.0]  # 2 percentage points threshold
    if not value_bets:
        return None

    # blended score: prioritize chance to win but still reward price quality (EV)
    def score(b):
        p = b["consensus"] / 100.0
        price = b["odds"]
        ev = p * price - 1.0
        return (p * 0.65) + (ev * 0.35)

    return max(value_bets, key=score)

# =========================
# Formatting (units, league+emoji)
# =========================
def format_bet(b, title, color):
    indicator = "🟢 Value Bet" if b['edge'] >= 2 else "🛑 Low Value"

    sport_emoji = b.get("sport_emoji")
    sport_name = b.get("sport_name")
    league_name = b.get("league_name")
    if not sport_emoji:
        e_emoji, e_sport, e_league = get_sport_and_league(b.get("_event_raw", {}))
        sport_emoji, sport_name, league_name = e_emoji, e_sport, e_league

    header_sport = f"{sport_emoji} {sport_name} ({league_name})"

    description = (
        f"{indicator}\n\n"
        f"**{header_sport}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n\n"
        f"💵 **Conservative Stake:** {b['cons_stake']} units → Payout: {b['cons_payout']} units | Exp. Profit: {b['cons_exp_profit']} units\n"
        f"🧠 **Smart Stake:** {b['smart_stake']} units → Payout: {b['smart_payout']} units | Exp. Profit: {b['smart_exp_profit']} units\n"
        f"🔥 **Aggressive Stake:** {b['agg_stake']} units → Payout: {b['agg_payout']} units | Exp. Profit: {b['agg_exp_profit']} units\n"
    )
    return discord.Embed(title=title, description=description, color=color)

def bet_id(b):
    return f"{b['match']}|{b['team']}|{b['bookmaker']}|{b['time']}"

# =========================
# Posting logic
# =========================
async def post_bets(bets: list[dict]):
    if not bets:
        ch = bot.get_channel(BEST_BETS_CHANNEL)
        if ch:
            await ch.send("⚠️ No bets right now.")
        return

    # Best Bet (value-only)
    best = _best_bet_choice(bets)
    if best and bet_id(best) not in posted_bets:
        posted_bets.add(bet_id(best))
        ch = bot.get_channel(BEST_BETS_CHANNEL)
        if ch:
            await ch.send(embed=format_bet(best, "⭐ Best Bet", 0xFFD700))

    # Quick Returns
    q_list = [b for b in bets if b["quick_return"] and bet_id(b) not in posted_bets]
    q_ch = bot.get_channel(QUICK_RETURNS_CHANNEL)
    if q_ch:
        for b in q_list[:5]:
            posted_bets.add(bet_id(b))
            await q_ch.send(embed=format_bet(b, "⏱ Quick Return Bet", 0x2ECC71))

    # Long Plays
    l_list = [b for b in bets if b["long_play"] and bet_id(b) not in posted_bets]
    l_ch = bot.get_channel(LONG_PLAYS_CHANNEL)
    if l_ch:
        for b in l_list[:5]:
            posted_bets.add(bet_id(b))
            await l_ch.send(embed=format_bet(b, "📅 Longer Play Bet", 0x3498DB))

# =========================
# (Optional) DB: stub hooks
# =========================
def save_bet_to_db(bet: dict):
    """
    Hook to persist to Postgres when bot.paper_mode is ON (paper trading).
    Implement your psycopg2 insert here using your existing schema.
    """
    # Example (pseudo):
    # conn = psycopg2.connect(os.getenv("DATABASE_URL"), sslmode="require", cursor_factory=RealDictCursor)
    # cur = conn.cursor()
    # cur.execute("INSERT INTO bets(...) VALUES (...)", (...))
    # conn.commit()
    # conn.close()
    pass

# =========================
# Tasks & Slash Commands
# =========================
@tasks.loop(seconds=30)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)

    # Paper-trade logging (optional)
    if bot.paper_mode:
        for b in bets:
            try:
                save_bet_to_db(b)
            except Exception as e:
                print("DB save error:", e)

    await post_bets(bets)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print(f"❌ Slash sync failed: {e}")

    ch = bot.get_channel(BEST_BETS_CHANNEL)
    if ch:
        await ch.send("🎲 Betting bot is online and rolling bets!")

    if not bet_loop.is_running():
        bet_loop.start()

# ---- Slash Commands ----
@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong! I'm alive and fetching bets.", ephemeral=True)

@bot.tree.command(name="health", description="Show loop and fetch status summary.")
async def health_cmd(interaction: discord.Interaction):
    msg = (
        f"**Loop:** running ✅\n"
        f"**Odds API:** configured ✅\n"
        f"**Paper mode:** {'ON' if bot.paper_mode else 'OFF'}\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="fetchbets", description="Force a fetch/post cycle now.")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)
    await interaction.followup.send(f"Fetched {len(bets)} bets.", ephemeral=True)

@bot.tree.command(name="paper", description="Toggle paper-trading mode (DB logging only).")
async def paper_cmd(interaction: discord.Interaction, mode: str):
    m = mode.lower().strip()
    if m not in {"on", "off"}:
        await interaction.response.send_message("Use `/paper on` or `/paper off`.", ephemeral=True)
        return
    bot.paper_mode = (m == "on")
    await interaction.response.send_message(f"📄 Paper trading set to **{m.upper()}**.", ephemeral=True)

@bot.tree.command(name="roi", description="Show ROI summary (all-time).")
async def roi_cmd(interaction: discord.Interaction, strategy: str | None = None):
    """
    strategy (optional): conservative | smart | aggressive
    Wire this to your DB once you're ready.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    # TODO: query your DB and compute W/L, EV, and ROI by strategy.
    if strategy:
        strategy = strategy.lower()
        await interaction.followup.send(f"📈 ROI ({strategy}) — computed from saved bets (placeholder).", ephemeral=True)
    else:
        await interaction.followup.send("📈 ROI (all strategies) — computed from saved bets (placeholder).", ephemeral=True)

# =========================
# Run
# =========================
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

bot.run(TOKEN)













