import os
import discord

# Simple loader for .env files without requiring external dependencies
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()
                except ValueError:
                    pass

# Get the token from environment variables
TOKEN = os.environ.get("DISCORD_TOKEN")

# Setup bot intents
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Logged in as {client.user.name} (ID: {client.user.id})")
    print("------")

@client.event
async def on_message(message):
    # Prevent the bot from replying to itself
    if message.author == client.user:
        return

    # Check if "kurisutina" is in the message content (case-insensitive)
    if "kurisutina" in message.content.lower():
        # Reply with "Hör auf mich Kurisutina zu nennen" in cursive (italics), and "Kurisutina" in bold as well
        response = "*Hör auf mich* ***Kurisutina*** *zu nennen!*"
        await message.reply(response)

    # Check if "horny" is in the message content (case-insensitive)
    if "horny" in message.content.lower():
        await message.reply(f"{message.author.mention} ist Horny!")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN environment variable is not set.")
        print("Please create a .env file containing: DISCORD_TOKEN=your_token_here")
    else:
        client.run(TOKEN)
