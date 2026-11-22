"""Optional Discord discovery interface; all file delivery stays in Telegram."""

import argparse
from datetime import UTC, datetime, timedelta
import asyncio
import logging
import os
import re

from .config import Settings
from .metadata import Metadata
from .store import Store

logger = logging.getLogger(__name__)


def configuration():
    settings = Settings.from_env(require_bot=False)
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise ValueError("Set DISCORD_TOKEN for the separate Discord bot.")
    try:
        guild_ids = {
            int(x.strip())
            for x in os.getenv("DISCORD_GUILD_IDS", "").split(",")
            if x.strip()
        }
    except ValueError:
        raise ValueError(
            "DISCORD_GUILD_IDS must be comma-separated numeric server IDs."
        ) from None
    if not guild_ids or min(guild_ids) <= 0 or len(guild_ids) > 25:
        raise ValueError("Set between one and 25 allowed DISCORD_GUILD_IDS.")
    public_catalog = (
        os.getenv("DISCORD_PUBLIC_CATALOG", "false").strip().lower() == "true"
    )
    username = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    if public_catalog and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("Public catalog links need TELEGRAM_BOT_USERNAME.")
    return settings, token, guild_ids, public_catalog, username


def create_client(settings, guild_ids, public_catalog, username):
    import discord
    from discord import app_commands

    class Client(discord.Client):
        def __init__(self):
            super().__init__(
                intents=discord.Intents.none(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.tree = app_commands.CommandTree(self)
            self.store = Store(settings)
            self.metadata = Metadata(settings)
            self.slots = asyncio.Semaphore(2)

        async def setup_hook(self):
            await self.store.connect()
            for guild_id in guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)

        async def close(self):
            await self.metadata.close()
            await self.store.close()
            await super().close()

        async def on_ready(self):
            logger.info(
                "Discord interface connected; public catalog enabled=%s", public_catalog
            )

    client = Client()

    async def authorized(interaction):
        if interaction.guild_id not in guild_ids:
            await interaction.response.send_message(
                "This interface is unavailable in this server.", ephemeral=True
            )
            return False
        return True

    async def reserve(interaction):
        if not await authorized(interaction):
            return False
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.wait_for(client.slots.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Busy right now. Please try again shortly.", ephemeral=True
            )
            return False
        return True

    @client.tree.command(
        name="movie", description="Discover movie and series information from TMDB"
    )
    @app_commands.describe(query="Title to find (maximum 160 characters)")
    @app_commands.checks.cooldown(3, 30, key=lambda interaction: interaction.user.id)
    async def movie(interaction: discord.Interaction, query: str):
        if not await reserve(interaction):
            return
        try:
            if not settings.tmdb_api_key:
                await interaction.followup.send(
                    "TMDB metadata is not configured.", ephemeral=True
                )
                return
            if not 1 <= len(query.strip()) <= 160:
                await interaction.followup.send(
                    "Enter a title between one and 160 characters.", ephemeral=True
                )
                return
            results = await client.metadata.search(query)
            if not results:
                await interaction.followup.send("No titles found.", ephemeral=True)
                return
            embeds = []
            for row in results[:5]:
                embed = discord.Embed(
                    title=row["title"][:256],
                    url=row["url"],
                    description=(row["overview"] or "No overview available.")[:700],
                    color=0x7C6AFF,
                )
                embed.add_field(name="Year", value=row["year"] or "—")
                embed.add_field(name="Rating", value=str(row["rating"]))
                if row["poster"]:
                    embed.set_thumbnail(url=row["poster"])
                embed.set_footer(
                    text="Movie data: TMDB · No media files are provided by TMDB"
                )
                embeds.append(embed)
            await interaction.followup.send(embeds=embeds, ephemeral=True)
        except Exception as exc:
            logger.warning("Discord metadata request failed: %s", type(exc).__name__)
            await interaction.followup.send(
                "Metadata is unavailable. Please try again later.", ephemeral=True
            )
        finally:
            client.slots.release()

    @client.tree.command(
        name="catalog",
        description="Find explicitly public catalog titles and open Telegram delivery",
    )
    @app_commands.describe(query="Title, optionally year:2021 lang:hindi quality:1080p")
    @app_commands.checks.cooldown(3, 30, key=lambda interaction: interaction.user.id)
    async def catalog(interaction: discord.Interaction, query: str):
        if not await reserve(interaction):
            return
        try:
            if not public_catalog:
                await interaction.followup.send(
                    "Public catalog discovery is disabled. Use /movie for metadata.",
                    ephemeral=True,
                )
                return
            if not 1 <= len(query.strip()) <= 200:
                await interaction.followup.send(
                    "Enter a title between one and 200 characters.", ephemeral=True
                )
                return
            selector = {
                "active": True,
                "public": True,
                "protected": {"$ne": True},
                "has_protected_content": {"$ne": True},
                "premium_only": {"$ne": True},
            }
            sources = (
                await client.store.db.sources.find(selector, {"_id": 1})
                .limit(1000)
                .to_list(1000)
            )
            ids = [row["_id"] for row in sources]
            results, _ = await client.store.search(query, source_ids=ids)
            entries = []
            for row in results[:5]:
                # Recheck source opt-in immediately before constructing external output.
                source = await client.store.db.sources.find_one(
                    {**selector, "_id": row["source_chat"]}, {"_id": 1}
                )
                if not source or row.get("source_protected"):
                    continue
                token = await client.store.create_link(
                    [row["_id"]], expires_at=datetime.now(UTC) + timedelta(days=7)
                )
                title = discord.utils.escape_markdown(
                    str(row.get("title") or "Untitled")[:150]
                )
                title = discord.utils.escape_mentions(title)
                detail = " · ".join(
                    str(x) for x in (row.get("year"), row.get("quality")) if x
                )
                entries.append(
                    f"**{title}** {detail}\n[Open in Telegram](https://t.me/{username}?start=link_{token})"
                )
            if not entries:
                await interaction.followup.send(
                    "No public catalog matches. Private sources are never searched here.",
                    ephemeral=True,
                )
                return
            embed = discord.Embed(
                title="Wovie · Public catalog",
                description="\n\n".join(entries),
                color=0x7C6AFF,
            )
            embed.set_footer(
                text="Telegram checks source access, membership, plans and limits before sending files."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc)[:300], ephemeral=True)
        except Exception as exc:
            logger.warning("Discord catalog request failed: %s", type(exc).__name__)
            await interaction.followup.send(
                "Catalog unavailable. Please try again later.", ephemeral=True
            )
        finally:
            client.slots.release()

    @client.tree.error
    async def command_error(interaction: discord.Interaction, error):
        message = (
            f"Slow down; try again in {max(1, int(error.retry_after))} seconds."
            if isinstance(error, app_commands.CommandOnCooldown)
            else "Unable to complete that request."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    return client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        import discord  # noqa: F401 — optional dependency, not required by the Telegram process.

        settings, token, guilds, public_catalog, username = configuration()
    except ImportError:
        parser.exit(2, "Install requirements-discord.txt in a separate environment.\n")
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    create_client(settings, guilds, public_catalog, username).run(
        token, log_handler=None
    )


if __name__ == "__main__":
    main()
