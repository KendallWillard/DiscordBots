import discord
from discord.ext import commands
from discord.ui import Button, View
import yt_dlp
import asyncio
from collections import deque
import re
import time  # <--- ADDED: For time tracking
import os
import traceback  # <--- ADDED: For full traceback logging
from spotify_scraper import SpotifyClient  # <--- ADDED: For Spotify scraping (no API creds needed)
from discord import app_commands  # Add this import if not already there
import aiohttp  # Add this import at the top if missing
from dotenv import load_dotenv  # <--- ADD this import

# Load opus explicitly
if not discord.opus.is_loaded():
    try:
        discord.opus.load_opus('/opt/homebrew/lib/libopus.0.dylib')  # <--- ADDED: Adjust to your exact path from 'find' command
        print("Successfully loaded libopus.")
    except Exception as e:
        print(f"Failed to load libopus: {repr(e)}")

# Configuration
load_dotenv()  # <--- ADD this line (loads .env file)
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN');

if DISCORD_TOKEN is None:
    raise ValueError("DISCORD_TOKEN not found in .env file!")
# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# yt-dlp options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '/tmp/%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': False,  # Keep False for debugging
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

ffmpeg_options = {
    'options': '-vn'  # <--- CHANGED: Removed 'before_options' to avoid invalid option errors for local files
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class MusicQueue:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.history = deque(maxlen=50)
        self.now_playing_msg = None  # <--- ADDED: Track the message for editing
        self.elapsed = 0  # <--- ADDED: Accumulated elapsed seconds
        self.start_time = None  # <--- ADDED: For resume tracking
        self.progress_task = None  # <--- ADDED: For update loop
        self.is_looping = False  # <--- ADDED: Loop toggle (placeholder; implement in play_next if needed)
    
    def add(self, item):
        self.queue.append(item)
    
    def add_next(self, item):
        self.queue.appendleft(item)
    
    def get_next(self):
        if self.current:
            self.history.append(self.current)
        self.current = self.queue.popleft() if self.queue else None
        return self.current
    
    def get_previous(self):
        if self.history:
            prev = self.history.pop()
            if self.current:
                self.queue.appendleft(self.current)
            self.current = prev
            return prev
        return None
    
    def clear(self):
            self.queue.clear()
            self.current = None
            self.now_playing_msg = None
            self.elapsed = 0
            self.start_time = None
            if self.progress_task:
                self.progress_task.cancel()
            self.progress_task = None
    
    def is_empty(self):
        return len(self.queue) == 0

# Guild-specific music queues
guild_queues = {}

def get_queue(guild_id):
    if guild_id not in guild_queues:
        guild_queues[guild_id] = MusicQueue()
    return guild_queues[guild_id]

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.thumbnail = data.get('thumbnail')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):  # stream param is unused now, but kept for compatibility
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=True))  # download=True to handle download
        
        if 'entries' in data:
            data = data['entries'][0]
        
        print("Data keys from yt-dlp:", list(data.keys()))  # <--- ADDED: Log available keys for debugging
        
        filename = data['requested_downloads'][0]['filepath']
        print(f"Downloaded filename: {filename}")  # <--- ADDED: Log filename
        print(f"File exists: {os.path.exists(filename)}")  # <--- ADDED: Check existence
        print(f"File size: {os.stat(filename).st_size if os.path.exists(filename) else 'N/A'} bytes")  # <--- ADDED: Check size (should be ~3MB for this song)
        print(f"File permissions: {oct(os.stat(filename).st_mode)[-3:] if os.path.exists(filename) else 'N/A'}")  # <--- ADDED: Check readable (should be 644 or similar)
        
        # Simulate FFmpeg command for logging (what discord.py will roughly run)
        print(f"Simulated FFmpeg command: /opt/homebrew/bin/ffmpeg -i {filename} {ffmpeg_options.get('options', '')} -f s16le -ar 48000 -ac 2 pipe:1")
        
        player = cls(discord.FFmpegPCMAudio(filename, executable="/opt/homebrew/bin/ffmpeg", **ffmpeg_options), data=data)
        player.filename = filename  # For cleanup
        return player

