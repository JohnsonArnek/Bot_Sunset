"""Private banner production tracking and administrator creation recording."""

import discord
from discord import app_commands
from discord.ext import commands

import banner_store as store
from cogs.bank import AccountID, Amount, Page, Reason, reply


def batch_text(row):
    pending = row["blocks"]-row["created"]
    land = pending*row["land_unit"]
    central = pending*(row["unit_cost"]-row["land_unit"])
    prepaid = pending*row["prepaid_unit"]
    return (f"**Purchase #{row['id']} · {discord.utils.escape_markdown(row['land_name'])[:100]}**\n"
            f"{row['created']:,}/{row['blocks']:,} banners created · **{pending:,} pending**\n"
            f"Creation cost: {row['unit_cost']:,} ems/banner · outstanding: **{land+central:,} ems**\n"
            f"Land liability: **{land:,}** ({prepaid:,} prepaid) · central subsidy: **{central:,} ems**\n"
            f"Bank funding needed: land **{land-prepaid:,}** + treasury **{central+prepaid:,} ems**")


@app_commands.guild_only()
class BannerCog(commands.GroupCog, name="banner", group_description="Track claim-banner creation costs and subsidies"):
    async def interaction_check(self, interaction):
        if interaction.guild_id is None:
            raise app_commands.NoPrivateMessage()
        await interaction.response.defer(ephemeral=True)
        return True

    @app_commands.command(description="View your land's banner backlog, or all creation liabilities as administrator")
    async def status(self, interaction: discord.Interaction, land_name: str = None, page: Page = 1):
        try:
            result = await store.status(interaction.guild_id,interaction.user.id,
                                        admin=interaction.permissions.administrator,land_name=land_name,page=page)
        except ValueError as exc:
            return await reply(interaction,str(exc))
        s = result["totals"]
        lines = [f"**Banner creation · page {page}**",
                 f"Pending: **{s['pending']:,} banners · {s['total']:,} ems**\n"
                 f"Land liabilities: **{s['land_share']:,}** · central subsidies: **{s['central_share']:,} ems**\n"
                 f"Already prepaid by bank: {s['prepaid']:,} ems · destroyed through recorded creation: {s['destroyed']:,} ems"]
        lines.extend(batch_text(r) for r in result["batches"])
        if not result["batches"]:
            lines.append("No tracked purchases on this page.")
        lines.append("Use /banner show for details. Creation is separate from delivery; /ledger deliver does not spend money.")
        await reply(interaction,"\n\n".join(lines))

    @app_commands.command(description="Show a claim purchase's fixed creation split and recent creation records")
    async def show(self, interaction: discord.Interaction, purchase_id: AccountID):
        try:
            row = await store.show(interaction.guild_id,interaction.user.id,purchase_id,admin=interaction.permissions.administrator)
        except ValueError as exc:
            return await reply(interaction,str(exc))
        lines = [batch_text(row),"**Recent creation records**"]
        for h in row["history"]:
            lines.append(f"#{h['operation_id']} · {h['blocks']:,} banners · {h['created_at'][:10]} UTC · by <@{h['actor_id']}>\n"
                         f"Physical land payment: {h['physical_land_payment']:,} ems\n{discord.utils.escape_markdown(h['reason'])}")
        if not row["history"]:
            lines.append("No banners marked created yet.")
        await reply(interaction,"\n\n".join(lines))

    @app_commands.command(description="[Admin] Record banners created and destroy their funded creation cost")
    @app_commands.describe(purchase_id="Purchase receipt ID shown by /claim buy or /banner status",
                           blocks="Number actually created in this batch",reason="Creation record or explanation",
                           land_payment="Physical confirms the unpaid land share was paid and spent outside the bank")
    @app_commands.choices(land_payment=[app_commands.Choice(name="Debit bank funds (recognises prepaid claims)",value="bank"),
                                       app_commands.Choice(name="Confirm physical land payment",value="physical")])
    async def create(self, interaction: discord.Interaction, purchase_id: AccountID,
                     blocks: app_commands.Range[int,1,2_000_000_000], reason: Reason, land_payment: str = "bank"):
        try:
            r = await store.create(interaction.guild_id,interaction.user.id,purchase_id,blocks,reason,
                                   admin=interaction.permissions.administrator,request_id=interaction.id,land_payment=land_payment)
        except ValueError as exc:
            return await reply(interaction,str(exc))
        await reply(interaction,f"**{'Already recorded' if r['duplicate'] else 'Creation recorded'} · transaction #{r['operation_id']}**\n"
                    f"Purchase #{r['purchase_id']} · {r['blocks']:,} banners · **{r['total']:,} ems destroyed**\n"
                    f"Cost split: land {r['land_share']:,} + central budget {r['central_share']:,} ems\n"
                    f"Bank debits: land {r['land_debit']:,} + treasury {r['treasury_debit']:,} ems\n"
                    f"Confirmed physical land payment: {r['physical']:,} ems\n"
                    f"{r['remaining']:,} banners still to create. Record delivery separately with /ledger deliver.")

    @app_commands.command(description="[Admin] Read or change the creation cost per banner for future purchases")
    async def cost(self, interaction: discord.Interaction, amount: Amount = None):
        try:
            value = await store.cost(interaction.guild_id,admin=interaction.permissions.administrator,amount=amount)
        except ValueError as exc:
            return await reply(interaction,str(exc))
        await reply(interaction,f"Banner creation cost: **{value:,} ems each**. Existing purchases keep their recorded cost and subsidy split.")


async def setup(bot: commands.Bot):
    await bot.add_cog(BannerCog())
