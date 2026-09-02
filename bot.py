import asyncio
import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID"))
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))
AUTO_CLOSE_MINUTES = int(os.getenv("AUTO_CLOSE_MINUTES", 1440))

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Track active timers and their corresponding tasks
# Format: { admin_msg_id: asyncio.Task }
active_timers = {}


def format_time(seconds: int) -> str:
  hrs, rem = divmod(seconds, 3600)
  mins, secs = divmod(rem, 60)
  return f"{hrs:02d}h {mins:02d}m {secs:02d}s"


# --- INTERACTIVE BUTTON VIEW ---
class TimerControlView(discord.ui.View):

  def __init__(
      self,
      admin_msg_id: int,
      target_channel: discord.TextChannel = None,
      user_id: int = None,
  ):
    super().__init__(timeout=None)  # Keeps button persistent
    self.admin_msg_id = admin_msg_id
    self.target_channel = target_channel

  @discord.ui.button(
      label="Cancel Timer", style=discord.ButtonStyle.danger, emoji="🛑"
  )
  async def cancel_button(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    # Check if this message has an active running timer task
    task = active_timers.get(self.admin_msg_id)
    if task:
      task.cancel()  # Triggers asyncio.CancelledError inside run_admin_timer
      await interaction.response.send_message(
          f"🛑 Timer cancelled by {interaction.user.mention}.", ephemeral=True
      )
    else:
      await interaction.response.send_message(
          "⚠️ This timer has already expired or been stopped.", ephemeral=True
      )


@bot.event
async def on_ready():
  try:
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} command(s).")
  except Exception as e:
    print(f"Failed to sync commands: {e}")
  print(f"Logged in as {bot.user.name}")


# --- TIMER ENGINE ---
async def run_admin_timer(
    admin_msg: discord.Message,
    total_seconds: int,
    target_channel: discord.TextChannel = None,
):
  remaining = total_seconds
  update_interval = 15

  try:
    while remaining > 0:
      await asyncio.sleep(update_interval)
      remaining -= update_interval

      # Check if ticket channel was deleted manually from Discord
      if target_channel:
        fetched = target_channel.guild.get_channel(target_channel.id)
        if fetched is None:
          await finalize_admin_embed(
              admin_msg, "✅ **Closed Early** (Ticket Deleted)"
          )
          return

      # Update countdown embed
      embed = admin_msg.embeds[0]
      embed.set_field_at(
          2,
          name="Time Remaining",
          value=f"⏳ `{format_time(remaining)}`",
          inline=False,
      )
      await admin_msg.edit(embed=embed)

    # Natural Expiration
    if target_channel:
      await target_channel.send(
          "⏰ **Timer expired.** Auto-closing ticket now..."
      )
      await asyncio.sleep(2)
      await target_channel.delete(reason="Ticket timer expired.")

    await finalize_admin_embed(
        admin_msg, "⏰ **Timer Expired**", color=discord.Color.red()
    )

  except asyncio.CancelledError:
    # Triggered when staff clicks the 'Cancel Timer' button
    await finalize_admin_embed(
        admin_msg, "🛑 **Cancelled Early**", color=discord.Color.gold()
    )
  finally:
    active_timers.pop(admin_msg.id, None)


async def finalize_admin_embed(
    admin_msg: discord.Message,
    status_text: str,
    color: discord.Color = discord.Color.gold(),
):
  """Disables buttons and updates final status."""
  try:
    embed = admin_msg.embeds[0]
    embed.color = color
    embed.set_field_at(2, name="Status", value=status_text, inline=False)

    # Disable the Cancel button upon completion
    view = discord.ui.View()
    for item in admin_msg.components:
      for component in item.children:
        disabled_button = discord.ui.Button(
            label=component.label,
            style=component.style,
            emoji=component.emoji,
            disabled=True,
        )
        view.add_item(disabled_button)

    await admin_msg.edit(embed=embed, view=view)
  except Exception as e:
    print(f"Failed to finalize embed: {e}")


# --- 1. SLASH COMMAND FOR CUSTOM TIMERS ---
@bot.tree.command(
    name="timer", description="Set a custom administrative timer."
)
@app_commands.describe(
    title="Title or purpose of the timer",
    hours="Duration hours",
    minutes="Duration minutes",
    user="Member in question (optional)",
    notes="Additional notes (optional)",
)
async def custom_timer(
    interaction: discord.Interaction,
    title: str,
    hours: int = 0,
    minutes: int = 0,
    user: discord.User = None,
    notes: str = None,
):
  total_seconds = (hours * 3600) + (minutes * 60)

  if total_seconds <= 0:
    await interaction.response.send_message(
        "❌ Please specify a duration greater than 0 minutes.", ephemeral=True
    )
    return

  admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)
  if not admin_channel:
    await interaction.response.send_message(
        "❌ Admin channel not found.", ephemeral=True
    )
    return
  if user:
    member_display = f"{user.mention} (`{user.name}` | ID: `{user.id}`)"
  else:
    member_display = "N/A"
  embed = discord.Embed(
      title=f"⏳ Timer: {title}",
      color=discord.Color.blue(),
      timestamp=discord.utils.utcnow(),
  )
  embed.add_field(
      name="Target Member",
      value=member_display,
      inline=True,
  )
  embed.add_field(name="Set By", value=interaction.user.mention, inline=True)
  embed.add_field(
      name="Time Remaining",
      value=f"⏳ `{format_time(total_seconds)}`",
      inline=False,
  )

  if notes:
    embed.add_field(name="Notes", value=notes, inline=False)

  # Send message initially without view to grab message ID
  admin_msg = await admin_channel.send(embed=embed)

  # Attach button view tied to message ID
  view = TimerControlView(admin_msg_id=admin_msg.id)
  await admin_msg.edit(view=view)

  # Track task
  task = asyncio.create_task(run_admin_timer(admin_msg, total_seconds))
  active_timers[admin_msg.id] = task

  await interaction.response.send_message(
      f"✅ Custom timer **'{title}'** started in {admin_channel.mention}!",
      ephemeral=True,
  )


# --- 2. AUTOMATIC TICKET LISTENER ---
@bot.event
async def on_guild_channel_create(channel):
  if (
      not isinstance(channel, discord.TextChannel)
      or channel.category_id != TICKET_CATEGORY_ID
  ):
    return

  admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)
  if not admin_channel:
    return

  await asyncio.sleep(3)  # Wait for Tickets v2 setup

  total_seconds = AUTO_CLOSE_MINUTES * 60
  embed = discord.Embed(
      title="🎟️ Ticket Auto-Close Timer", color=discord.Color.blue()
  )
  embed.add_field(name="Ticket Channel", value=channel.mention, inline=True)
  embed.add_field(
      name="Channel Topic", value=channel.topic or "None", inline=True
  )
  embed.add_field(
      name="Time Remaining",
      value=f"⏳ `{format_time(total_seconds)}`",
      inline=False,
  )
  embed.set_footer(text=f"Channel ID: {channel.id}")

  admin_msg = await admin_channel.send(embed=embed)

  # Attach button view
  view = TimerControlView(admin_msg_id=admin_msg.id, target_channel=channel)
  await admin_msg.edit(view=view)

  task = asyncio.create_task(
      run_admin_timer(admin_msg, total_seconds, target_channel=channel)
  )
  active_timers[admin_msg.id] = task


@bot.event
async def on_guild_channel_delete(channel):
  # Find and cancel timer corresponding to deleted ticket
  for admin_msg_id, task in list(active_timers.items()):
    # We cancel through task lookup
    pass


bot.run(TOKEN)