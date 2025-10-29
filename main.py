# main.py
import os
import re
import asyncio
import logging
from threading import Thread
from collections import deque

import requests
from flask import Flask

import discord
from discord.ext import commands, tasks

import yt_dlp

# Optional Spotify
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except Exception:
    SPOTIFY_AVAILABLE = False
    spotipy = None

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("musicbot")

# -------------------- Flask keep-alive --------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "🎵 Music Bot (Render) — Alive"

@app.route("/health")
def health():
    return {"status": "healthy", "bot": "online"}

def run_flask():
    port = int(os.getenv("PORT", 8080))
    log.info(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port)

def start_keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# -------------------- Discord bot setup --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True)
bot.remove_command("help")

# -------------------- Global queues/state --------------------
music_queues = {}   # guild_id => deque of Player objects
now_playing = {}    # guild_id => Player
loop_mode = {}      # guild_id => 'off'|'track'|'queue'
idle_timers = {}    # guild_id => asyncio.Task

# Auto-leave settings
IDLE_TIMEOUT = 180  # 3 minutes
ALONE_TIMEOUT = 60  # 1 minute

# -------------------- yt-dlp config --------------------
COOKIEFILE_ENV = os.getenv("COOKIEFILE", "")

def get_ytdl_options():
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "source_address": "0.0.0.0",
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
        "no_check_certificate": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "nocheckcertificate": True,
        "age_limit": None,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Sec-Fetch-Mode": "navigate"
        }
    }
    
    if COOKIEFILE_ENV and os.path.exists(COOKIEFILE_ENV):
        opts["cookiefile"] = COOKIEFILE_ENV
        log.info(f"Using cookies file: {COOKIEFILE_ENV}")
    elif COOKIEFILE_ENV:
        log.warning(f"COOKIEFILE set to '{COOKIEFILE_ENV}' but file not found!")
    
    return opts

ytdl = yt_dlp.YoutubeDL(get_ytdl_options())

ffmpeg_opts = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -b:a 192k"  # Higher quality audio
}

