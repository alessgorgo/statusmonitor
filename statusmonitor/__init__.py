import inspect

from .statusmonitor import StatusMonitor

__red_end_user_data_statement__ = (
    "This cog stores server configuration (channel, roles, colour) and the "
    "reachability history of the services an administrator adds. It stores no "
    "personal data about end users."
)


async def setup(bot):
    cog = StatusMonitor(bot)
    result = bot.add_cog(cog)
    if inspect.isawaitable(result):  # Red 3.5 / discord.py 2.x
        await result
