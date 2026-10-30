"""recipe-interceptor: replies to recipe links in the food channel with a nyetcook.ing link."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from yks.bot.client import YKSBot

# Links to these sites (and their subdomains, e.g. www.) get rewritten.
RECIPE_DOMAINS: tuple[str, ...] = (
    "cooking.nytimes.com",
    "bonappetit.com",
)

PROXY_PREFIX = "nyetcook.ing/"

_URL_RE = re.compile(
    r"https?://((?:[\w-]+\.)*(?:"
    + "|".join(re.escape(domain) for domain in RECIPE_DOMAINS)
    + r"))(?![\w.-])([^\s<>]*)",
    re.IGNORECASE,
)


def rewrite_recipe_urls(content: str) -> list[str]:
    """Return a nyetcook.ing link for each recipe URL in `content`, in order, without repeats."""
    urls = [f"https://{PROXY_PREFIX}{host}{rest}" for host, rest in _URL_RE.findall(content)]
    return list(dict.fromkeys(urls))


class RecipeInterceptor(commands.Cog, name="recipe-interceptor"):
    def __init__(self, bot: YKSBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        channel_id = self.bot.settings.yks_food_channel_id
        if channel_id is None or message.channel.id != channel_id:
            return
        if message.author.bot or message.author.id in self.bot.settings.blocked_user_ids:
            return
        urls = rewrite_recipe_urls(message.content)
        if urls:
            await message.reply("\n".join(urls), mention_author=False, suppress_embeds=True)


async def setup(bot: YKSBot) -> None:
    await bot.add_cog(RecipeInterceptor(bot))
