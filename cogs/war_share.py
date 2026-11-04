"""Explicit sharing of an immutable estimate by its requesting administrator."""

import discord


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
        self.text = text
        self.attempted = False

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
            await interaction.response.send_message("Sharing was already attempted. Check the channel for the post.", ephemeral=True)
            return
        self.attempted = True  # before the first await: concurrent clicks cannot post twice
        button.disabled = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.channel.send(**message_payload(self.text), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            # An ambiguous network error might have posted it; do not auto-retry.
            await interaction.followup.send(
                "Could not confirm sharing. Check the channel; run the command again if no post appeared.", ephemeral=True)
            return
        await interaction.followup.send("Estimate shared in this channel.", ephemeral=True)
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass  # The public post succeeded even if its private button cannot be updated.