class MusicControls(View):
    def __init__(self, bot, ctx):
        super().__init__(timeout=None)
        self.bot = bot
        self.ctx = ctx

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def play_pause(self, interaction: discord.Interaction, button: Button):
        queue = get_queue(self.ctx.guild.id)
        if self.ctx.voice_client:
            if self.ctx.voice_client.is_paused():
                self.ctx.voice_client.resume()
                button.label = "Pause"
                button.emoji = "⏸️"
                queue.start_time = time.time()
            elif self.ctx.voice_client.is_playing():
                self.ctx.voice_client.pause()
                button.label = "Play"
                button.emoji = "▶️"
                queue.elapsed += time.time() - queue.start_time
            await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client:
            self.ctx.voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Previous", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: Button):
        queue = get_queue(self.ctx.guild.id)
        prev = queue.get_previous()
        if prev:
            if self.ctx.voice_client:
                self.ctx.voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client:
            queue = get_queue(self.ctx.guild.id)
            queue.clear()
            self.ctx.voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Queue", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def show_queue(self, interaction: discord.Interaction, button: Button):
        await self.bot.get_command('queue').callback(self.ctx)
        await interaction.response.defer()

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle(self, interaction: discord.Interaction, button: Button):
        await self.bot.get_command('shuffle').callback(self.ctx)
        await interaction.response.defer()

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop(self, interaction: discord.Interaction, button: Button):
        queue = get_queue(self.ctx.guild.id)
        queue.is_looping = not queue.is_looping
        button.style = discord.ButtonStyle.success if queue.is_looping else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Vol +10%", emoji="🔊", style=discord.ButtonStyle.green, row=2)
    async def volume_up(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.source:
            vol = min(1.0, self.ctx.voice_client.source.volume + 0.1)
            self.ctx.voice_client.source.volume = vol
            await interaction.response.edit_message(embed=build_now_playing_embed(self.ctx), view=self)

    @discord.ui.button(label="Vol -10%", emoji="🔉", style=discord.ButtonStyle.red, row=2)
    async def volume_down(self, interaction: discord.Interaction, button: Button):
        if self.ctx.voice_client and self.ctx.voice_client.source:
            vol = max(0.0, self.ctx.voice_client.source.volume - 0.1)
            self.ctx.voice_client.source.volume = vol
            await interaction.response.edit_message(embed=build_now_playing_embed(self.ctx), view=self)

    @discord.ui.button(label="Add to Queue", emoji="➕", style=discord.ButtonStyle.blurple, row=3)
    async def add_to_queue(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "**Add to Queue:**\n"
            "Paste your song/link/search after this command and hit Enter:\n"
            "```!play ```",
            ephemeral=True  # Optional: Only you see it (less spam)
        )

    @discord.ui.button(label="Play Next", emoji="⏭️", style=discord.ButtonStyle.blurple, row=3)
    async def play_next_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "**Play Next:**\n"
            "Paste your song/link/search after this command and hit Enter:\n"
            "```!playnext ```",
            ephemeral=True  # Optional: Only you see it
        )

def build_now_playing_embed(ctx):
    queue = get_queue(ctx.guild.id)
    if not queue.current:
        return discord.Embed(title="❌ Nothing Playing", description="Use `!play` to start!", color=0xFF0000)
    
    duration = queue.current['duration'] if queue.current['duration'] else 0
    elapsed = queue.elapsed
    if ctx.voice_client.is_playing() and queue.start_time:
        elapsed += time.time() - queue.start_time
    
    progress = min(1, elapsed / duration) if duration > 0 else 0
    bar = "█" * int(10 * progress) + "░" * (10 - int(10 * progress))
    time_str = f"{int(elapsed // 60)}:{int(elapsed % 60):02d} / {duration // 60}:{duration % 60:02d}"
    
    embed = discord.Embed(title="🎵 Now Playing", description=f"**{queue.current['title']}**", color=0x1DB954)
    embed.add_field(name="⏱️ Duration", value=f"{duration // 60}:{duration % 60:02d}", inline=True)
    embed.add_field(name="📋 In Queue", value=f"{len(queue.queue)} songs", inline=True)
    embed.add_field(name="🔊 Volume", value=f"{int(ctx.voice_client.source.volume * 100)}%" if ctx.voice_client else "N/A", inline=True)
    embed.add_field(name="⏳ Progress", value=f"{bar} {time_str}", inline=False)
    
    if queue.current.get('thumbnail'):
        embed.set_image(url=queue.current['thumbnail'])
    
    embed.set_footer(text="⏯️ Pause/Play | ⏭️ Skip | ⏮️ Previous | ⏹️ Stop")
    return embed

