import asyncio
import os
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

if not DISCORD_TOKEN:
    raise ValueError("Missing DISCORD_TOKEN in environment variables.")
if not GUILD_ID:
    raise ValueError("Missing or invalid GUILD_ID in environment variables.")

ALLOWED_ROLES = [
    "Owner",
    "Developer",
    "Community Manager",
    "Community Helper",
]

API_GROUPS = []

key_index = 1
while True:
    api_key = os.getenv("ROBLOX_API_KEY_{}".format(key_index))
    if not api_key:
        break

    universe_ids = []
    uid_index = 1
    while True:
        uid = os.getenv("UNIVERSE_ID_{}_{}".format(key_index, uid_index))
        if not uid:
            break
        universe_ids.append(uid)
        uid_index += 1

    if universe_ids:
        API_GROUPS.append({"api_key": api_key, "universe_ids": universe_ids})
    else:
        print("Warning: ROBLOX_API_KEY_{} has no universe IDs, skipping.".format(key_index))

    key_index += 1


if not API_GROUPS:
    legacy_key = os.getenv("ROBLOX_API_KEY")
    if legacy_key:
        legacy_ids = []
        i = 1
        while True:
            uid = os.getenv("UNIVERSE_ID_{}".format(i), "")
            if not uid:
                break
            legacy_ids.append(uid)
            i += 1
        if legacy_ids:
            API_GROUPS.append({"api_key": legacy_key, "universe_ids": legacy_ids})
            print("Loaded legacy config: 1 API key, {} universe(s).".format(len(legacy_ids)))

if not API_GROUPS:
    raise ValueError("No API groups found. Define ROBLOX_API_KEY_1 + UNIVERSE_ID_1_1 in .env")

ALL_UNIVERSE_IDS = [uid for group in API_GROUPS for uid in group["universe_ids"]]

UNIVERSE_API_KEY = {}
for group in API_GROUPS:
    for uid in group["universe_ids"]:
        UNIVERSE_API_KEY[uid] = group["api_key"]

