"""A single self-updating status panel for user-defined services."""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, pagify

log = logging.getLogger("red.statusmonitor")

# Service states, also used as the values stored in a service history.
UP, DEGRADED, DOWN, UNKNOWN = 1, 2, 0, -1

CELL = {UP: "\N{LARGE GREEN SQUARE}", DEGRADED: "\N{LARGE YELLOW SQUARE}",
        DOWN: "\N{LARGE RED SQUARE}", UNKNOWN: "\N{WHITE LARGE SQUARE}"}
DOT = {UP: "\N{LARGE GREEN CIRCLE}", DEGRADED: "\N{LARGE YELLOW CIRCLE}",
       DOWN: "\N{LARGE RED CIRCLE}", UNKNOWN: "\N{MEDIUM WHITE CIRCLE}"}
LABEL = {UP: "Operational", DEGRADED: "Degraded", DOWN: "Down", UNKNOWN: "Unknown"}

TICK = 20  # how often the background loop wakes up, in seconds
MAX_HISTORY = 40  # hard cap on stored checks per service
MAX_SERVICES = 20  # embeds allow 25 fields; leave room for the summary
HOST_PORT_RE = re.compile(r"^(?P<host>[A-Za-z0-9_.\-]+):(?P<port>\d{1,5})$")

DEFAULT_GUILD = {
    "channel_id": None,
    "message_id": None,
    "title": "Service Status",
    "color": 0x5865F2,
    "roles": [],
    "services": {},
    "history_len": 20,
    "interval": 60,
    "timeout": 10,
    "degraded_ms": 2000,
    "alerts": True,
    "show_links": False,
    "last_check": 0,
}