async def update_progress(ctx):
    queue = get_queue(ctx.guild.id)
    while ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        if queue.now_playing_msg:
            await queue.now_playing_msg.edit(embed=build_now_playing_embed(ctx))
        await asyncio.sleep(1)
    queue.progress_task = None

async def play_next(ctx):
    """Play the next song in queue"""
    queue = get_queue(ctx.guild.id)
    
    if queue.progress_task:
        queue.progress_task.cancel()
    
    if queue.is_empty():
        embed = discord.Embed(
            title="🎵 Queue Finished",
            description="All songs have been played!",
            color=0x1DB954
        )
        embed.set_footer(text="Use !play to add more songs")
        await ctx.send(embed=embed)
        return
    
    next_song = queue.get_next()
    if next_song:
        try:
            player = await YTDLSource.from_url(next_song['url'], loop=bot.loop)
            
            def after_playing(error):
                queue.elapsed = 0
                queue.start_time = None
                if queue.progress_task:
                    queue.progress_task.cancel()
                try:
                    if os.path.exists(player.filename):
                        os.remove(player.filename)
                except Exception as cleanup_err:
                    print(f"Cleanup error: {repr(cleanup_err)}")
                if error:
                    print(f"Player error: {error}")
                if queue.is_looping and queue.current:  # Re-add for loop
                    queue.add_next(queue.current)
                asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            
            ctx.voice_client.play(player, after=after_playing)
            
            queue.elapsed = 0
            queue.start_time = time.time()
            
            embed = build_now_playing_embed(ctx)  # <--- CHANGED: Use new build function
            view = MusicControls(bot, ctx)
            msg = await ctx.send(embed=embed, view=view)
            queue.now_playing_msg = msg
            
            queue.progress_task = bot.loop.create_task(update_progress(ctx))
        except Exception as e:
            print(f"Error playing track: {repr(e)}\n{traceback.format_exc()}")
            await ctx.send(f"❌ Error playing track, skipping to next...")
            await play_next(ctx)

@bot.event
async def on_ready():
    print(f'{bot.user} is connected and ready!')
    print('Note: This bot uses yt-dlp to search and play music from YouTube')
    print("Opus loaded on ready:", discord.opus.is_loaded())
    try:
        synced = await bot.tree.sync()  # <--- ADDED: Syncs global slash commands
        print(f"Synced {len(synced)} slash command(s) globally!")
    except Exception as e:
        print(f"Sync error: {e}")


@bot.command(name='join')
async def join(ctx):
    """Join your voice channel"""
    if not ctx.author.voice:
        await ctx.send("❌ You need to be in a voice channel!")
        return
    
    channel = ctx.author.voice.channel
    if ctx.voice_client is not None:
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()
    await ctx.send(f"✅ Joined {channel.name}")

@bot.command(name='leave')
async def leave(ctx):
    """Leave voice channel"""
    if ctx.voice_client:
        queue = get_queue(ctx.guild.id)
        queue.clear()
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Left voice channel")
    else:
        await ctx.send("❌ Not in a voice channel")

@bot.command(name='test')
async def test_audio(ctx):
    """Test audio playback with a local file"""
    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            await ctx.send("❌ You need to be in a voice channel!")
            return
    
    try:
        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio('/tmp/test.mp3'))
        ctx.voice_client.play(source)
        await ctx.send("🔊 Playing test audio file...")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")
        print(f"Test audio error: {e}")