TAG_CLOSED = int(os.getenv("TAG_CLOSED", "0"))
TAG_BANNED = int(os.getenv("TAG_BANNED", "0"))
TAG_REVIEWED = int(os.getenv("TAG_REVIEWED", "0"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def has_allowed_role(member):
    return any(role.name in ALLOWED_ROLES for role in getattr(member, "roles", []))


def trim_embed_value(text, limit=1024):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def chunk_lines(lines, limit=1024):
    chunks = []
    current = []
    for line in lines:
        candidate = "\n".join(current + [line])
        if len(candidate) > limit and current:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def restriction_user_id(restriction):
    path = restriction.get("path", "")
    if path:
        return path.rstrip("/").split("/")[-1]
    user = restriction.get("user")
    if isinstance(user, str) and "/" in user:
        return user.rstrip("/").split("/")[-1]
    user_restriction_id = restriction.get("userRestrictionId")
    if user_restriction_id:
        return str(user_restriction_id)
    return None


async def get_roblox_user_info(session, user_id):
    url = "https://users.roblox.com/v1/users/{}".format(user_id)
    async with session.get(url) as resp:
        if resp.status == 200:
            return await resp.json()
    return None


async def get_roblox_user_avatar(session, user_id):
    url = "https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={}&size=150x150&format=Png".format(user_id)
    async with session.get(url) as resp:
        if resp.status == 200:
            data = await resp.json()
            items = data.get("data", [])
            if items:
                return items[0].get("imageUrl")
    return None


async def get_roblox_friends_count(session, user_id):
    url = "https://friends.roblox.com/v1/users/{}/friends/count".format(user_id)
    async with session.get(url) as resp:
        if resp.status == 200:
            return (await resp.json()).get("count", 0)
    return 0


async def get_roblox_followers_count(session, user_id):
    url = "https://friends.roblox.com/v1/users/{}/followers/count".format(user_id)
    async with session.get(url) as resp:
        if resp.status == 200:
            return (await resp.json()).get("count", 0)
    return 0


async def get_roblox_following_count(session, user_id):
    url = "https://friends.roblox.com/v1/users/{}/followings/count".format(user_id)
    async with session.get(url) as resp:
        if resp.status == 200:
            return (await resp.json()).get("count", 0)
    return 0


async def get_user_id_by_name(session, username):
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": False}
    async with session.post(url, json=payload) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        users = data.get("data", [])
        return users[0]["id"] if users else None


async def get_universe_info(session, universe_id):
    url = "https://develop.roblox.com/v1/universes/{}".format(universe_id)
    async with session.get(url) as resp:
        if resp.status != 200:
            return {
                "name": "Universe {}".format(universe_id),
                "url": "https://www.roblox.com/discover#/",
            }
        data = await resp.json()
        name = data.get("name") or "Universe {}".format(universe_id)
        root_place_id = data.get("rootPlaceId")
        if root_place_id:
            url = "https://www.roblox.com/games/{}".format(root_place_id)
        else:
            url = "https://www.roblox.com/discover#/"
        return {"name": name, "url": url}


async def list_user_restrictions(session, universe_id, api_key):
    url = "https://apis.roblox.com/cloud/v2/universes/{}/user-restrictions".format(universe_id)
    headers = {"x-api-key": api_key}
    restrictions = []
    page_token = None
    while True:
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                return None, await resp.text()
            data = await resp.json()
            restrictions.extend(data.get("userRestrictions", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return restrictions, None


async def get_user_restriction(session, user_id, universe_id):
    api_key = UNIVERSE_API_KEY[universe_id]
    url = "https://apis.roblox.com/cloud/v2/universes/{}/user-restrictions/{}".format(
        universe_id, user_id
    )
    headers = {"x-api-key": api_key}
    async with session.get(url, headers=headers) as resp:
        if resp.status == 200:
            return await resp.json()
        return None


async def ban_in_universe(session, user_id, reason, duration_seconds, universe_id):
    api_key = UNIVERSE_API_KEY[universe_id]
    url = "https://apis.roblox.com/cloud/v2/universes/{}/user-restrictions/{}".format(
        universe_id, user_id
    )
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    restriction = {
        "active": True,
        "privateReason": reason or "Discord report.",
        "displayReason": reason or "You have been banned from Murder Mystery 2.",
        "excludeAltAccounts": False,
        "duration": "{}s".format(duration_seconds) if duration_seconds is not None else None,
    }
    async with session.patch(url, headers=headers, json={"gameJoinRestriction": restriction}) as resp:
        if resp.status in (200, 201):
            return True, None
        return False, await resp.text()


async def unban_in_universe(session, user_id, universe_id):
    api_key = UNIVERSE_API_KEY[universe_id]
    url = "https://apis.roblox.com/cloud/v2/universes/{}/user-restrictions/{}".format(
        universe_id, user_id
    )
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    restriction = {
        "active": False,
        "privateReason": "",
        "displayReason": "",
        "excludeAltAccounts": False,
        "duration": None,
    }
    async with session.patch(url, headers=headers, json={"gameJoinRestriction": restriction}) as resp:
        if resp.status in (200, 201):
            return True, None
        return False, await resp.text()


async def apply_restriction_in_universe(session, user_id, restriction, universe_id):
    api_key = UNIVERSE_API_KEY[universe_id]
    url = "https://apis.roblox.com/cloud/v2/universes/{}/user-restrictions/{}".format(
        universe_id, user_id
    )
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    async with session.patch(url, headers=headers, json={"gameJoinRestriction": restriction}) as resp:
        if resp.status in (200, 201):
            return True, None
        return False, await resp.text()


async def fetch_user_data(session, user_id):
    user_info = await get_roblox_user_info(session, user_id)
    avatar_url = await get_roblox_user_avatar(session, user_id)
    friends = await get_roblox_friends_count(session, user_id)
    followers = await get_roblox_followers_count(session, user_id)
    following = await get_roblox_following_count(session, user_id)
    username = user_info.get("name", "Unknown") if user_info else "Unknown"
    display_name = user_info.get("displayName", username) if user_info else username
    return username, display_name, avatar_url, friends, followers, following


def build_user_embed(user_id, display_name, username, avatar_url, friends, followers, following, color):
    profile_url = "https://www.roblox.com/users/{}/profile".format(user_id)
    friends_url = "https://www.roblox.com/users/{}/friends".format(user_id)
    followers_url = "https://www.roblox.com/users/{}/followers".format(user_id)
    following_url = "https://www.roblox.com/users/{}/following".format(user_id)
    desc = "[**{}**]({}) Friends **|** [**{:,}**]({}) Followers **|** [**{}**]({}) Following".format(
        friends, friends_url, followers, followers_url, following, following_url
    )
    embed = discord.Embed(
        title="**{} (@{})**".format(display_name, username),
        url=profile_url,
        description=desc,
        timestamp=datetime.now(timezone.utc),
        color=color,
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="ID: {}".format(user_id))
    return embed


class ConfirmView(discord.ui.View):
    def __init__(self, action, owner_id):
        super().__init__(timeout=30)
        self.action = action
        self.owner_id = owner_id
        self.confirmed = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is not your action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="{} cancelled.".format(self.action.capitalize()),
            view=self,
        )
        self.stop()


@bot.event
async def on_ready():
    print("Bot ready: {}".format(bot.user))
    print("Loaded {} API group(s), {} universe(s) total.".format(len(API_GROUPS), len(ALL_UNIVERSE_IDS)))
    for i, group in enumerate(API_GROUPS, 1):
        print("  Group {}: {} universe(s) → {}".format(i, len(group["universe_ids"]), group["universe_ids"]))
    guild_obj = discord.Object(id=GUILD_ID)
    synced = await bot.tree.sync(guild=guild_obj)
    print("Synced {} command(s) to guild {}.".format(len(synced), GUILD_ID))


guild = discord.Object(id=GUILD_ID)


@bot.tree.command(
    name="unban",
    description="Unban a Roblox player from your game",
    guild=guild,
)
@app_commands.describe(username="Player Roblox username")
async def unban_command(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    if not has_allowed_role(interaction.user):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        user_id = await get_user_id_by_name(session, username)
        if not user_id:
            await interaction.followup.send("User **{}** was not found on Roblox.".format(username), ephemeral=True)
            return

        fetched_username, display_name, avatar_url, friends, followers, following = await fetch_user_data(session, user_id)

        profile_url = "https://www.roblox.com/users/{}/profile".format(user_id)
        confirm_embed = discord.Embed(
            title="Unban confirmation",
            description="Are you sure you want to unban [**{} (@{})**]({})?\n-# This action will expire in 30 seconds.".format(display_name, fetched_username, profile_url),
            color=0xFEE75C,
            timestamp=datetime.now(timezone.utc),
        )
        if avatar_url:
            confirm_embed.set_thumbnail(url=avatar_url)

        view = ConfirmView("unban", interaction.user.id)
        await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        results = []
        for uid in ALL_UNIVERSE_IDS:
            ok, err = await unban_in_universe(session, user_id, uid)
            results.append((uid, ok, err))

        failed = [(uid, err) for uid, ok, err in results if not ok]

        embed = build_user_embed(
            user_id, display_name, fetched_username, avatar_url,
            friends, followers, following,
            0x57F287 if not failed else 0xE74C3C,
        )
        embed.add_field(name="🛡 Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(
            name="🎮 Places",
            value="{}/{} unbanned".format(len(results) - len(failed), len(results)),
            inline=True,
        )
        if failed:
            embed.add_field(
                name="⚠️ Failed",
                value=trim_embed_value("\n".join(["Universe `{}`: {}".format(uid, err) for uid, err in failed])),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=False)


@bot.tree.command(
    name="ban",
    description="Permanently ban a Roblox exploiter from your game",
    guild=guild,
)
@app_commands.describe(
    username="Player Roblox username",
    reason="Optional custom ban reason (shown to the player)",
    evidence="Optional link to forum post or evidence",
)
async def ban_command(
    interaction: discord.Interaction,
    username: str,
    reason: str = None,
    evidence: str = None,
):
    await interaction.response.defer(ephemeral=True)
    if not has_allowed_role(interaction.user):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        uid_int = await get_user_id_by_name(session, username)
        if not uid_int:
            await interaction.followup.send("User **{}** was not found on Roblox.".format(username), ephemeral=True)
            return

        fetched_username, display_name, avatar_url, friends, followers, following = await fetch_user_data(session, uid_int)

        profile_url = "https://www.roblox.com/users/{}/profile".format(uid_int)
        confirm_embed = discord.Embed(
            title="Ban confirmation",
            description="Are you sure you want to ban [**{} (@{})**]({})?\n-# This action will expire in 30 seconds.".format(display_name, fetched_username, profile_url),
            color=0xFEE75C,
            timestamp=datetime.now(timezone.utc),
        )
        if avatar_url:
            confirm_embed.set_thumbnail(url=avatar_url)
        if reason:
            confirm_embed.add_field(name="Reason", value=reason, inline=False)
        if evidence:
            confirm_embed.add_field(name="Evidence", value=evidence, inline=False)

        view = ConfirmView("ban", interaction.user.id)
        await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        results = []
        for uid in ALL_UNIVERSE_IDS:
            ok, err = await ban_in_universe(session, uid_int, reason, None, uid)
            results.append((uid, ok, err))

        failed = [(uid, err) for uid, ok, err in results if not ok]

        embed = build_user_embed(
            uid_int, display_name, fetched_username, avatar_url,
            friends, followers, following,
            0x99AAB5 if not failed else 0xE74C3C,
        )
        embed.add_field(name="⏱ Duration", value="Permanent", inline=True)
        embed.add_field(name="🛡 Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(
            name="🎮 Places",
            value="{}/{} banned".format(len(results) - len(failed), len(results)),
            inline=True,
        )
        if reason:
            embed.add_field(name="📋 Reason", value=reason, inline=False)
        if evidence:
            embed.add_field(name="🔗 Proof", value=evidence, inline=False)
        if failed:
            embed.add_field(
                name="⚠️ Failed",
                value=trim_embed_value("\n".join(["Universe `{}`: {}".format(uid, err) for uid, err in failed])),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=False)

        if isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel
            current_name = thread.name
            if current_name.startswith("Exp:"):
                new_name = "{}, {}".format(current_name, fetched_username)
            else:
                new_name = "Exp: {}".format(fetched_username)
            if len(new_name) > 100:
                new_name = new_name[:97] + "..."
            try:
                await thread.edit(name=new_name)
            except (discord.Forbidden, discord.HTTPException):
                pass


@bot.tree.command(
    name="infoban",
    description="Check ban status of a Roblox player across all universes",
    guild=guild,
)
@app_commands.describe(username="Player Roblox username")
async def infoban_command(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    if not has_allowed_role(interaction.user):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        user_id = await get_user_id_by_name(session, username)
        if not user_id:
            await interaction.followup.send("User **{}** was not found on Roblox.".format(username), ephemeral=True)
            return

        username, display_name, avatar_url, friends, followers, following = await fetch_user_data(session, user_id)

        restriction_tasks = [get_user_restriction(session, user_id, uid) for uid in ALL_UNIVERSE_IDS]
        universe_info_tasks = [get_universe_info(session, uid) for uid in ALL_UNIVERSE_IDS]

        restriction_results, universe_info_results = await asyncio.gather(
            asyncio.gather(*restriction_tasks),
            asyncio.gather(*universe_info_tasks),
        )

        banned_lines = []

        for universe_id, data, info in zip(ALL_UNIVERSE_IDS, restriction_results, universe_info_results):
            game_join = (data or {}).get("gameJoinRestriction", {})
            if not game_join.get("active", False):
                continue

            place_name = info.get("name", "Universe {}".format(universe_id))
            place_url = info.get("url", "https://www.roblox.com/discover#/")

            start_time = game_join.get("startTime", "")
            duration = game_join.get("duration")
            display_reason = game_join.get("privateReason", "")

            if start_time:
                try:
                    dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    date_str = "<t:{}:f>".format(int(dt.timestamp()))
                except Exception:
                    date_str = start_time
            else:
                date_str = "Unknown"

            duration_str = "Permanent" if not duration else duration
            reason_str = " • Reason: {}".format(display_reason) if display_reason else ""

            banned_lines.append(
                "**[{}]({})** — {} | {}{}".format(place_name, place_url, date_str, duration_str, reason_str)
            )

        total_banned = len(banned_lines)

        if total_banned == 0:
            embed = build_user_embed(
                user_id, display_name, username, avatar_url,
                friends, followers, following,
                0x57F287,
            )
            embed.add_field(
                name="✅ Not banned",
                value="This player is not banned in any of the {} configured place(s).".format(len(ALL_UNIVERSE_IDS)),
                inline=False,
            )
        else:
            embed = build_user_embed(
                user_id, display_name, username, avatar_url,
                friends, followers, following,
                0xE74C3C,
            )
            embed.add_field(
                name="🔨 Banned in {}/{} place(s)".format(total_banned, len(ALL_UNIVERSE_IDS)),
                value=trim_embed_value("\n".join(banned_lines)),
                inline=False,
            )

        await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="closerep",
    description="Close a report forum post and tag it accordingly",
    guild=guild,
)
@app_commands.describe(
    outcome="Whether the reported player was banned or not",
    reason="Reason why the report was rejected (only for not banned)",
)
@app_commands.choices(
    outcome=[
        app_commands.Choice(name="banned", value="banned"),
        app_commands.Choice(name="not banned", value="not_banned"),
    ],
    reason=[
        app_commands.Choice(
            name="Proofs hasn't been provided (screenshot or video) 🎥.",
            value="Proofs hasn't been provided (screenshot or video) 🎥.",
        ),
        app_commands.Choice(
            name="Missing the exploiter username (in the clip) ⚠️.",
            value="Missing the exploiter username (in the clip) ⚠️.",
        ),
        app_commands.Choice(
            name="This isn't an exploiter ❗.",
            value="This isn't an exploiter ❗.",
        ),
    ],
)
async def closerep_command(
    interaction: discord.Interaction,
    outcome: app_commands.Choice[str] = None,
    reason: app_commands.Choice[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    if not has_allowed_role(interaction.user):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        await interaction.followup.send("This command must be used inside a forum post (thread).", ephemeral=True)
        return

    is_banned = outcome is None or outcome.value == "banned"

    existing_tag_ids = [t.id for t in channel.applied_tags]
    new_tag_ids = list(existing_tag_ids)

    if TAG_CLOSED and TAG_CLOSED not in new_tag_ids:
        new_tag_ids.append(TAG_CLOSED)

    if is_banned:
        if TAG_BANNED and TAG_BANNED not in new_tag_ids:
            new_tag_ids.append(TAG_BANNED)
        if TAG_REVIEWED in new_tag_ids:
            new_tag_ids.remove(TAG_REVIEWED)
    else:
        if TAG_REVIEWED and TAG_REVIEWED not in new_tag_ids:
            new_tag_ids.append(TAG_REVIEWED)
        if TAG_BANNED in new_tag_ids:
            new_tag_ids.remove(TAG_BANNED)

    parent = channel.parent
    available_tags = {t.id: t for t in getattr(parent, "available_tags", [])}
    tags_to_apply = [available_tags[tid] for tid in new_tag_ids if tid in available_tags]

    try:
        await channel.edit(applied_tags=tags_to_apply)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to edit this thread.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send("Failed to update the thread: {}".format(e), ephemeral=True)
        return

    owner_mention = "<@{}>".format(channel.owner_id) if channel.owner_id else ""

    if is_banned:
        embed_title = "Exploiter banned! Thanks for reporting."
        embed_color = 0x57F287
    else:
        embed_title = "Your report does not meet the Exploiter Report Rules."
        embed_color = 0xFEE75C

    description = None
    if not is_banned and reason:
        description = "**What's wrong:** {}".format(reason.value)

    embed = discord.Embed(
        title=embed_title,
        description=description,
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🛡 Closed by", value=interaction.user.mention, inline=True)
    embed.set_footer(text="Report closed")

    await channel.send(content=owner_mention, embed=embed)
    await interaction.followup.send("Report closed.", ephemeral=True)


@bot.tree.command(
    name="syncbans",
    description="Sync missing bans between all configured universes",
    guild=guild,
)
async def syncbans_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    if not has_allowed_role(interaction.user):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    if not ALL_UNIVERSE_IDS:
        await interaction.followup.send("no universe IDs found.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        universe_bans = {}
        all_bans = {}
        source_errors = []

        for universe_id in ALL_UNIVERSE_IDS:
            api_key = UNIVERSE_API_KEY[universe_id]
            restrictions, source_err = await list_user_restrictions(session, universe_id, api_key)
            if restrictions is None:
                source_errors.append((universe_id, source_err))
                continue

            current_bans = {}
            for item in restrictions:
                game_join = item.get("gameJoinRestriction") or {}
                if game_join.get("active") is not True:
                    continue
                user_id = restriction_user_id(item)
                if not user_id or not str(user_id).isdigit():
                    continue
                current_bans[str(user_id)] = game_join
                if str(user_id) not in all_bans:
                    all_bans[str(user_id)] = {
                        "restriction": game_join,
                        "source_universe_id": universe_id,
                    }
            universe_bans[universe_id] = current_bans

        if not all_bans and not source_errors:
            await interaction.followup.send("rn there are no active bans to sync.", ephemeral=True)
            return

        results = []
        already_synced = []

        for target_universe_id in ALL_UNIVERSE_IDS:
            if target_universe_id not in universe_bans:
                continue
            target_bans = universe_bans[target_universe_id]
            for user_id, item in all_bans.items():
                if user_id in target_bans:
                    already_synced.append((user_id, target_universe_id))
                    continue
                game_join = item["restriction"]
                ok, sync_err = await apply_restriction_in_universe(session, user_id, game_join, target_universe_id)
                results.append((user_id, item["source_universe_id"], target_universe_id, ok, sync_err))

        universe_info = {}
        for universe_id in ALL_UNIVERSE_IDS:
            universe_info[universe_id] = await get_universe_info(session, universe_id)

        migrated = [
            (user_id, source_universe_id, target_universe_id)
            for user_id, source_universe_id, target_universe_id, ok, _ in results if ok
        ]
        failed = [
            (user_id, source_universe_id, target_universe_id, sync_err)
            for user_id, source_universe_id, target_universe_id, ok, sync_err in results if not ok
        ]

        if not migrated and not failed:
            await interaction.followup.send("everything is already synced. nothing new to add.", ephemeral=True)
            return

        updates_by_universe = {}
        for _, _, target_universe_id in migrated:
            updates_by_universe[target_universe_id] = updates_by_universe.get(target_universe_id, 0) + 1

        embed = discord.Embed(
            title="syncbans finished",
            color=0x57F287 if not failed else 0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🛡 Moderator", value=interaction.user.mention, inline=True)

        if updates_by_universe:
            update_lines = []
            for universe_id, added_count in sorted(updates_by_universe.items(), key=lambda item: item[1], reverse=True):
                info = universe_info.get(universe_id, {})
                place_name = info.get("name", "Universe {}".format(universe_id))
                place_url = info.get("url", "https://www.roblox.com/discover#/")
                update_lines.append("**[{}]({})** - {} new ban(s)".format(place_name, place_url, added_count))

            chunks = chunk_lines(update_lines)
            for index, chunk in enumerate(chunks, start=1):
                field_name = "📥 Updated places" if index == 1 else "📥 Updated places ({})".format(index)
                embed.add_field(name=field_name, value=chunk, inline=False)

        if failed:
            embed.add_field(
                name="⚠️ Failed users",
                value=trim_embed_value(
                    "\n".join([
                        "User `{}` from `{}` to `{}`: {}".format(
                            user_id, source_universe_id, target_universe_id, sync_err
                        )
                        for user_id, source_universe_id, target_universe_id, sync_err in failed[:10]
                    ])
                ),
                inline=False,
            )

        if source_errors:
            embed.add_field(
                name="⚠️ Some universes couldn't be read",
                value=trim_embed_value(
                    "\n".join([
                        "Universe `{}`: {}".format(source_universe_id, source_err)
                        for source_universe_id, source_err in source_errors[:10]
                    ])
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    print("Commands synced to guild {}.".format(GUILD_ID))


async def main():
    retry_delay = 60
    while True:
        try:
            async with bot:
                await bot.start(DISCORD_TOKEN)
        except discord.HTTPException as e:
            if e.status == 429:
                print("Discord rate limited the bot login. Waiting {} seconds before retry.".format(retry_delay))
                await asyncio.sleep(retry_delay)
            else:
                raise
        except Exception as e:
            print("Bot crashed: {}".format(e))
            await asyncio.sleep(retry_delay)


asyncio.run(main())
