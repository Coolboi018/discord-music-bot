from keep_alive import keep_alive
keep_alive()
import discord
from discord.ext import commands
import yt_dlp
import asyncio
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import os

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# --- YTDLP Setup ---
yt_dlp.utils.bug_reports_message = lambda: ''
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'quiet': True,
    'source_address': '0.0.0.0',
    'default_search': 'ytsearch',
    'extract_flat': False,
    'cookiefile': 'youtube.com_cookies.txt',
}
ffmpeg_options = {
    'options': '-vn'
}
ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# --- Spotify Setup ---
SPOTIFY_CLIENT_ID = "your_spotify_client_id"
SPOTIFY_CLIENT_SECRET = "your_spotify_client_secret"
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET))

queues = {}

# --- Helper ---
async def search_youtube(query):
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        if 'entries' in data:
            return data['entries'][0]['url']
        return data['url']
    except Exception as e:
        print(f"yt-dlp error: {e}")
        return None

async def play_next(ctx):
    guild_id = ctx.guild.id
    if queues[guild_id]:
        url = queues[guild_id].pop(0)
        await play_song(ctx, url)
    else:
        await asyncio.sleep(60)
        if not ctx.voice_client.is_playing():
            await ctx.voice_client.disconnect()

async def play_song(ctx, url):
    voice = ctx.voice_client
    if not voice:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            voice = await channel.connect()
        else:
            await ctx.send("❌ You need to be in a voice channel first!")
            return

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
    if data is None:
        await ctx.send("❌ Could not extract audio info.")
        return
    if 'entries' in data:
        data = data['entries'][0]

    source = discord.FFmpegPCMAudio(data['url'], **ffmpeg_options)
    voice.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))

    embed = discord.Embed(title="🎵 Now Playing", description=f"[{data.get('title', 'Unknown')}]({data.get('webpage_url', url)})", color=0x1DB954)
    embed.set_thumbnail(url=data.get("thumbnail", ""))
    await ctx.send(embed=embed)

# --- Commands ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.command()
async def play(ctx, *, query):
    guild_id = ctx.guild.id
    if guild_id not in queues:
        queues[guild_id] = []

    if "spotify.com" in query:
        await ctx.send("🎧 Spotify link detected! Fetching tracks...")
        try:
            if "track" in query:
                track = sp.track(query)
                search_query = f"{track['name']} {track['artists'][0]['name']} audio"
                yt_url = await search_youtube(search_query)
                if yt_url:
                    queues[guild_id].append(yt_url)
            elif "playlist" in query:
                results = sp.playlist_items(query)
                for item in results['items']:
                    track = item['track']
                    search_query = f"{track['name']} {track['artists'][0]['name']} audio"
                    yt_url = await search_youtube(search_query)
                    if yt_url:
                        queues[guild_id].append(yt_url)
            else:
                await ctx.send("❌ Unsupported Spotify link type.")
        except Exception as e:
            await ctx.send(f"⚠️ Spotify error: {e}")
            return
    else:
        yt_url = await search_youtube(query)
        if yt_url:
            queues[guild_id].append(yt_url)
        else:
            await ctx.send("❌ Couldn’t find any tracks for that query.")
            return

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await play_next(ctx)
    else:
        await ctx.send("🎶 Added to queue!")

@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped!")
    else:
        await ctx.send("❌ Nothing is playing!")

@bot.command()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused.")
    else:
        await ctx.send("❌ Nothing is playing!")

@bot.command()
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("❌ Nothing is paused!")

@bot.command()
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id].clear()
        ctx.voice_client.stop()
        await ctx.send("🛑 Stopped playback and cleared queue.")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Left the voice channel.")
    else:
        await ctx.send("❌ Not connected to any voice channel.")

@bot.command()
async def queue(ctx):
    q = queues.get(ctx.guild.id, [])
    if not q:
        await ctx.send("📭 The queue is empty.")
    else:
        msg = "\n".join([f"{i+1}. {url}" for i, url in enumerate(q[:10])])
        await ctx.send(f"🎶 **Current Queue:**\n{msg}")

@bot.command()
async def loop(ctx):
    await ctx.send("🔁 Loop feature coming soon (temporarily disabled).")

# --- Run bot ---
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    bot.run(token)