async def song_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    print(f"AUTOCOMPLETE TRIGGERED: current='{current}'")
    
    if len(current) < 3:
        print("AUTOCOMPLETE: Query too short")
        return []
    
    async with aiohttp.ClientSession() as session:
        try:
            # YouTube suggestions (your current fast ones)
            yt_url = "https://suggestqueries.google.com/complete/search"
            yt_params = {'client': 'youtube', 'ds': 'yt', 'q': current}
            async with session.get(yt_url, params=yt_params, timeout=2.0) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    start = text.find('(')
                    if start != -1:
                        json_text = text[start+1:-1]
                        import json
                        data = json.loads(json_text)
                        if len(data) > 1:
                            suggestions = [item[0] for item in data[1][:5] if isinstance(item, list) and len(item) > 0]
                            choices = [app_commands.Choice(name=sugg[:100], value=sugg) for sugg in suggestions]
                            print(f"AUTOCOMPLETE: Returning {len(choices)} YouTube suggestions")
                            return choices
        except Exception as e:
            print(f"AUTOCOMPLETE YouTube error: {repr(e)}")
        
        # Fallback: General Google suggestions (often Spotify-like for music)
        try:
            google_url = "https://suggestqueries.google.com/complete/search"
            google_params = {'client': 'firefox', 'q': f"{current} spotify"}
            async with session.get(google_url, params=google_params, timeout=2.0) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if len(data) > 1:
                        suggestions = [sugg for sugg in data[1][:5] if isinstance(sugg, str)]
                        # Clean "song name spotify" → "song name"
                        clean_suggestions = [sugg.replace(" spotify", "", 1).replace(" Spotify", "", 1) for sugg in suggestions]
                        choices = [app_commands.Choice(name=sugg[:100], value=sugg) for sugg in clean_suggestions]
                        print(f"AUTOCOMPLETE: Returning {len(choices)} Spotify-like suggestions")
                        return choices
        except Exception as e:
            print(f"AUTOCOMPLETE Google fallback error: {repr(e)}")
    
    print("AUTOCOMPLETE: No suggestions, returning empty")
    return []

@bot.hybrid_command(name="p", description="Play a song from YouTube/Spotify", aliases=["play"])
@app_commands.describe(query="Song name, YouTube/Spotify URL, or search query")
@app_commands.autocomplete(query=song_autocomplete)  # Attach autocomplete here
async def play(ctx: commands.Context, *, query: str):
    """Play a song from YouTube/Spotify (searches YouTube if not a URL)"""
    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            await ctx.send("❌ You need to be in a voice channel!")
            return
    
    async with ctx.typing():
        try:
            queue = get_queue(ctx.guild.id)
            if 'spotify.com' in query:  # <--- UPDATED: Scrape with spotifyscraper
                await ctx.send("🔍 Scraping from Spotify (no API needed)...")
                client = SpotifyClient()
                
                spotify_type = None
                spotify_url = query.split('?')[0]  # Strip params
                if '/track/' in spotify_url:
                    spotify_type = 'track'
                    data = client.get_track_info(spotify_url)
                    tracks = [data] if data else []
                elif '/playlist/' in spotify_url:
                    spotify_type = 'playlist'
                    data = client.get_playlist_info(spotify_url)
                    tracks = data.get('tracks', [])[:100]  # Limit to 100 to avoid overload; adjust as needed
                elif '/album/' in spotify_url:
                    spotify_type = 'album'
                    data = client.get_album_info(spotify_url)
                    tracks = data.get('tracks', [])[:100]
                else:
                    await ctx.send("❌ Invalid Spotify URL. Supports tracks, playlists, and albums.")
                    client.close()
                    return
                
                if not tracks:
                        await ctx.send("❌ No tracks found or URL is private/restricted.")
                        client.close()
                        return

                added = 0
                skipped = 0
                started_playback = False

                for track in tracks:
                    artist = track.get('artists', [{}])[0].get('name', '') if track.get('artists') else ''
                    title = track.get('name', '')
                    if not title:
                        skipped += 1
                        continue
                    
                    search_query = f"ytsearch:{artist} {title}"
                    yt_data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
                    
                    if 'entries' in yt_data and yt_data['entries']:
                        valid_entries = [e for e in yt_data['entries'] if e and e.get('duration', 0) > 60]
                        if valid_entries:
                            yt_entry = sorted(valid_entries, key=lambda x: x.get('duration', 0), reverse=True)[0]
                            song_info = {
                                'url': yt_entry.get('webpage_url'),
                                'title': yt_entry.get('title'),
                                'duration': yt_entry.get('duration'),
                                'thumbnail': yt_entry.get('thumbnail')
                            }
                            queue.add(song_info)
                            added += 1

                            # Start playback on the very first successful add
                            if not started_playback and not ctx.voice_client.is_playing():
                                await play_next(ctx)
                                started_playback = True
                        else:
                            skipped += 1
                    else:
                        skipped += 1

                client.close()
                
                await ctx.send(f"✅ Added {added} tracks from Spotify {spotify_type}! (Skipped {skipped} due to no matches)")
            else:
                # Search YouTube
                search_query = f"ytsearch:{query}"
                # Change download=False to extract metadata only
                data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
                
                if 'entries' in data and data['entries']:
                    data = data['entries'][0]
                    
                    queue = get_queue(ctx.guild.id)
                    queue.add({
                        'url': data.get('webpage_url'), # Use the YouTube link, not the raw stream link
                        'title': data.get('title'),
                        'duration': data.get('duration'),
                        'thumbnail': data.get('thumbnail')
                    })
                    
                    if not ctx.voice_client.is_playing():
                        await play_next(ctx)
                    else:
                        await ctx.send(f"✅ Added to queue: **{data.get('title')}**")
                else:
                    await ctx.send("❌ No results found")
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")




