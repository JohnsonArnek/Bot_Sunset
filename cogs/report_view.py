"""Reader-bound navigation for private, read-only Discord reports."""

import logging

import discord


log = logging.getLogger("claims-bot")
NO_MENTIONS = discord.AllowedMentions.none()


def clip(text, limit):
    """Fit Discord's UTF-16 limits without cutting a surrogate pair."""
    raw = text.encode("utf-16-le")
    if len(raw) <= limit * 2:
        return text
    return raw[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


class ReportSelect(discord.ui.Select):
    def __init__(self, prompt, choices):
        # Short indices also support stored names longer than Discord's option value limit.
        self.keys = [key for key, _ in choices]
        super().__init__(placeholder=prompt, row=0, options=[
            discord.SelectOption(label=clip(name, 100), value=str(i)) for i, (_, name) in enumerate(choices)])

    async def callback(self, interaction):
        if not self.values or self.values[0] not in {option.value for option in self.options}:
            await interaction.response.send_message("Choose an entry in the current list.", ephemeral=True)
            return
        await self.view.navigate(interaction, self.view.page, self.keys[int(self.values[0])])


class ReportView(discord.ui.View):
    def __init__(self, interaction, render, page, pages, *, selection=None, choices=(), prompt=None):
        super().__init__(timeout=300)
        self.source = interaction
        self.reader = interaction.user.id
        self.guild = interaction.guild_id
        self.channel = interaction.channel_id
        self.render = render
        self.page, self.selection = page, selection
        self.busy = False
        if selection is None:
            self.remove_item(self.back)
            self.previous.disabled = page <= 1
            self.next_page.disabled = page >= pages
        else:
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        if choices:
            self.add_item(ReportSelect(prompt, choices))

    async def interaction_check(self, interaction):
        if (interaction.user.id != self.reader or interaction.guild_id != self.guild or
                interaction.channel_id != self.channel or self.is_finished() or self.busy):
            await interaction.response.send_message("Open your own report or use the latest controls.", ephemeral=True)
            return False
        return True

    async def navigate(self, interaction, page, selection):
        if not await self.interaction_check(interaction):
            return
        self.busy = True
        new_view = None
        try:
            await interaction.response.defer()
            # Each renderer checks current access before reading the server's data.
            embed, new_view = await self.render(interaction, page, selection)
            await interaction.edit_original_response(content=None, embed=embed, view=new_view, allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            if new_view:
                new_view.stop()
            message = "This report could not be loaded. Run the command again."
            if isinstance(exc, PermissionError):
                message = "You no longer have access to this report."
            else:
                log.warning("Report navigation failed (%s)", type(exc).__name__)
            try:
                await interaction.edit_original_response(content=message, embed=None, view=None, allowed_mentions=NO_MENTIONS)
            except (discord.HTTPException, OSError, TimeoutError):
                log.info("Could not update the report message")
        finally:
            self.stop()

    @discord.ui.button(label="Previous", row=1)
    async def previous(self, interaction, button):
        await self.navigate(interaction, self.page - 1, None)

    @discord.ui.button(label="Next", row=1)
    async def next_page(self, interaction, button):
        await self.navigate(interaction, self.page + 1, None)

    @discord.ui.button(label="Back to list", row=1)
    async def back(self, interaction, button):
        await self.navigate(interaction, self.page, None)

    @discord.ui.button(label="Refresh", row=1, style=discord.ButtonStyle.primary)
    async def refresh(self, interaction, button):
        await self.navigate(interaction, self.page, self.selection)

    async def on_timeout(self):
        if self.busy:
            return
        self.stop()
        for control in self.children:
            control.disabled = True
        try:
            await self.source.edit_original_response(view=self)
        except (discord.HTTPException, OSError, TimeoutError):
            log.info("Could not disable expired report controls")
