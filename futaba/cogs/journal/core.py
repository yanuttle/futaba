#
# cogs/journal/core.py
#
# futaba - A Discord Mod bot for the Programming server
# Copyright (c) 2017-2018 Jake Richardson, Ammon Smith, jackylam5
#
# futaba is available free of charge under the terms of the MIT
# License. You are free to redistribute and/or modify it under those
# terms. It is distributed in the hopes that it will be useful, but
# WITHOUT ANY WARRANTY. See the LICENSE file for more details.
#

"""
Cog for configuring Futaba journalling output, directing certain kinds
of messages to different logging channels.
"""

import asyncio
import logging
from itertools import islice
from pprint import pformat

import discord
from discord.ext import commands

from futaba import permissions
from futaba.converters import TextChannelConv
from futaba.exceptions import CommandFailed, SendHelp
from futaba.journal import ChannelOutputListener, Router
from futaba.str_builder import StringBuilder
from ..abc import AbstractCog

logger = logging.getLogger(__name__)

__all__ = ["Journal"]


class Journal(AbstractCog):
    __slots__ = ("router", "journal")

    def __init__(self, bot):
        super().__init__(bot)
        bot.journal_cog = self
        self.router = Router()
        self.journal = bot.get_broadcaster("/journal")

    def setup(self):
        logger.info("Loading journal output channels from the database")
        with self.bot.sql.transaction():
            for guild in self.bot.guilds:
                for output in self.bot.sql.journal.get_journal_channels(guild):
                    logger.info(
                        "Registering journal channel #%s (%d) for path '%s'",
                        output.channel.name,
                        output.channel.id,
                        output.path,
                    )
                    self.router.register(
                        ChannelOutputListener(self.router, output.path, output.channel)
                    )
        self.router.start(self.bot.loop)

    @commands.group(name="journal", aliases=["log"])
    async def log(self, ctx):
        """ Configure channel output for bot journal events. """

        if ctx.invoked_subcommand is None:
            raise SendHelp()

    @log.command(name="show", aliases=["display", "list"])
    @commands.guild_only()
    @permissions.check_mod()
    async def log_show(self, ctx, *channels: TextChannelConv):
        """
        Displays current settings for this guild.
        If channels are provided, then only outputs for those channels are fetched.
        """

        if channels:
            outputs = self.bot.sql.journal.get_journals_on_channels(channels)
        else:
            outputs = self.bot.sql.journal.get_journal_channels(ctx.guild)

        outputs = sorted(outputs, key=lambda out: out.path)
        attributes = []
        descr = StringBuilder()
        for output in outputs:
            if not output.settings.recursive:
                attributes.append("exact path")

            attr_str = f'({", ".join(attributes)})' if attributes else ""
            descr.writeln(
                f"- `{output.path}` mounted at {output.channel.mention} {attr_str}"
            )
            attributes.clear()

        if outputs:
            embed = discord.Embed(colour=discord.Colour.teal(), description=str(descr))
            embed.set_author(name="Current journal outputs")
        else:
            embed = discord.Embed(colour=discord.Colour.dark_purple())
            embed.set_author(name=f"No journal outputs!")

        await ctx.send(embed=embed)

    def log_updated_message(self, channel):
        outputs = list(self.bot.sql.journal.get_journals_on_channels((channel,)))
        if outputs:
            paths = " ".join(f"`{output.path}`" for output in outputs)
        else:
            paths = "(none)"
        return f"Channel outputs updated! Current journal paths: {paths}"

    @log.command(name="add", aliases=["append", "extend", "new", "set", "update"])
    @commands.guild_only()
    @permissions.check_mod()
    async def log_add(self, ctx, channel: TextChannelConv, path: str, *flags: str):
        """
        Add a journal logger to the channel for the given path.
        Accepts the optional flags:
            -exact, Don't recursively accept journal events from children.
        """

        logger.info(
            "Adding journal logger for channel #%s (%d) on path '%s'",
            channel.name,
            channel.id,
            path,
        )

        recursive = True
        for flag in flags:
            if flag == "-exact":
                recursive = False
            else:
                raise CommandFailed(content=f"No such flag: `{flag}`")

        logger.debug("Registering route")
        self.router.register(ChannelOutputListener(self.router, path, channel))

        logger.debug("Updating database for channel output")
        with self.bot.sql.transaction():
            journal_sql = self.bot.sql.journal
            if journal_sql.has_journal_channel(channel, path):
                journal_sql.update_journal_channel(channel, path, recursive)
            else:
                journal_sql.add_journal_channel(channel, path, recursive)

        await channel.send(content=self.log_updated_message(channel))
        content = f"Added journal logger to {channel.mention} for `{path}`"
        self.journal.send(
            "channel/add",
            ctx.guild,
            content,
            icon="journal",
            channel=channel,
            path=path,
            recursive=recursive,
        )

    @log.command(name="remove", aliases=["rm", "delete", "del"])
    @commands.guild_only()
    @permissions.check_mod()
    async def log_remove(self, ctx, channel: TextChannelConv, path: str):
        """
        Removes a journal logger for the given path from the channel.
        """

        logger.info(
            "Removing journal logger for channel #%s (%d) from path '%s'",
            channel.name,
            channel.id,
            path,
        )

        listener = self.router.get(path, channel=channel)
        if listener is None:
            # No listener found
            raise CommandFailed(
                content=f"No output on `{path}` found for {channel.mention}"
            )

        self.router.unregister(listener)

        with self.bot.sql.transaction():
            self.bot.sql.journal.delete_journal_channel(channel, path)

        await channel.send(content=self.log_updated_message(channel))
        content = f"Removed journal logger to {channel.mention} for `{path}`"
        self.journal.send(
            "channel/remove",
            ctx.guild,
            content,
            icon="journal",
            channel=channel,
            path=path,
        )

    @log.command(name="rename", aliases=["rn", "move", "mv"])
    @commands.guild_only()
    @permissions.check_mod()
    async def log_rename(
        self,
        ctx,
        old_channel: TextChannelConv,
        new_channel: TextChannelConv,
        path: str,
        *flags: str,
    ):
        """
        Moves a journal logger from one channel to another.
        Accepts the optional flags:
            -exact, Don't recursively accept journal events from children.
        """

        logger.info(
            "Moving journal logger from channel #%s (%d) to #%s (%d) for path '%s'",
            old_channel.name,
            old_channel.id,
            new_channel.name,
            new_channel.id,
            path,
        )

        recursive = True
        for flag in flags:
            if flag == "-exact":
                recursive = False
            else:
                raise CommandFailed(content=f"No such flag: `{flag}`")

        listener = self.router.get(path, channel=old_channel)
        if listener is None:
            # No listener found at old channel
            raise CommandFailed(
                content=f"No output on `{path}` found for {old_channel.mention}"
            )

        listener.channel = new_channel

        logger.debug("Updating database for moved channel output")
        with self.bot.sql.transaction():
            self.bot.sql.journal.delete_journal_channel(old_channel, path)
            self.bot.sql.journal.add_journal_channel(new_channel, path, recursive)

        await asyncio.gather(
            old_channel.send(content=self.log_updated_message(old_channel)),
            new_channel.send(content=self.log_updated_message(new_channel)),
        )

        content = f"Moved journal logger from {old_channel.mention} to {new_channel.mention} for `{path}`"
        self.journal.send(
            "channel/move",
            ctx.guild,
            content,
            icon="journal",
            old_channel=old_channel,
            new_channel=new_channel,
            path=path,
            recursive=recursive,
        )

    @log.command(name="send", aliases=["broadcast"])
    @commands.guild_only()
    @permissions.check_mod()
    async def log_send(self, ctx, path: str, content: str, *attributes: str):
        """
        Manually send a journal event to test logging channels.
        The content must be a single argument, wrapped in quotes if
        it has spaces, and you can specify a number of journal attributes
        in the form KEY=VALUE.
        """

        if path == "/":
            raise CommandFailed(content="Cannot broadcast on /")

        journal_attributes = {}
        for attribute in attributes:
            try:
                key, value = attribute.split("=")
                journal_attributes[key] = value
            except ValueError:
                raise CommandFailed(
                    content="All attributes must be in the form KEY=VALUE"
                )

        logger.info(
            "Sending manual journal event: '%s' (attrs: %s)",
            content,
            journal_attributes,
        )
        self.bot.get_broadcaster(path).send(
            "", ctx.guild, content, **journal_attributes
        )

    @log.command(name="find", aliases=["search"])
    @commands.guild_only()
    @permissions.check_mod()
    async def log_find(self, ctx, *, condition: str = None):
        """
        List previous journal events that match the given conditions.
        The condition is a Python expression with no variables but
        the following attributes:

        path: str
        ppath: PurePath
        guild: discord.Guild
        content: str
        attributes: dict
        """

        def create_scope(event):
            return {
                "path": str(event.path),
                "ppath": event.path,
                "guild": ctx.guild,
                "content": event.content,
                "attributes": event.attributes,
            }

        events = reversed(self.journal.history)
        events = filter(lambda evt: evt.guild == ctx.guild, events)
        events = islice(events, 20)

        if condition is None:
            matched = events
        else:
            matched = []
            for event in events:
                if eval(condition, create_scope(event)):
                    matched.append(event)

        if matched:
            embed = discord.Embed(colour=discord.Colour.dark_teal())
            embed.set_author(name="Matched journal events")
            descr = StringBuilder()
            for event in matched:
                descr.writeln(f"Path: `{event.path}`, Content: {event.content}")
                descr.writeln(f"Attributes: ```py\n{pformat(event.attributes)}\n```\n")
            embed.description = str(descr)
        else:
            embed = discord.Embed(colour=discord.Colour.dark_purple())
            embed.set_author(name="No matching journal entries")