@bot.command(name='pause')
async def pause(ctx):
    """Pause playback"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        queue = get_queue(ctx.guild.id)
        queue.elapsed += time.time() - queue.start_time
        await ctx.send("⏸️ Paused")
    else:
        await ctx.send("❌ Nothing is playing")

@bot.command(name='resume')
async def resume(ctx):
    """Resume playback"""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        queue = get_queue(ctx.guild.id)
        queue.start_time = time.time()
        await ctx.send("▶️ Resumed")
    else:
        await ctx.send("❌ Nothing is paused")

@bot.command(name='stop')
async def stop(ctx):
    """Stop playback and clear queue"""
    if ctx.voice_client:
        queue = get_queue(ctx.guild.id)
        queue.clear()
        ctx.voice_client.stop()
        await ctx.send("⏹️ Stopped and cleared queue")
    else:
        await ctx.send("❌ Not playing anything")

@bot.command(name='skip')
async def skip(ctx):
    """Skip to next track"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped")
    else:
        await ctx.send("❌ Nothing is playing")

@bot.command(name='previous')
async def previous(ctx):
    """Go back to previous track"""
    queue = get_queue(ctx.guild.id)
    prev = queue.get_previous()
    
    if prev:
        queue.current = prev  # Set directly for play_next
        await play_next(ctx)  # <--- CHANGED: Call play_next to handle embed/view/progress
        await ctx.send(f"⏮️ Playing previous: **{prev['title']}**")
    else:
        await ctx.send("❌ No previous track in history")

@bot.command(name='queue')
async def show_queue(ctx):
    """Show current queue"""
    queue = get_queue(ctx.guild.id)
    
    if queue.is_empty() and not queue.current:
        embed = discord.Embed(
            title="📭 Queue is Empty",
            description="No songs in queue! Use `!play` to add some music.",
            color=0xFF0000
        )
        await ctx.send(embed=embed)
        return
    
    embed = discord.Embed(title="🎵 Music Queue", color=0x1DB954)
    
    if queue.current:
        duration = f"{queue.current['duration'] // 60}:{queue.current['duration'] % 60:02d}" if queue.current['duration'] else "?"
        embed.add_field(
            name="▶️ Now Playing", 
            value=f"**{queue.current['title']}** `[{duration}]`", 
            inline=False
        )
        if queue.current.get('thumbnail'):
            embed.set_thumbnail(url=queue.current['thumbnail'])
    
    if not queue.is_empty():
        queue_list = []
        total_duration = 0
        
        for i, song in enumerate(list(queue.queue)[:10], 1):
            duration = song['duration'] if song['duration'] else 0
            total_duration += duration
            duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "?"
            queue_list.append(f"`{i}.` {song['title']} `[{duration_str}]`")
        
        total_min = total_duration // 60
        total_sec = total_duration % 60
        
        embed.add_field(
            name=f"📋 Up Next • {len(queue.queue)} tracks • {total_min}:{total_sec:02d} total", 
            value="\n".join(queue_list) if queue_list else "Empty",
            inline=False
        )
        
        if len(queue.queue) > 10:
            embed.set_footer(text=f"+ {len(queue.queue) - 10} more songs in queue")
    
    await ctx.send(embed=embed)

