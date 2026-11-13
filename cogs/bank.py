"""Private account reads and administrator-authorised emerald adjustments."""

import discord
from discord import app_commands
from discord.ext import commands

import bank_store as store
from cogs.war_share import message_payload


Amount = app_commands.Range[int, 1, store.MAX_AMOUNT]
AccountID = app_commands.Range[int, 1, 2**53 - 1]
Page = app_commands.Range[int, 1, 1_000_000]
Reason = app_commands.Range[str, 1, 250]


def account_name(row):
    if row["kind"] == "member":
        return f"<@{row['subject_id']}>"
    return discord.utils.escape_markdown(row["label"])[:100]


async def reply(interaction, text):
    await interaction.followup.send(**message_payload(text), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def target(member, land_name, account_id):
    if member is not None and member.bot:
        raise ValueError("Choose a person rather than a bot account.")
    return dict(member=member.id if member else None, land_name=land_name, account_id=account_id)


@app_commands.guild_only()
class BankCog(commands.GroupCog, name="bank", group_description="Emerald accounts, statements and administrator banking"):
    async def interaction_check(self, interaction):
        if interaction.guild_id is None:
            raise app_commands.NoPrivateMessage()
        await interaction.response.defer(ephemeral=True)
        return True

    async def read(self, interaction, member, land_name, account_id, page=1, statement=False, treasury=False):
        try:
            row = await store.account(interaction.guild_id, interaction.user.id, admin=interaction.permissions.administrator,
                                      page=page, treasury=treasury, **target(member, land_name, account_id))
        except ValueError as exc:
            return await reply(interaction, str(exc))
        lines = [f"**{account_name(row)} · account #{row['id']}**", f"Balance: **{row['balance']:,} ems**"]
        if row["banners"]:
            b = row["banners"]
            share = b["land_share"] if row["kind"] == "land" else b["central_share"]
            needed = b["land_share"]-b["prepaid"] if row["kind"] == "land" else b["central_share"]+b["prepaid"]
            lines.append(f"**Banner creation liabilities** · {b['pending']:,} banners pending\n"
                         f"This budget's share: **{share:,} ems** · bank funding still needed: **{needed:,} ems**\n"
                         f"Land contributions already transferred to treasury: {b['prepaid']:,} ems.\n"
                         "Funds are not reserved. Physical land payment can be confirmed by an administrator at creation.")
        if statement:
            lines.append(f"**Statement · page {page} · 3 entries per page**")
            for e in row["entries"]:
                lines.append(f"**#{e['operation_id']} · {e['delta']:+,} ems** · {e['action']} · {e['created_at'][:10]} UTC\n"
                             f"By <@{e['actor_id']}> · balance after entry: {e['balance_after']:,} ems\n"
                             f"{discord.utils.escape_markdown(e['reason'])}")
            if not row["entries"]:
                lines.append("No entries on this page.")
        else:
            lines.append("Balances use whole emeralds. An administrator records deposits and withdrawals.")
        await reply(interaction, "\n\n".join(lines))

    @app_commands.command(description="Read your balance, your land's balance, or an account as administrator")
    @app_commands.describe(member="Yourself, or another member for administrators", land_name="Your settlement; administrators may select any",
                           account_id="Administrator lookup, including archived accounts")
    async def balance(self, interaction: discord.Interaction, member: discord.Member = None,
                      land_name: str = None, account_id: AccountID = None):
        await self.read(interaction, member, land_name, account_id)

    @app_commands.command(description="Read the dated entries and reasons behind an account balance")
    async def statement(self, interaction: discord.Interaction, member: discord.Member = None,
                        land_name: str = None, account_id: AccountID = None, page: Page = 1):
        await self.read(interaction, member, land_name, account_id, page=page, statement=True)

    async def adjust(self, interaction, amount, reason, member, land_name, account_id, withdraw):
        try:
            row = await store.adjust(interaction.guild_id, interaction.user.id, amount, reason,
                                     admin=interaction.permissions.administrator, request_id=interaction.id, withdraw=withdraw,
                                     **target(member, land_name, account_id))
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"**{'Already recorded' if row['duplicate'] else 'Recorded'} · transaction #{row['operation_id']}**\n"
                    f"{account_name(row)} · account #{row['account_id']}\n"
                    f"Change: **{row['delta']:+,} ems** · balance after transaction: **{row['balance']:,} ems**\n"
                    f"Reason: {discord.utils.escape_markdown(row['reason'])}")

    @app_commands.command(description="[Admin] Credit a member or land after approving a deposit")
    async def deposit(self, interaction: discord.Interaction, amount: Amount, reason: Reason,
                      member: discord.Member = None, land_name: str = None, account_id: AccountID = None):
        await self.adjust(interaction, amount, reason, member, land_name, account_id, False)

    @app_commands.command(description="[Admin] Debit an account after approving a withdrawal; no overdrafts")
    async def withdraw(self, interaction: discord.Interaction, amount: Amount, reason: Reason,
                       member: discord.Member = None, land_name: str = None, account_id: AccountID = None):
        await self.adjust(interaction, amount, reason, member, land_name, account_id, True)

    @app_commands.command(description="[Admin] List settlement accounts, opened personal accounts and the treasury")
    async def accounts(self, interaction: discord.Interaction, page: Page = 1):
        try:
            rows = await store.accounts(interaction.guild_id, admin=interaction.permissions.administrator, page=page)
        except ValueError as exc:
            return await reply(interaction, str(exc))
        lines = [f"**Bank accounts · page {page}**"]
        lines.extend(f"#{r['id']} · {account_name(r)} · **{r['balance']:,} ems**" + (" · archived land" if r["archived"] else "") for r in rows)
        if not rows:
            lines.append("No accounts on this page.")
        lines.append("Personal accounts start at zero and open on first use. Use account_id to inspect an archived account.")
        await reply(interaction, "\n".join(lines))

    @app_commands.command(description="[Admin] Read the central treasury balance and its latest entries")
    async def treasury(self, interaction: discord.Interaction, page: Page = 1):
        await self.read(interaction, None, None, None, page=page, statement=True, treasury=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BankCog())
