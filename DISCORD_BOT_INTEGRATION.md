## Discord Bot Integration (Python)

This note is written for an AI/maintainer to wire the relay output into a Discord bot.

### Options
- **Pull directly from Swarm FM**: let the bot open the HLS/HTTP stream with `FFmpegOpusAudio`. This skips AES67 but is the simplest.
- **Subscribe to AES67**: listen to the RTP multicast/unicast emitted by `stream_relay.py` and feed decoded PCM into Discord voice.

### Direct-from-stream approach
```python
import discord

SOURCE_URL = "https://example/stream.m3u8"  # swap with sniffed URL
FFMPEG_OPTS = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]

async def play_voice(vc: discord.VoiceClient):
    audio = discord.FFmpegOpusAudio(SOURCE_URL, before_options=" ".join(FFMPEG_OPTS))
    vc.play(audio)
```
Notes:
- Make sure `ffmpeg` is on PATH on the host running the bot.
- Use the `stream_relay.py sniff` command to discover the real URL when the site changes.

### AES67 subscription approach
1) Start the relay: `python stream_relay.py relay --target-rtp rtp://239.69.0.1:5004 --sdp-file swarmfm.sdp`
2) In the bot, point ffmpeg at the RTP endpoint (and optionally the SDP file) and pipe PCM/Opus to Discord:
```python
import discord

SDP_FILE = "swarmfm.sdp"  # keep in sync with relay

async def play_from_aes67(vc: discord.VoiceClient):
    # ffmpeg auto-joins the RTP stream using SDP; converts to 48k stereo PCM for Discord.
    audio = discord.FFmpegPCMAudio(
        SDP_FILE,
        before_options="-protocol_whitelist file,rtp,udp,tcp",
        options="-ar 48000 -ac 2 -f s16le"
    )
    vc.play(audio)
```
3) If multicast is blocked, change `--target-rtp` to a unicast address and open the firewall for UDP.

### Operational tips
- Keep the relay process supervised (systemd/pm2/forever) and restart on failure.
- If ffmpeg complains about payload type, align the bot and relay payload (`--payload-type` flag).
- Discord voice prefers ~128 kbps Opus. If you want ffmpeg to encode to Opus client-side, swap `FFmpegPCMAudio` for `FFmpegOpusAudio` and add `-c:a libopus -b:a 128k`.

