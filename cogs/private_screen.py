"""Reader-bound private menus with single-use navigation and confirmations."""

import logging

import discord

from cogs.report_view import NO_MENTIONS


log = logging.getLogger("claims-bot")


class PrivateScreen(discord.ui.View):
    stale = "Open your own menu or use its latest controls."
    unavailable = "This item is no longer available."
    temporary = "This menu could not be loaded. Reopen it and check the latest status."

    def __init__(self, interaction):
        super().__init__(timeout=600)
        self.owner, self.guild, self.channel = interaction.user.id, interaction.guild_id, interaction.channel_id
        self.source = interaction
        self.busy = False

    async def interaction_check(self, interaction):
        if (interaction.user.id != self.owner or interaction.guild_id != self.guild or interaction.channel_id != self.channel
                or self.is_finished() or self.busy):
            await interaction.response.send_message(self.stale,
                                                   ephemeral=True, allowed_mentions=NO_MENTIONS)
            return False
        return True

    async def replace(self, interaction, render_next, *, commit=None):
        if not await self.interaction_check(interaction):
            return
        self.busy = True
        notice = next_view = None
        try:
            await interaction.response.defer()
            if commit:
                try:
                    notice = await commit(interaction)
                except ValueError as exc:
                    notice = str(exc)
            embed, next_view = await render_next(interaction)
            await interaction.edit_original_response(content=notice, embed=embed, view=next_view, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            if next_view:
                next_view.stop()
            message = self.unavailable if isinstance(exc, LookupError) else self.temporary
            if not isinstance(exc, LookupError):
                log.warning("Private screen unavailable (%s)", type(exc).__name__)
            if notice:
                message = f"{notice}\nReopen the menu for its latest status."
            try:
                await interaction.edit_original_response(content=message, embed=None, view=None, allowed_mentions=NO_MENTIONS)
            except (discord.HTTPException, OSError, TimeoutError):
                log.warning("Private screen update could not be displayed")
        finally:
            self.stop()

    async def on_timeout(self):
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await self.source.edit_original_response(view=self)
        except (discord.HTTPException, OSError, TimeoutError):
            log.info("Expired private screen could not be updated")
