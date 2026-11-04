"""Explicit sharing of an immutable estimate by its requesting administrator."""

import logging

import discord


log = logging.getLogger("claims-bot.war-share")


def message_payload(text):
    if len(text) <= 2000:
        return {"content": text}
    return {"embed": discord.Embed(description=text)}


class ShareEstimate(discord.ui.View):
    def __init__(self, interaction, text):
        super().__init__(timeout=300)
        self.requester = interaction.user.id
        self.guild_id = interaction.guild_id
        self.channel_id = interaction.channel_id
        self.source = interaction
        self.text = text
        self.attempted = False
        self.shared = False

    async def _update_button(self):
        # Ephemeral messages must be edited through their interaction webhook,
        # not Message.edit's ordinary channel-message endpoint.
        try:
            await self.source.edit_original_response(view=self)
        except (discord.HTTPException, OSError, TimeoutError):
            log.warning("War share button update failed guild=%s channel=%s", self.guild_id, self.channel_id)

    async def _notice(self, interaction, text):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except (discord.HTTPException, OSError, TimeoutError):
            # The click token can expire while the original command token is valid.
            try:
                await self.source.followup.send(text, ephemeral=True)
            except (discord.HTTPException, OSError, TimeoutError):
                log.warning("War share notice failed guild=%s channel=%s", self.guild_id, self.channel_id)

    async def interaction_check(self, interaction):
        if (interaction.user.id != self.requester or interaction.guild_id != self.guild_id
                or interaction.channel_id != self.channel_id or not interaction.permissions.administrator):
            await interaction.response.send_message(
                "Only the requesting administrator can share this estimate.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Share estimate in this channel", style=discord.ButtonStyle.secondary)
    async def share(self, interaction, button):
        # Recheck here as well, so a callback cannot bypass current admin status.
        if not await self.interaction_check(interaction):
            return
        if self.attempted:
            await self._notice(interaction, "This estimate has already been shared." if self.shared else
                               "The previous share is pending or unconfirmed. Check the channel before starting a new estimate.")
            return
        self.attempted = True  # before the first await: concurrent clicks cannot post twice
        button.disabled = True
        try:
            # The snapshot is ready: publish it as the click's public response.
            # Do not defer ephemerally: that would make its first followup private.
            await interaction.response.send_message(**message_payload(self.text), ephemeral=False,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, OSError, TimeoutError) as exc:
            status = getattr(exc, "status", None)
            code = getattr(exc, "code", None)
            log.warning("War estimate share failed guild=%s channel=%s error=%s http=%s discord_code=%s",
                        self.guild_id, self.channel_id, type(exc).__name__, status, code)
            reference = f"HTTP {status}, Discord {code}" if status is not None else type(exc).__name__
            # A rejected request did not post. Network/server failures and an
            # already-acknowledged interaction have an ambiguous outcome.
            rejected = status is not None and 400 <= status < 500 and status != 408 and code != 40060
            if rejected:
                self.attempted = False
                button.disabled = False
                if status == 403 or code in (50001, 50013):
                    detail = ("Check the app's View Channel, Send Messages (Send Messages in Threads for a thread), "
                              "and Embed Links permissions here, then click Share again.")
                elif code == 10062:
                    detail = "The button click expired. Click Share again."
                else:
                    detail = "Please report this error code if it persists; you can retry with Share."
                notice = f"Discord rejected sharing ({reference}). No public post was created. {detail}"
            else:
                notice = (f"Discord did not confirm sharing ({reference}). Check the channel before running a new estimate; "
                          "this button is locked to prevent duplicate posts.")
            await self._notice(interaction, notice)
            await self._update_button()
            return
        self.shared = True
        button.label = "Estimate shared"
        await self._update_button()