@bot.command(name='playnext')
async def play_next_command(ctx, *, query: str):
    """Add a song to play next in queue"""
    async with ctx.typing():
        try:
            search_query = f"ytsearch:{query}"
            data = await bot.loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
            
            if 'entries' in data and data['entries']:
                data = data['entries'][0]
                
                queue = get_queue(ctx.guild.id)
                queue.add_next({
                    'url': data.get('url') or data.get('webpage_url'),
                    'title': data.get('title'),
                    'duration': data.get('duration'),
                    'thumbnail': data.get('thumbnail')
                })
                
                await ctx.send(f"⏩ Added to play next: **{data.get('title')}**")
            else:
                await ctx.send("❌ No results found")
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")

@bot.command(name='shuffle')
async def shuffle(ctx):
    """Shuffle the current queue"""
    import random
    queue = get_queue(ctx.guild.id)
    
    if queue.is_empty():
        await ctx.send("❌ Queue is empty!")
        return
    
    queue_list = list(queue.queue)
    random.shuffle(queue_list)
    queue.queue = deque(queue_list)
    
    embed = discord.Embed(
        title="🔀 Queue Shuffled",
        description=f"Shuffled {len(queue_list)} songs!",
        color=0x1DB954
    )
    await ctx.send(embed=embed)

@bot.command(name='loop')
async def loop(ctx, mode: str = "off"):
    """Loop current song or queue (options: song, queue, off)"""
    # This is a placeholder - you'd need to implement loop logic in play_next
    embed = discord.Embed(
        title="🔁 Loop Mode",
        description=f"Loop mode set to: **{mode}**\n(Feature coming soon!)",
        color=0x1DB954
    )
    await ctx.send(embed=embed)

@bot.command(name='lyrics')
async def lyrics(ctx):
    """Get lyrics for current song (placeholder)"""
    queue = get_queue(ctx.guild.id)
    
    if not queue.current:
        await ctx.send("❌ No song is playing!")
        return
    
    embed = discord.Embed(
        title="📝 Lyrics",
        description=f"Search for lyrics: **{queue.current['title']}**\n\n[Search on Genius](https://genius.com/search?q={queue.current['title'].replace(' ', '%20')})",
        color=0x1DB954
    )
    await ctx.send(embed=embed)

@bot.command(name='clearqueue')
async def clear_queue(ctx):
    """Clear all songs from queue"""
    queue = get_queue(ctx.guild.id)
    queue_size = len(queue.queue)
    queue.queue.clear()
    
    embed = discord.Embed(
        title="🗑️ Queue Cleared",
        description=f"Removed {queue_size} songs from queue",
        color=0xFF6B6B
    )
    await ctx.send(embed=embed)

@bot.command(name='remove')
async def remove(ctx, position: int):
    """Remove a song from queue by position"""
    queue = get_queue(ctx.guild.id)
    
    if position < 1 or position > len(queue.queue):
        await ctx.send(f"❌ Invalid position! Queue has {len(queue.queue)} songs.")
        return
    
    queue_list = list(queue.queue)
    removed = queue_list.pop(position - 1)
    queue.queue = deque(queue_list)
    
    embed = discord.Embed(
        title="➖ Removed from Queue",
        description=f"Removed: **{removed['title']}**",
        color=0xFF6B6B
    )
    await ctx.send(embed=embed)

@bot.command(name='stats')
async def stats(ctx):
    """Show bot statistics"""
    queue = get_queue(ctx.guild.id)
    
    total_duration = sum(song.get('duration', 0) for song in queue.queue)
    hours = total_duration // 3600
    minutes = (total_duration % 3600) // 60
    
    embed = discord.Embed(title="📊 Music Bot Stats", color=0x1DB954)
    embed.add_field(name="🎵 Songs in Queue", value=len(queue.queue), inline=True)
    embed.add_field(name="⏱️ Total Duration", value=f"{hours}h {minutes}m", inline=True)
    embed.add_field(name="🔊 Current Volume", value=f"{int(ctx.voice_client.source.volume * 100)}%" if ctx.voice_client and ctx.voice_client.source else "N/A", inline=True)
    embed.add_field(name="📜 History", value=len(queue.history), inline=True)
    embed.add_field(name="🎧 Voice Channel", value=ctx.voice_client.channel.name if ctx.voice_client else "Not connected", inline=True)
    embed.add_field(name="👥 Listeners", value=len(ctx.voice_client.channel.members) - 1 if ctx.voice_client else 0, inline=True)
    
    await ctx.send(embed=embed)