class StatusMonitor(commands.Cog):
    """Track services or links and keep one live status embed updated."""

    __author__ = "Fleeq"
    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x5354415455534D31, force_registration=True)
        self.config.register_guild(**DEFAULT_GUILD)
        self.session: Optional[aiohttp.ClientSession] = None
        self._locks: Dict[int, asyncio.Lock] = {}
        self._task = asyncio.ensure_future(self._status_loop())

    def format_help_for_context(self, ctx: commands.Context) -> str:
        return f"{super().format_help_for_context(ctx)}\n\nVersion: {self.__version__}"

    def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
        if self.session and not self.session.closed:
            asyncio.ensure_future(self.session.close())

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        """This cog stores no personal data."""
        return

    # ------------------------------------------------------------------ checks

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "Red-DiscordBot StatusMonitor/1.0"}
            )
        return self.session

    async def _check(self, svc: Dict[str, Any], timeout: int, degraded_ms: int) -> Dict[str, Any]:
        """Probe one service and return its result payload."""
        start = time.perf_counter()
        if svc.get("type") == "tcp":
            host, _, port = svc["target"].rpartition(":")
            writer = None
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, int(port)), timeout=timeout
                )
                latency = int((time.perf_counter() - start) * 1000)
                state = DEGRADED if latency > degraded_ms else UP
                return {"state": state, "latency": latency, "code": "TCP", "error": None}
            except asyncio.TimeoutError:
                return {"state": DOWN, "latency": None, "code": None, "error": "Timed out"}
            except Exception as exc:  # refused, DNS failure, bad port...
                return {"state": DOWN, "latency": None, "code": None, "error": _short(exc)}
            finally:
                if writer is not None:
                    writer.close()

        session = await self._get_session()
        try:
            async with session.get(
                svc["target"],
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as resp:
                latency = int((time.perf_counter() - start) * 1000)
                expect: List[int] = svc.get("expect") or []
                ok = resp.status in expect if expect else 200 <= resp.status < 400
                if not ok:
                    return {
                        "state": DOWN,
                        "latency": latency,
                        "code": resp.status,
                        "error": f"HTTP {resp.status}",
                    }
                state = DEGRADED if latency > degraded_ms else UP
                return {"state": state, "latency": latency, "code": resp.status, "error": None}
        except asyncio.TimeoutError:
            return {"state": DOWN, "latency": None, "code": None, "error": "Timed out"}
        except aiohttp.ClientError as exc:
            return {"state": DOWN, "latency": None, "code": None, "error": _short(exc)}
        except Exception as exc:
            log.debug("Unexpected error checking %s", svc.get("target"), exc_info=True)
            return {"state": DOWN, "latency": None, "code": None, "error": _short(exc)}

    # -------------------------------------------------------------- background

    async def _status_loop(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("StatusMonitor update loop errored")
            await asyncio.sleep(TICK)

    async def _tick(self) -> None:
        all_guilds = await self.config.all_guilds()
        now = time.time()
        for guild_id, data in all_guilds.items():
            if not data.get("services") or not data.get("channel_id"):
                continue
            if now - data.get("last_check", 0) < data.get("interval", 60) - 1:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None or await self.bot.cog_disabled_in_guild(self, guild):
                continue
            try:
                await self.update_guild(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Failed updating status panel for guild %s", guild_id)

    async def update_guild(self, guild: discord.Guild) -> Optional[str]:
        """Run every check for a guild and edit (or post) its panel.

        Returns an error string when the panel could not be delivered.
        """
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            conf = self.config.guild(guild)
            data = await conf.all()
            services: Dict[str, Any] = data["services"]
            if not services:
                return "No services configured."
            channel = guild.get_channel(data["channel_id"]) if data["channel_id"] else None
            if channel is None:
                return "The status channel is not set or no longer exists."

            sem = asyncio.Semaphore(10)

            async def run(key: str, svc: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
                async with sem:
                    return key, await self._check(svc, data["timeout"], data["degraded_ms"])

            results = dict(await asyncio.gather(*(run(k, v) for k, v in services.items())))

            changed: List[Tuple[str, int, int]] = []
            async with conf.services() as stored:
                for key, res in results.items():
                    svc = stored.get(key)
                    if svc is None:  # removed while we were probing
                        continue
                    previous = (svc.get("last") or {}).get("state", UNKNOWN)
                    if previous != UNKNOWN and previous != res["state"]:
                        changed.append((svc.get("name", key), previous, res["state"]))
                    history = list(svc.get("history", []))
                    history.append(res["state"])
                    svc["history"] = history[-MAX_HISTORY:]
                    svc["last"] = res
                data["services"] = {k: dict(v) for k, v in stored.items()}
            await conf.last_check.set(time.time())

            error = await self._publish(guild, channel, data)
            if not error and data["alerts"] and changed:
                await self._send_alerts(channel, data, changed)
            return error

    async def _publish(
        self, guild: discord.Guild, channel: discord.abc.GuildChannel, data: Dict[str, Any]
    ) -> Optional[str]:
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return f"I need Send Messages and Embed Links in {channel.mention}."

        content = self._content(guild, data)
        embed = self._embed(guild, data)

        if data["message_id"]:
            message = channel.get_partial_message(data["message_id"])
            try:
                await message.edit(
                    content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
                return None
            except discord.NotFound:
                pass  # panel was deleted, fall through and post a fresh one
            except discord.Forbidden:
                return f"I cannot edit my panel in {channel.mention}."

        try:
            message = await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
            )
        except discord.Forbidden:
            return f"I cannot post in {channel.mention}."
        await self.config.guild(guild).message_id.set(message.id)
        return None

    async def _send_alerts(
        self, channel: discord.abc.GuildChannel, data: Dict[str, Any], changed: List[Tuple[str, int, int]]
    ) -> None:
        mentions = " ".join(f"<@&{r}>" for r in data["roles"])
        lines = [
            f"{DOT[new]} **{name}** is now **{LABEL[new].lower()}** (was {LABEL[old].lower()})"
            for name, old, new in changed
        ]
        try:
            await channel.send(
                f"{mentions}\n" + "\n".join(lines) if mentions else "\n".join(lines),
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
            )
        except discord.HTTPException:
            log.debug("Could not send status alert", exc_info=True)

    # ---------------------------------------------------------------- renderer

    def _content(self, guild: discord.Guild, data: Dict[str, Any]) -> Optional[str]:
        roles = [guild.get_role(r) for r in data["roles"]]
        mentions = " ".join(r.mention for r in roles if r is not None)
        return mentions or None

    def _embed(self, guild: discord.Guild, data: Dict[str, Any]) -> discord.Embed:
        services: Dict[str, Any] = data["services"]
        length = data["history_len"]
        states = [(s.get("last") or {}).get("state", UNKNOWN) for s in services.values()]
        down = states.count(DOWN)
        degraded = states.count(DEGRADED)

        if down:
            summary = f"{DOT[DOWN]} **{down}** of **{len(states)}** services down"
        elif degraded:
            summary = f"{DOT[DEGRADED]} **{degraded}** of **{len(states)}** services degraded"
        elif states and all(s == UNKNOWN for s in states):
            summary = f"{DOT[UNKNOWN]} Waiting for the first check"
        else:
            summary = f"{DOT[UP]} All systems operational"

        embed = discord.Embed(
            title=data["title"],
            colour=discord.Colour(data["color"]),
            description=f"{summary}\nLast check: <t:{int(time.time())}:R>",
            timestamp=datetime.now(timezone.utc),
        )
        for svc in services.values():
            last = svc.get("last") or {}
            state = last.get("state", UNKNOWN)
            name = f"{DOT[state]}  {svc.get('name', '?')}"
            detail = LABEL[state]
            if last.get("error"):
                detail = f"{LABEL[state]} \N{BULLET} {last['error']}"
            elif last.get("latency") is not None:
                detail = f"{LABEL[state]} \N{BULLET} {last['latency']}ms"
            value = self._bar(svc.get("history", []), length)
            value += f"\n`{_uptime(svc.get('history', []))}` uptime \N{BULLET} {detail}"
            if data["show_links"] and svc.get("type") == "http":
                value += f"\n[{_hostname(svc['target'])}]({svc['target']})"
            embed.add_field(name=name[:256], value=value[:1024], inline=False)

        embed.set_footer(text=f"Checked every {data['interval']}s \N{BULLET} {len(services)} service(s)")
        return embed

    @staticmethod
    def _bar(history: List[int], length: int) -> str:
        recent = history[-length:]
        padding = [UNKNOWN] * max(0, length - len(recent))
        return "".join(CELL[s] for s in padding + recent)

    # ---------------------------------------------------------------- commands

    @commands.guild_only()
    @commands.group(name="statusmon", aliases=["stm"])
    @commands.admin_or_permissions(manage_guild=True)
    async def statusmon(self, ctx: commands.Context) -> None:
        """Configure the live service status panel."""

    @statusmon.command(name="add")
    async def stm_add(self, ctx: commands.Context, name: str, target: str) -> None:
        """Add a service to monitor.

        `target` is either a link (`https://example.com/health`) or a
        `host:port` pair for a plain TCP check (`db.example.com:5432`).
        Quote names that contain spaces: `[p]statusmon add "My API" https://api.example.com`
        """
        target = target.strip("<>")
        if target.lower().startswith(("http://", "https://")):
            kind = "http"
        elif HOST_PORT_RE.match(target):
            if not 0 < int(HOST_PORT_RE.match(target).group("port")) < 65536:
                await ctx.send("That port number is not valid.")
                return
            kind = "tcp"
        else:
            await ctx.send(
                "That target is not valid. Use a full link (`https://example.com`) "
                "or a `host:port` pair (`example.com:443`)."
            )
            return

        key = name.lower()
        async with self.config.guild(ctx.guild).services() as services:
            if key in services:
                await ctx.send(f"`{name}` is already being monitored.")
                return
            if len(services) >= MAX_SERVICES:
                await ctx.send(f"You can monitor at most {MAX_SERVICES} services.")
                return
            services[key] = {
                "name": name,
                "target": target,
                "type": kind,
                "expect": [],
                "history": [],
                "last": None,
            }
        await ctx.send(f"Added **{name}** (`{target}`). Refreshing the panel...")
        await self._refresh_and_report(ctx)

    @statusmon.command(name="remove", aliases=["delete", "del"])
    async def stm_remove(self, ctx: commands.Context, *, name: str) -> None:
        """Stop monitoring a service."""
        async with self.config.guild(ctx.guild).services() as services:
            removed = services.pop(name.lower(), None)
        if removed is None:
            await ctx.send(f"`{name}` is not being monitored.")
            return
        await ctx.send(f"Removed **{removed['name']}**.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="list")
    async def stm_list(self, ctx: commands.Context) -> None:
        """List the monitored services."""
        services = await self.config.guild(ctx.guild).services()
        if not services:
            await ctx.send("No services configured yet. Add one with `[p]statusmon add`.")
            return
        rows = []
        for svc in services.values():
            state = (svc.get("last") or {}).get("state", UNKNOWN)
            rows.append(
                f"{LABEL[state]:<12} {svc['name']:<24} {svc['target']}"
                + (f"  (expects {', '.join(map(str, svc['expect']))})" if svc.get("expect") else "")
            )
        for page in pagify("\n".join(rows), shorten_by=12):
            await ctx.send(box(page, lang="less"))

    @statusmon.command(name="channel")
    async def stm_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel the status panel lives in.

        Moving the panel posts a new message in the new channel.
        """
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.send(f"I need Send Messages and Embed Links in {channel.mention}.")
            return
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await self.config.guild(ctx.guild).message_id.set(None)
        await ctx.send(f"The status panel will be posted in {channel.mention}.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="roles")
    async def stm_roles(self, ctx: commands.Context, *roles: discord.Role) -> None:
        """Set the roles mentioned on the panel. Run with no roles to clear them."""
        await self.config.guild(ctx.guild).roles.set([r.id for r in roles])
        if roles:
            await ctx.send(
                "Panel will mention: "
                + humanize_list([r.name for r in roles])
                + ".\nNote: Discord only notifies on the first post, since later "
                "updates are edits. Use `[p]statusmon alerts on` for real pings when "
                "a service changes state."
            )
        else:
            await ctx.send("Cleared the mentioned roles.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="color", aliases=["colour"])
    async def stm_color(self, ctx: commands.Context, colour: discord.Colour) -> None:
        """Set the embed colour, e.g. `#00b894`, `0x00b894` or `blurple`."""
        await self.config.guild(ctx.guild).color.set(colour.value)
        await ctx.send(f"Embed colour set to `#{colour.value:06X}`.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="title")
    async def stm_title(self, ctx: commands.Context, *, title: str) -> None:
        """Set the panel title."""
        await self.config.guild(ctx.guild).title.set(title[:256])
        await ctx.send(f"Title set to **{title[:256]}**.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="history")
    async def stm_history(self, ctx: commands.Context, length: int) -> None:
        """Set how many past checks the status bar shows (5-40)."""
        if not 5 <= length <= MAX_HISTORY:
            await ctx.send(f"Pick a length between 5 and {MAX_HISTORY}.")
            return
        await self.config.guild(ctx.guild).history_len.set(length)
        await ctx.send(f"The status bar now shows the last {length} checks.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="interval")
    async def stm_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often services are checked (60-3600 seconds)."""
        if not 60 <= seconds <= 3600:
            await ctx.send("Pick an interval between 60 and 3600 seconds.")
            return
        await self.config.guild(ctx.guild).interval.set(seconds)
        await ctx.send(f"Services will be checked every {seconds} seconds.")

    @statusmon.command(name="timeout")
    async def stm_timeout(self, ctx: commands.Context, seconds: int) -> None:
        """Set how long a check waits before it counts as down (1-30 seconds)."""
        if not 1 <= seconds <= 30:
            await ctx.send("Pick a timeout between 1 and 30 seconds.")
            return
        await self.config.guild(ctx.guild).timeout.set(seconds)
        await ctx.send(f"Checks now time out after {seconds} seconds.")

    @statusmon.command(name="slow")
    async def stm_slow(self, ctx: commands.Context, milliseconds: int) -> None:
        """Set the latency above which a service counts as degraded (yellow)."""
        if not 100 <= milliseconds <= 30000:
            await ctx.send("Pick a threshold between 100 and 30000 milliseconds.")
            return
        await self.config.guild(ctx.guild).degraded_ms.set(milliseconds)
        await ctx.send(f"Responses slower than {milliseconds}ms now count as degraded.")

    @statusmon.command(name="expect")
    async def stm_expect(self, ctx: commands.Context, name: str, *codes: int) -> None:
        """Set which HTTP codes count as up for a service.

        Run with no codes to go back to the default (any 2xx or 3xx).
        """
        async with self.config.guild(ctx.guild).services() as services:
            svc = services.get(name.lower())
            if svc is None:
                await ctx.send(f"`{name}` is not being monitored.")
                return
            if svc["type"] != "http":
                await ctx.send("Expected codes only apply to link checks.")
                return
            svc["expect"] = [c for c in codes if 100 <= c <= 599]
        if codes:
            await ctx.send(f"**{name}** is up when it returns {humanize_list([f'`{c}`' for c in codes])}.")
        else:
            await ctx.send(f"**{name}** is up on any 2xx or 3xx response.")

    @statusmon.command(name="alerts")
    async def stm_alerts(self, ctx: commands.Context, on_off: bool) -> None:
        """Post a separate ping message when a service changes state."""
        await self.config.guild(ctx.guild).alerts.set(on_off)
        await ctx.send(
            "State-change alerts are now **on**. They are posted as extra messages "
            "next to the panel, so the roles actually get notified."
            if on_off
            else "State-change alerts are now **off**."
        )

    @statusmon.command(name="links")
    async def stm_links(self, ctx: commands.Context, on_off: bool) -> None:
        """Show or hide each service's link on the panel."""
        await self.config.guild(ctx.guild).show_links.set(on_off)
        await ctx.send(f"Links are now **{'shown' if on_off else 'hidden'}** on the panel.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="post")
    async def stm_post(self, ctx: commands.Context) -> None:
        """Post a fresh panel, replacing the old one."""
        data = await self.config.guild(ctx.guild).all()
        if not data["channel_id"]:
            await ctx.send("Set a channel first with `[p]statusmon channel #channel`.")
            return
        if not data["services"]:
            await ctx.send("Add a service first with `[p]statusmon add`.")
            return
        await self.config.guild(ctx.guild).message_id.set(None)
        await self._refresh_and_report(ctx)

    @statusmon.command(name="refresh")
    async def stm_refresh(self, ctx: commands.Context) -> None:
        """Check every service right now and update the panel."""
        await self._refresh_and_report(ctx)

    @statusmon.command(name="clearhistory")
    async def stm_clearhistory(self, ctx: commands.Context) -> None:
        """Wipe the recorded history of every service."""
        async with self.config.guild(ctx.guild).services() as services:
            for svc in services.values():
                svc["history"] = []
                svc["last"] = None
        await ctx.send("History cleared.")
        await self._refresh_and_report(ctx, silent_when_empty=True)

    @statusmon.command(name="settings", aliases=["show"])
    async def stm_settings(self, ctx: commands.Context) -> None:
        """Show the current configuration."""
        data = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(data["channel_id"]) if data["channel_id"] else None
        roles = [ctx.guild.get_role(r) for r in data["roles"]]
        role_names = humanize_list([r.name for r in roles if r]) or "none"
        embed = discord.Embed(
            title="StatusMonitor settings",
            colour=discord.Colour(data["color"]),
            description=(
                f"**Channel:** {channel.mention if channel else 'not set'}\n"
                f"**Panel title:** {data['title']}\n"
                f"**Colour:** `#{data['color']:06X}`\n"
                f"**Mentioned roles:** {role_names}\n"
                f"**Check interval:** {data['interval']}s\n"
                f"**Timeout:** {data['timeout']}s\n"
                f"**Degraded above:** {data['degraded_ms']}ms\n"
                f"**Status bar length:** {data['history_len']} checks\n"
                f"**Change alerts:** {'on' if data['alerts'] else 'off'}\n"
                f"**Show links:** {'on' if data['show_links'] else 'off'}\n"
                f"**Services:** {len(data['services'])}"
            ),
        )
        await ctx.send(embed=embed)

    async def _refresh_and_report(
        self, ctx: commands.Context, silent_when_empty: bool = False
    ) -> None:
        data = await self.config.guild(ctx.guild).all()
        if not data["services"] or not data["channel_id"]:
            if not silent_when_empty:
                await ctx.send(
                    "Nothing to show yet - set a channel with `[p]statusmon channel` "
                    "and add a service with `[p]statusmon add`."
                )
            return
        error = await self.update_guild(ctx.guild)
        if error:
            await ctx.send(error)


def _uptime(history: List[int]) -> str:
    known = [s for s in history if s != UNKNOWN]
    if not known:
        return "  --  "
    ratio = sum(1 for s in known if s in (UP, DEGRADED)) / len(known)
    return f"{ratio * 100:5.1f}%"


def _hostname(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def _short(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return text[:80]
