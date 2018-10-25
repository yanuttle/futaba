#
# cogs/filter/check/file.py
#
# futaba - A Discord Mod bot for the Programming server
# Copyright (c) 2017-2018 Jake Richardson, Ammon Smith, jackylam5
#
# futaba is available free of charge under the terms of the MIT
# License. You are free to redistribute and/or modify it under those
# terms. It is distributed in the hopes that it will be useful, but
# WITHOUT ANY WARRANTY. See the LICENSE file for more details.
#

import asyncio
import logging
import os
from collections import namedtuple
from hashlib import sha1
from urllib.parse import urlparse

import discord

from futaba.download import download_links
from futaba.enums import FilterType
from futaba.str_builder import StringBuilder
from futaba.utils import URL_REGEX
from .common import journal_violation

logger = logging.getLogger(__name__)

__all__ = ["FoundFileViolation", "check_file_filter"]


FoundFileViolation = namedtuple(
    "FoundFileViolation",
    ("journal", "message", "filter_type", "url", "binio", "hashsum"),
)


async def check_file_filter(cog, message):
    file_urls = URL_REGEX.findall(message.content)
    file_urls.extend(attach.url for attach in message.attachments)

    if not file_urls:
        logger.debug("Message has no attachments, skipping")
        return

    triggered = None
    buffers = await download_links(file_urls)
    hashsums = {}

    for binio, url in zip(buffers, file_urls):
        if binio is not None:
            digest = sha1(binio.getbuffer()).digest()
            hashsums[digest] = (binio, url)

    for hashsum, filter_type in cog.content_filters[message.guild].items():
        try:
            binio, url = hashsums[hashsum]
        except KeyError:
            # Hash sum not found, not a match
            continue

        if triggered is None or filter_type.value > triggered.filter_type.value:
            triggered = FoundFileViolation(
                journal=cog.journal,
                message=message,
                filter_type=filter_type,
                url=url,
                binio=binio,
                hashsum=hashsum,
            )

    if triggered is None:
        logger.debug("No content violations found!")
    else:
        roles = cog.bot.sql.settings.get_special_roles(message.guild)
        settings = cog.bot.sql.filter.get_settings(message.guild)
        await found_file_violation(triggered, roles, settings.reupload)


async def found_file_violation(roles, triggered, reupload):
    """
    Processes a violation of the file content filter. This coroutine is responsible
    for actual enforcement, based on the filter_type.
    """

    journal = triggered.journal
    message = triggered.message
    filter_type = triggered.filter_type
    url = triggered.url
    binio = triggered.binio
    hashsum = triggered.hashsum

    logger.info(
        "Punishing file content filter violation (%s, level %s) by '%s' (%d)",
        hashsum.hex(),
        filter_type.value,
        message.author.name,
        message.author.id,
    )

    severity = filter_type.level

    async def message_violator():
        logger.debug("Sending message to user who violated the filter")
        response = StringBuilder()
        response.write(
            f"The message you posted in {message.channel.mention} contains or links to a file "
        )
        response.writeln(
            f"that violates a {filter_type.value} content filter: `{hashsum.hex()}`."
        )
        response.writeln(f"Your original link: <{url}>")

        if reupload:
            response.writeln("The filtered file has been attached to this message.")

        if severity >= FilterType.JAIL.level:
            if roles.jail is not None:
                response.writeln(
                    "This offense is serious enough to warrant immediate revocation of posting privileges.\n"
                    f"As such, you have been assigned the `{roles.jail.name}` role, until a moderator clears you."
                )

        kwargs = {}
        if reupload:
            response.writeln(
                "In case the link is broken, the file has been attached below:"
            )
            filename = os.path.basename(urlparse(url).path)
            kwargs["file"] = discord.File(binio.getbuffer(), filename=filename)

        kwargs["content"] = str(response)
        await message.author.send(**kwargs)

    if severity >= FilterType.FLAG.level:
        logger.info("Notifying staff of filter violation")
        journal_violation(journal, "file", message, filter_type, url)

    if severity >= FilterType.BLOCK.level:
        logger.info(
            "Deleting inappropriate message id %d and notifying user", message.id
        )
        await asyncio.gather(message.delete(), message_violator())

    if severity >= FilterType.JAIL.level:

        if roles.jail is None:
            logger.info(
                "Jailing user for inappropriate file, except there is no jail role configured!"
            )
            content = f"Cannot jail {message.author.mention} for filter violation because no jail role is set!"
            journal.send("file/jail", message.guild, content, icon="warning")
        else:
            logger.info("Jailing user for inappropriate file")
            await message.author.add_roles(
                roles.jail, reason="Jailed for violating file filter"
            )