@bot.command(name='volume')
async def volume(ctx, vol: int):
    """Change volume (0-100)"""
    if ctx.voice_client is None:
        await ctx.send("❌ Not connected to voice")
        return
    
    if 0 <= vol <= 100:
        ctx.voice_client.source.volume = vol / 100
        
        # Visual volume indicator
        bars = "█" * (vol // 10) + "░" * (10 - vol // 10)
        
        embed = discord.Embed(
            title="🔊 Volume Changed",
            description=f"**{vol}%**\n{bars}",
            color=0x1DB954
        )
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ Volume must be between 0 and 100")

@bot.command(name='np')
async def now_playing(ctx):
    """Show currently playing track with progress bar"""
    queue = get_queue(ctx.guild.id)
    
    if queue.current:
        duration = queue.current['duration'] if queue.current['duration'] else 0
        duration_min = duration // 60
        duration_sec = duration % 60
        
        # Create a visual progress bar (simplified since we don't track exact position)
        status_emoji = "▶️" if ctx.voice_client and ctx.voice_client.is_playing() else "⏸️"
        
        embed = discord.Embed(
            title=f"{status_emoji} Now Playing",
            description=f"**{queue.current['title']}**",
            color=0x1DB954
        )
        
        embed.add_field(name="⏱️ Duration", value=f"{duration_min}:{duration_sec:02d}", inline=True)
        embed.add_field(name="🔊 Volume", value=f"{int(ctx.voice_client.source.volume * 100)}%" if ctx.voice_client else "N/A", inline=True)
        embed.add_field(name="📋 Queue", value=f"{len(get_queue(ctx.guild.id).queue)} songs", inline=True)
        
        if queue.current.get('thumbnail'):
            embed.set_image(url=queue.current['thumbnail'])
        
        # Add controls hint
        embed.set_footer(text="⏯️ !pause | ⏭️ !skip | ⏮️ !previous | ⏹️ !stop")
        
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Nothing Playing",
            description="Use `!play` to start playing music!",
            color=0xFF0000
        )
        await ctx.send(embed=embed)

@bot.command(name='help_music')
async def help_music(ctx):
    """Show all available commands"""
    embed = discord.Embed(
        title="🎵 Music Bot Commands",
        description="Your personal Discord DJ!",
        color=0x1DB954
    )
    
    # Playback controls
    playback = """
    `!join` - Join your voice channel
    `!leave` - Leave voice channel
    `!play [song/url]` - Play or queue a song
    `!playnext [song]` - Add to front of queue
    `!pause` - Pause current track
    `!resume` - Resume playback
    `!skip` - Skip to next track
    `!previous` - Play previous track
    `!stop` - Stop and clear queue
    """
    embed.add_field(name="▶️ Playback", value=playback, inline=False)
    
    # Queue management
    queue_mgmt = """
    `!queue` - View current queue
    `!shuffle` - Shuffle queue
    `!clearqueue` - Clear all songs
    `!remove [#]` - Remove song by position
    """
    embed.add_field(name="📋 Queue", value=queue_mgmt, inline=False)
    
    # Info commands
    info = """
    `!np` - Now playing info
    `!stats` - Bot statistics
    `!lyrics` - Get lyrics link
    `!volume [0-100]` - Set volume
    """
    embed.add_field(name="ℹ️ Info", value=info, inline=False)
    
    embed.set_footer(text="💡 Tip: You can use Spotify playlist URLs with !play")
    embed.set_thumbnail(url="https://cdn.discordapp.com/embed/avatars/0.png")
    
    await ctx.send(embed=embed)

bot.run(DISCORD_TOKEN)
