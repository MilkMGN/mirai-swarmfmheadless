# mirai-swarmfmheadless

Utilities for relaying the Swarm FM player audio into an AES67 RTP stream (and for Discord bot consumption).

## Quick start
1. Install deps:
   - `python -m pip install playwright`
   - `python -m playwright install chromium`
   - `ffmpeg` on PATH
2. Discover the live stream URL:
   - `python stream_relay.py sniff`
3. Relay to AES67 (default multicast `rtp://239.69.0.1:5004`):
   - `python stream_relay.py relay`
   - SDP metadata is written to `aes67.sdp` for receivers.

See `DISCORD_BOT_INTEGRATION.md` for wiring the stream into a Discord bot.