# -------------------- Player wrapper --------------------
class Player(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, ctx, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title") or "Unknown title"
        self.url = data.get("url")
        self.webpage_url = data.get("webpage_url")
        self.duration = data.get("duration", 0)
        self.requester = ctx.author
        self.ctx = ctx

    @classmethod
    async def from_query(cls, query, ctx, *, loop=None, timeout=120):
        loop = loop or asyncio.get_event_loop()
        try:
            ytdl_temp = yt_dlp.YoutubeDL(get_ytdl_options())
            func = lambda: ytdl_temp.extract_info(query, download=False)
            data = await asyncio.wait_for(loop.run_in_executor(None, func), timeout=timeout)
            
            if not data:
                raise Exception("No data returned from yt-dlp")
            
            if "entries" in data and data["entries"]:
                data = data["entries"][0]
            
            if not data.get("url"):
                raise Exception("No playable URL found")
            
            src = discord.FFmpegPCMAudio(data["url"], **ffmpeg_opts)
            return cls(src, data=data, ctx=ctx)
            
        except asyncio.TimeoutError:
            raise Exception("⏱️ Timeout while fetching audio")
        except Exception as e:
            err = str(e).lower()
            if "sign in" in err or "not available" in err or "members-only" in err:
                raise Exception("🔒 Video requires YouTube login. Please set COOKIEFILE environment variable.")
            elif "private video" in err:
                raise Exception("🔒 This video is private")
            elif "video unavailable" in err:
                raise Exception("❌ Video unavailable")
            else:
                raise Exception(f"Error: {str(e)[:200]}")

# -------------------- Spotify helpers --------------------
def spotify_queries_from_url(spotify_url):
    queries = []
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    
    if not SPOTIFY_AVAILABLE:
        log.warning("Spotipy not installed")
        return queries
        
    if not client_id or not client_secret:
        log.warning("Spotify credentials not set")
        return queries

    try:
        auth = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
        sp = spotipy.Spotify(auth_manager=auth)
        
        if "track" in spotify_url:
            track = sp.track(spotify_url)
            name = track.get("name")
            artist = track.get("artists", [{}])[0].get("name", "")
            if name:
                queries.append(f"{name} {artist}")
                
        elif "playlist" in spotify_url:
            playlist_id = spotify_url.split("/")[-1].split("?")[0]
            results = sp.playlist_tracks(playlist_id, limit=100)
            
            for item in results.get("items", []):
                t = item.get("track")
                if t:
                    name = t.get("name")
                    artist = t.get("artists", [{}])[0].get("name", "")
                    if name:
                        queries.append(f"{name} {artist}")
            
            while results.get("next"):
                results = sp.next(results)
                for item in results.get("items", []):
                    t = item.get("track")
                    if t:
                        name = t.get("name")
                        artist = t.get("artists", [{}])[0].get("name", "")
                        if name:
                            queries.append(f"{name} {artist}")
                        
        elif "album" in spotify_url:
            album_id = spotify_url.split("/")[-1].split("?")[0]
            results = sp.album_tracks(album_id, limit=50)
            album_info = sp.album(album_id)
            album_artist = album_info.get("artists", [{}])[0].get("name", "")
            
            for item in results.get("items", []):
                name = item.get("name")
                artist = item.get("artists", [{}])[0].get("name", album_artist)
                if name:
                    queries.append(f"{name} {artist}")
            
            while results.get("next"):
                results = sp.next(results)
                for item in results.get("items", []):
                    name = item.get("name")
                    artist = item.get("artists", [{}])[0].get("name", album_artist)
                    if name:
                        queries.append(f"{name} {artist}")
                    
    except Exception as e:
        log.error(f"Spotify API error: {e}")
        raise
    
    return queries

def simple_spotify_title_scrape(spotify_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(spotify_url, timeout=10, headers=headers)
        m = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
        if m:
            title = m.group(1).replace(" - Spotify", "").replace(" | Spotify", "").strip()
            title = re.sub(r'\s*-\s*song\s+(and\s+)?lyrics.*', '', title, flags=re.I)
            return title
    except Exception as e:
        log.debug(f"Spotify scrape error: {e}")
    return None

# -------------------- Auto-leave --------------------
def cancel_idle_timer(guild_id):
    if guild_id in idle_timers:
        idle_timers[guild_id].cancel()
        del idle_timers[guild_id]

async def start_idle_disconnect_timer(ctx):
    guild_id = ctx.guild.id
    cancel_idle_timer(guild_id)
    
    async def idle_check():
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            if ctx.voice_client and not ctx.voice_client.is_playing():
                q = music_queues.get(guild_id, deque())
                if not q:
                    try:
                        await ctx.send(f"⏰ Left due to {IDLE_TIMEOUT//60} min inactivity")
                        await ctx.voice_client.disconnect()
                    except:
                        pass
                    music_queues.pop(guild_id, None)
                    now_playing.pop(guild_id, None)
                    loop_mode.pop(guild_id, None)
                    idle_timers.pop(guild_id, None)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Idle timer error: {e}")
    
    idle_timers[guild_id] = asyncio.create_task(idle_check())

@tasks.loop(seconds=30)
async def check_alone_in_voice():
    for guild in bot.guilds:
        if guild.voice_client and guild.voice_client.is_connected():
            channel = guild.voice_client.channel
            human_count = sum(1 for m in channel.members if not m.bot)
            
            if human_count == 0:
                if guild.id not in idle_timers or idle_timers[guild.id].done():
                    async def alone_disconnect():
                        try:
                            await asyncio.sleep(ALONE_TIMEOUT)
                            if guild.voice_client and guild.voice_client.is_connected():
                                channel = guild.voice_client.channel
                                human_count = sum(1 for m in channel.members if not m.bot)
                                
                                if human_count == 0:
                                    try:
                                        text_channel = None
                                        if guild.id in now_playing:
                                            text_channel = now_playing[guild.id].ctx.channel
                                        elif guild.text_channels:
                                            text_channel = guild.text_channels[0]
                                        
                                        if text_channel:
                                            await text_channel.send("👋 Everyone left, disconnecting!")
                                        await guild.voice_client.disconnect()
                                    except:
                                        pass
                                    music_queues.pop(guild.id, None)
                                    now_playing.pop(guild.id, None)
                                    loop_mode.pop(guild.id, None)
                                    idle_timers.pop(guild.id, None)
                        except asyncio.CancelledError:
                            pass
                    
                    idle_timers[guild.id] = asyncio.create_task(alone_disconnect())

# -------------------- Playback logic --------------------
async def _start_playback(ctx):
    guild_id = ctx.guild.id
    q = music_queues.get(guild_id)
    
    if not q or not ctx.voice_client:
        return
    
    player = q.popleft()
    now_playing[guild_id] = player
    
    def after_playing(error):
        if error:
            log.error(f"Player error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
    
    ctx.voice_client.play(player, after=after_playing)
    
    duration_str = f"{player.duration // 60}:{player.duration % 60:02d}" if player.duration else "Unknown"
    embed = discord.Embed(title="🎵 Now Playing", description=f"**{player.title}**", color=discord.Color.blue())
    embed.add_field(name="Duration", value=duration_str, inline=True)
    embed.add_field(name="Requested by", value=player.requester.mention, inline=True)
    if player.webpage_url:
        embed.add_field(name="Link", value=f"[Click here]({player.webpage_url})", inline=False)
    
    await ctx.send(embed=embed)

async def play_next(ctx):
    guild_id = ctx.guild.id
    current_loop = loop_mode.get(guild_id, "off")
    
    if current_loop == "track":
        player = now_playing.get(guild_id)
        if player:
            try:
                new_player = await Player.from_query(player.webpage_url or player.title, ctx)
                now_playing[guild_id] = new_player
                
                def after_playing(error):
                    if error:
                        log.error(f"Player error: {error}")
                    asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
                
                ctx.voice_client.play(new_player, after=after_playing)
                return
            except Exception as e:
                log.error(f"Loop track error: {e}")
    
    q = music_queues.get(guild_id, deque())
    
    if current_loop == "queue":
        player = now_playing.get(guild_id)
        if player:
            try:
                new_player = await Player.from_query(player.webpage_url or player.title, ctx)
                q.append(new_player)
            except:
                pass
    
    if q:
        await _start_playback(ctx)
    else:
        now_playing.pop(guild_id, None)
        await start_idle_disconnect_timer(ctx)

# -------------------- Commands --------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user}")
    log.info(f"Cookies: {COOKIEFILE_ENV if COOKIEFILE_ENV else 'Not set'}")
    log.info(f"Spotify: {'Configured' if os.getenv('SPOTIFY_CLIENT_ID') else 'Not configured'}")
    
    music_queues.clear()
    now_playing.clear()
    loop_mode.clear()
    idle_timers.clear()
    
    if not check_alone_in_voice.is_running():
        check_alone_in_voice.start()

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    if after.channel and bot.user in after.channel.members:
        cancel_idle_timer(after.channel.guild.id)

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query: str):
    if not ctx.author.voice:
        await ctx.send("❌ Join a voice channel first!")
        return

    channel = ctx.author.voice.channel
    if not ctx.voice_client:
        try:
            await channel.connect()
        except Exception as e:
            await ctx.send(f"❌ Couldn't connect: {e}")
            return

    guild_id = ctx.guild.id
    q = music_queues.setdefault(guild_id, deque())
    loop_mode.setdefault(guild_id, "off")
    cancel_idle_timer(guild_id)

    status = await ctx.send("🔍 Searching...")

    # Spotify handling
    if "spotify.com" in query or "spotify.link" in query:
        try:
            queries = spotify_queries_from_url(query)
            
            if not queries:
                scraped = simple_spotify_title_scrape(query)
                if scraped:
                    queries = [scraped]
                else:
                    await status.edit(content="❌ Spotify error. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env variables.")
                    return

            await status.edit(content=f"🎵 Processing {len(queries)} track(s)...")
            added = 0
            failed = 0
            
            for idx, qstr in enumerate(queries, 1):
                try:
                    if idx % 5 == 0:
                        await status.edit(content=f"🎵 Processing: {idx}/{len(queries)}")
                    
                    player = await Player.from_query(f"ytsearch:{qstr}", ctx, loop=bot.loop)
                    q.append(player)
                    added += 1
                except Exception as e:
                    failed += 1
                    log.warning(f"Failed '{qstr}': {e}")
            
            result_msg = f"✅ Added **{added}** track(s)"
            if failed > 0:
                result_msg += f" ({failed} failed)"
            
            await status.edit(content=result_msg)
            
            if not ctx.voice_client.is_playing() and guild_id not in now_playing:
                await asyncio.sleep(0.5)
                await status.delete()
                await _start_playback(ctx)
            else:
                await asyncio.sleep(3)
                await status.delete()
            return
            
        except Exception as e:
            await status.edit(content=f"❌ Spotify error: {str(e)[:500]}")
            await asyncio.sleep(5)
            await status.delete()
            return

    # YouTube playlist
    is_playlist = ("youtube.com" in query or "youtu.be" in query) and ("list=" in query or "playlist" in query)
    
    try:
        if is_playlist:
            await status.edit(content="📋 Extracting playlist...")
            loop = asyncio.get_event_loop()
            
            ytdl_playlist = yt_dlp.YoutubeDL({**get_ytdl_options(), "extract_flat": True})
            data = await loop.run_in_executor(None, lambda: ytdl_playlist.extract_info(query, download=False))
            
            if not data:
                raise Exception("Could not extract playlist")
            
            entries = data.get("entries", [])
            if not entries:
                raise Exception("No videos found")
            
            await status.edit(content=f"📋 Found {len(entries)} videos. Adding...")
            
            added = 0
            failed = 0
            
            for idx, entry in enumerate(entries, 1):
                try:
                    if idx % 5 == 0:
                        await status.edit(content=f"📋 Adding: {idx}/{len(entries)}")
                    
                    video_url = entry.get("url") or f"https://youtube.com/watch?v={entry['id']}"
                    player = await Player.from_query(video_url, ctx, loop=bot.loop, timeout=60)
                    q.append(player)
                    added += 1
                except Exception as e:
                    failed += 1
                    log.warning(f"Failed video {idx}: {e}")
            
            result_msg = f"✅ Added **{added}** video(s) from playlist"
            if failed > 0:
                result_msg += f" ({failed} failed)"
            
            await status.edit(content=result_msg)
            
            if not ctx.voice_client.is_playing() and guild_id not in now_playing:
                await asyncio.sleep(0.5)
                await status.delete()
                await _start_playback(ctx)
            else:
                await asyncio.sleep(3)
                await status.delete()
            return
        
        # Single video/search
        player = await Player.from_query(query, ctx, loop=bot.loop)
        q.append(player)
        
        if ctx.voice_client.is_playing() or guild_id in now_playing:
            pos = len(q)
            await status.edit(content=f"✅ Added to queue (#{pos}): **{player.title}**")
            await asyncio.sleep(3)
            await status.delete()
        else:
            await status.delete()
            await _start_playback(ctx)
            
    except Exception as e:
        await status.edit(content=f"❌ Er
