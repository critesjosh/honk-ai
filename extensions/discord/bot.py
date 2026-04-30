import asyncio
import json
import os
import re
import logging
import aiohttp
import discord
from discord.ext import commands
import dotenv

dotenv.load_dotenv()

# Enable logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot configuration
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = '!'  # Command prefix
BASE_API_URL = os.getenv("API_BASE", "https://gptcloud.arc53.com")
API_URL = BASE_API_URL + "/stream"
API_KEY = os.getenv("API_KEY")

# MCP key provisioning
MCP_PROVISIONING_KEY = os.getenv("MCP_PROVISIONING_KEY", "")
NOIR_GUILD_ID = int(os.getenv("NOIR_GUILD_ID", "0"))
BOT_ROLE_ID = os.getenv("BOT_ROLE_ID", "1492704234842493050")

# Public URL users hit from their MCP clients. Prefer PUBLIC_HOSTNAME
# (set on the hub compose) and construct the https URL; fall back to a
# placeholder so the instructions still read sensibly in dev.
_public_host = os.getenv("PUBLIC_HOSTNAME", "").strip()
MCP_PUBLIC_URL = (
    f"https://{_public_host}" if _public_host else "https://your-docsgpt.example.com"
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Store conversation history per user
conversation_histories = {}

def format_for_discord(text):
    """Converts standard Markdown to Discord-friendly formatting."""
    lines = text.split('\n')
    formatted = []
    for line in lines:
        # Convert headers to bold (Discord doesn't render # headers)
        header_match = re.match(r'^(#{1,3})\s+(.*)', line)
        if header_match:
            formatted.append(f'**{header_match.group(2)}**')
        else:
            formatted.append(line)
    return '\n'.join(formatted)


def chunk_string(text, max_length=2000):
    """Splits a string into chunks, avoiding breaks inside code blocks."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while len(text) > max_length:
        # Try to split at a code block boundary first
        split_at = -1

        # Look for a double newline near the limit (paragraph break)
        idx = text.rfind('\n\n', 0, max_length)
        if idx > max_length // 2:
            split_at = idx

        # If no good paragraph break, try a single newline
        if split_at == -1:
            idx = text.rfind('\n', 0, max_length)
            if idx > max_length // 2:
                split_at = idx

        # Last resort: split at a space
        if split_at == -1:
            idx = text.rfind(' ', 0, max_length)
            if idx > 0:
                split_at = idx
            else:
                split_at = max_length

        chunk = text[:split_at]

        # If we're splitting inside a code block, close and reopen it
        open_blocks = chunk.count('```')
        if open_blocks % 2 == 1:
            # Find the language hint from the last opening ```
            last_open = chunk.rfind('```')
            lang_match = re.match(r'```(\w*)', chunk[last_open:])
            lang = lang_match.group(1) if lang_match else ''
            chunk += '\n```'
            text = f'```{lang}\n' + text[split_at:].lstrip('\n')
        else:
            text = text[split_at:].lstrip('\n')

        chunks.append(chunk)

    if text:
        chunks.append(text)
    return chunks

def split_string(input_str):
    """Splits the input string to detect bot user or role mentions."""
    # Match user mention (@BotName) or role mention (@RoleName)
    pattern = r'^<@[!&]?(?:{0}|{1})>\s*'.format(bot.user.id, BOT_ROLE_ID)
    match = re.match(pattern, input_str)
    if match:
        content = input_str[match.end():].strip()
        return str(bot.user.id), content
    return None, input_str

@bot.event
async def setup_hook():
    """Sync slash commands once on startup (not on every reconnect).

    When ``NOIR_GUILD_ID`` is set we register commands at the guild
    scope (instant) AND tear down any prior global registrations.
    Without that teardown a deploy that started without the env var
    leaves global commands hanging around — Discord then renders both
    copies in the configured guild and users see every command twice.
    """
    if NOIR_GUILD_ID:
        noir_guild = discord.Object(id=NOIR_GUILD_ID)
        # Copy globals → guild tree, push, then wipe globals on
        # Discord's side. The decorator-defined commands re-populate
        # the in-memory global tree on every restart, so this dance
        # has to run every time.
        bot.tree.copy_global_to(guild=noir_guild)
        await bot.tree.sync(guild=noir_guild)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        logger.info(
            f"Slash commands synced to guild {NOIR_GUILD_ID}; "
            "global registrations cleared"
        )
    else:
        await bot.tree.sync()
        logger.info("Slash commands synced globally")


@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')


@bot.tree.command(
    name="mcp-key",
    description="Get your personal API key for the Aztec MCP server",
)
async def mcp_key(interaction: discord.Interaction):
    """Provisions a personal MCP API key via ephemeral message."""
    # Guild restriction
    if NOIR_GUILD_ID and interaction.guild_id != NOIR_GUILD_ID:
        await interaction.response.send_message(
            "This command is only available in the Noir Discord.",
            ephemeral=True,
        )
        return

    if not MCP_PROVISIONING_KEY:
        await interaction.response.send_message(
            "MCP key provisioning is not configured. Please contact an admin.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    payload = {
        "discord_user_id": str(interaction.user.id),
        "discord_username": interaction.user.display_name,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Provisioning-Key": MCP_PROVISIONING_KEY,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{BASE_API_URL}/api/internal/create_mcp_key",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"/get-api-key failed: {resp.status} {error_text}")
                    await interaction.followup.send(
                        "Sorry, there was an error generating your key. "
                        "Please try again later.",
                        ephemeral=True,
                    )
                    return
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"/get-api-key connection error: {e}")
        await interaction.followup.send(
            "Sorry, the service is temporarily unavailable. "
            "Please try again later.",
            ephemeral=True,
        )
        return

    api_key = data["api_key"]

    # Message 1: The key (separate from config to reduce screenshot disclosure risk)
    await interaction.followup.send(
        f"**Your Aztec MCP API Key:**\n```\n{api_key}\n```\n"
        "This key is personal to you. Do not share it.",
        ephemeral=True,
    )

    # Message 2: Setup instructions (no secret in this message)
    instructions = (
        "**Aztec MCP — setup**\n"
        f"Connection details:\n"
        f"• `API_URL` = `{MCP_PUBLIC_URL}`\n"
        "• `API_KEY` = the key from the previous message\n"
        "\n"
        "__Claude Desktop__ — add to `claude_desktop_config.json`:\n"
        "```json\n"
        "{\n"
        '  "mcpServers": {\n'
        '    "aztec-docs": {\n'
        '      "command": "npx",\n'
        '      "args": ["docsgpt-mcp-server"],\n'
        '      "env": {\n'
        f'        "API_URL": "{MCP_PUBLIC_URL}",\n'
        '        "API_KEY": "<paste your key here>"\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
        "```\n"
        "__Claude Code__ — run:\n"
        "```bash\n"
        "claude mcp add aztec-docs \\\n"
        f"  -e API_URL={MCP_PUBLIC_URL} \\\n"
        "  -e API_KEY=<paste your key here> \\\n"
        "  -- npx docsgpt-mcp-server\n"
        "```\n"
        "__Codex__ — add to `~/.codex/config.toml`:\n"
        "```toml\n"
        "[mcp_servers.aztec-docs]\n"
        'command = "npx"\n'
        'args = ["docsgpt-mcp-server"]\n'
        f'env = {{ API_URL = "{MCP_PUBLIC_URL}", '
        'API_KEY = "<paste your key here>" }\n'
        "```"
    )
    await interaction.followup.send(instructions, ephemeral=True)


@bot.tree.command(
    name="forget-me",
    description="Erase your Honk AI data (MCP key + conversation history)",
)
async def forget_me(interaction: discord.Interaction):
    """Calls the backend forget endpoint (GDPR Article 17 — right to erasure).

    Unlike ``/mcp-key`` this is intentionally allowed in DMs — the bot
    accepts and stores DM conversations (see ``on_message`` handler), so
    the erasure path must be reachable from the same surface where the
    data was created. Inside guilds it's still gated to ``NOIR_GUILD_ID``.
    """
    is_dm = interaction.guild_id is None
    if not is_dm and NOIR_GUILD_ID and interaction.guild_id != NOIR_GUILD_ID:
        await interaction.response.send_message(
            "This command is only available in the Noir Discord or in a DM.",
            ephemeral=True,
        )
        return

    if not MCP_PROVISIONING_KEY:
        await interaction.response.send_message(
            "Account erasure is not configured. Please contact an admin.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    payload = {"discord_user_id": str(interaction.user.id)}
    headers = {
        "Content-Type": "application/json",
        "X-Provisioning-Key": MCP_PROVISIONING_KEY,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{BASE_API_URL}/api/internal/forget_discord_user",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"/forget-me failed: {resp.status} {error_text}")
                    await interaction.followup.send(
                        "Sorry, there was an error erasing your data. "
                        "Please try again later.",
                        ephemeral=True,
                    )
                    return
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"/forget-me connection error: {e}")
        await interaction.followup.send(
            "Sorry, the service is temporarily unavailable. "
            "Please try again later.",
            ephemeral=True,
        )
        return

    # Drop the in-memory conversation history too.
    conversation_histories.pop(interaction.user.id, None)

    deleted = data.get("deleted", {})
    summary_lines = ["**Done.** Your Honk AI data has been erased:"]
    if deleted.get("agents"):
        summary_lines.append(
            f"• MCP API key revoked ({deleted['agents']} agent record)"
        )
    if deleted.get("conversations"):
        summary_lines.append(
            f"• {deleted['conversations']} conversation(s) deleted"
        )
    if not deleted.get("agents") and not deleted.get("conversations"):
        summary_lines.append("• No data was found for your Discord ID.")
    summary_lines.append(
        "\nYou can run `/mcp-key` again any time to provision a fresh key."
    )
    await interaction.followup.send("\n".join(summary_lines), ephemeral=True)


async def generate_answer(question, messages, conversation_id):
    """Generates an answer using the streaming API endpoint."""
    payload = {
        "question": question,
        "api_key": API_KEY,
        "history": json.dumps(messages),
        "conversation_id": conversation_id
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8"
    }
    timeout = aiohttp.ClientTimeout(total=180)
    answer = ""
    new_conversation_id = conversation_id
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(API_URL, json=payload, headers=headers) as resp:
            if resp.status != 200:
                return {"answer": "Sorry, I couldn't find an answer.", "conversation_id": None}
            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type", "")
                if event_type == "answer":
                    answer += event.get("answer", "")
                elif event_type == "id":
                    new_conversation_id = event.get("id")
    return {"answer": answer or "Sorry, I couldn't find an answer.", "conversation_id": new_conversation_id}

@bot.command(name="start")
async def start(ctx):
    """Handles the /start command."""
    await ctx.send(f"Hi {ctx.author.mention}! How can I assist you today?")

@bot.command(name="reset")
async def reset(ctx):
    """Clears conversation history for the user."""
    user_id = ctx.author.id
    if user_id in conversation_histories:
        del conversation_histories[user_id]
    await ctx.send(f"Conversation reset, {ctx.author.mention}. Ask me anything!")

@bot.command(name="custom_help")
async def custom_help_command(ctx):
    """Handles the /custom_help command."""
    help_text = (
        "Here are the available commands:\n"
        "`!start` - Begin a new conversation with the bot\n"
        "`!reset` - Clear your conversation history\n"
        "`!custom_help` - Display this help message\n\n"
        "You can also mention me or send a direct message to ask a question!"
    )
    await ctx.send(help_text)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Process commands first
    await bot.process_commands(message)

    # Check if the message is in a DM channel
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip()
    else:
        # In guild channels, check if the message mentions the bot at the start
        content = message.content.strip()
        prefix, content = split_string(content)
        if prefix is None:
            return
        part_prefix = str(bot.user.id)
        if part_prefix != prefix:
            return  # Bot not mentioned at the start, so do not process

    # Now process the message
    user_id = message.author.id
    if user_id not in conversation_histories:
        conversation_histories[user_id] = {
            "history": [],
            "conversation_id": None
        }

    conversation = conversation_histories[user_id]
    conversation["history"].append({"prompt": content})

    # Show typing indicator while generating and sending the answer
    async with message.channel.typing():
        try:
            response_doc = await generate_answer(
                content,
                conversation["history"],
                conversation["conversation_id"]
            )
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            logger.error(f"Error generating answer: {e}")
            await message.channel.send(
                "Sorry, the request timed out. Please try again with a shorter message."
            )
            conversation["history"].pop()
            return

        answer = response_doc["answer"]
        conversation_id = response_doc["conversation_id"]

        formatted = format_for_discord(answer)
        answer_chunks = chunk_string(formatted)
        for chunk in answer_chunks:
            await message.channel.send(chunk)

    conversation["history"][-1]["response"] = answer
    conversation["conversation_id"] = conversation_id

    # Keep conversation history to last 10 exchanges
    conversation["history"] = conversation["history"][-10:]

bot.run(TOKEN)