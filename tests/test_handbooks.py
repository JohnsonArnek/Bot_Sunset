"""Synthetic articles only: private handbook text must never enter fixtures."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import discord

import database as db
import acclimation_store
import handbook_store as store
from cogs import handbook_browser as browser
from cogs.sunset import SunsetCog


def article(slug="public", book="citizens", pages=None):
    return dict(slug=slug, book=book, title=f"Example {slug}", summary="Synthetic test article",
                pages=pages or ["Example page one", "Example page two"])


def interaction(user=10, guild=100, *, admin=False, roles=()):
    return SimpleNamespace(user=SimpleNamespace(id=user, roles=[SimpleNamespace(id=r) for r in roles]),
                           guild_id=guild, guild=SimpleNamespace(id=guild, owner_id=900), channel_id=55,
                           permissions=discord.Permissions(administrator=admin), response=AsyncMock(),
                           followup=AsyncMock(), edit_original_response=AsyncMock())


class StoreContract:
    async def test_private_queries_and_import_are_server_scoped(self):
        await store.import_articles(self.guild, [article(), article("hidden", "war")])
        self.assertEqual([r["slug"] for r in await store.catalog(self.guild, {"citizens"})], ["public"])
        self.assertIsNone(await store.article(self.guild, {"citizens"}, "hidden"))
        self.assertIsNone(await store.article(self.guild + 1, set(store.BOOKS), "hidden"))
        self.assertEqual(await store.catalog(self.guild, set()), [])
        await store.import_articles(self.guild, [article(pages=["Updated"])])
        self.assertEqual((await store.article(self.guild, {"citizens"}, "public"))["pages"], ["Updated"])
        self.assertIsNotNone(await store.article(self.guild, {"war"}, "hidden"))
        await db.init_db()
        self.assertEqual(len(await store.catalog(self.guild, set(store.BOOKS))), 2)

    async def test_import_rolls_back_and_validates_every_article_before_writing(self):
        await store.import_articles(self.guild, [article()])
        with self.assertRaises(ValueError):
            await store.import_articles(self.guild, [article(pages=["Unwanted edit"]), article("bad", "unknown")])
        original = db.DBConnection.execute

        async def fail_second(conn, query, params=()):
            if params and "second" in params:
                raise RuntimeError("Synthetic failure")
            return await original(conn, query, params)

        with patch.object(db.DBConnection, "execute", fail_second), self.assertRaises(RuntimeError):
            await store.import_articles(self.guild, [article(pages=["Unwanted edit"]), article("second")])
        self.assertEqual((await store.article(self.guild, {"citizens"}, "public"))["pages"], article()["pages"])

    async def test_current_bot_appointments_and_land_officers_control_access(self):
        i = interaction(guild=self.guild)
        self.assertEqual(await store.allowed_books(i), {"citizens"})
        await acclimation_store.appoint(self.guild, 900, 10, "hand", admin=True)
        self.assertEqual(await store.allowed_books(i), {"citizens", "hands"})
        await acclimation_store.appoint(self.guild, 900, 10, "tribune", admin=True)
        self.assertEqual(await store.allowed_books(i), {"citizens", "tribunes"})
        await acclimation_store.appoint(self.guild, 900, 10, "dismissed", admin=True)
        self.assertEqual(await store.allowed_books(i), {"citizens"})
        # Real helpers for officers, including an officer who is not a members row.
        async with db.DBConnection() as conn:
            land = await conn.execute_returning_one(
                "INSERT INTO lands (name,owner_id,chunks,guild_id,created_at) VALUES (?,?,?,?,?) RETURNING id",
                (f"Test-{self.guild}", 20, 0, self.guild, "2026-11-13T00:00:00+00:00"))
        await db.add_officer(land["id"], 10)
        self.assertIn("settlements", await store.allowed_books(i))
        self.assertIn("settlements", await store.allowed_books(interaction(20, self.guild)))
        self.assertEqual(await store.allowed_books(interaction(10, self.guild + 1)), {"citizens"})
        await db.remove_officer(land["id"], 10)
        self.assertEqual(await store.allowed_books(i), {"citizens"})


class HandbookSQLite(StoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db, "IS_POSTGRES", False))
        self.enterContext(patch.object(db, "DB_PATH", str(Path(temp.name) / "test.db")))
        self.guild = 100
        await db.init_db()

    async def test_staff_admin_and_dm_boundaries(self):
        await db.set_config(self.guild, "staff_role_id", "77")
        self.assertEqual(await store.allowed_books(interaction(roles=(77,))), {"citizens", "civil", "settlements"})
        self.assertEqual(await store.allowed_books(interaction(admin=True)), set(store.BOOKS))
        self.assertEqual(await store.allowed_books(interaction(900)), {"citizens"})
        await db.set_config(self.guild, "staff_role_id", "0")
        self.assertEqual(await store.allowed_books(interaction(900)), {"citizens", "civil", "settlements"})
        i = interaction()
        i.guild = None
        i.guild_id = None
        self.assertEqual(await store.allowed_books(i), set())

    async def test_hidden_titles_never_enter_search_library_or_direct_responses(self):
        await store.import_articles(100, [article(), article("secret-war", "war")])
        i = interaction()
        results = await browser.choices(i, "secret")
        self.assertEqual(results, [])
        embed, view = await browser.render(i)
        self.assertNotIn("War", embed.description)
        self.assertNotIn("war", str(view.to_components()))
        await browser.open_help(i, "secret-war")
        denied = i.followup.send.await_args
        await browser.open_help(i, "nonexistent")
        self.assertEqual(denied, i.followup.send.await_args)
        self.assertNotIn("secret", str(denied))
        self.assertTrue(denied.kwargs["ephemeral"])
        self.assertEqual(await browser.choices(interaction(guild=101, admin=True), ""), [])
        view.stop()

    async def test_revoke_between_pages_clears_old_article_and_controls(self):
        await store.import_articles(100, [article("official", "hands")])
        await acclimation_store.appoint(100, 900, 10, "hand", admin=True)
        i = interaction()
        _, view = await browser.render(i, slug="official")
        await acclimation_store.appoint(100, 900, 10, "dismissed", admin=True)
        await view.next_page.callback(i)
        self.assertEqual(i.edit_original_response.await_args.kwargs,
                         dict(content=browser.UNAVAILABLE, embed=None, view=None))
        self.assertTrue(view.is_finished())
        self.assertEqual(await browser.choices(i, "official"), [])

    async def test_navigation_reads_current_content_and_permissions(self):
        await store.import_articles(100, [article(), article("war", "war")])
        i = interaction(admin=True)
        _, view = await browser.render(i, slug="war")
        i.permissions = discord.Permissions.none()
        await view.next_page.callback(i)
        self.assertEqual(i.edit_original_response.await_args.kwargs["content"], browser.UNAVAILABLE)
        _, view = await browser.render(i, slug="public")
        await store.import_articles(100, [article(pages=["Current first", "Current second"])])
        await view.next_page.callback(i)
        payload = i.edit_original_response.await_args.kwargs
        self.assertEqual(payload["embed"].description, "Current second")
        self.assertTrue(payload["view"].next_page.disabled)
        payload["view"].stop()

    async def test_menus_are_reader_server_channel_bound_and_expire(self):
        await store.import_articles(100, [article()])
        _, view = await browser.render(interaction())
        for i in (interaction(11), interaction(guild=101)):
            await view.navigate(i)
            i.edit_original_response.assert_not_awaited()
            self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])
        i = interaction()
        i.channel_id = 56
        self.assertFalse(await view.interaction_check(i))
        view.stop()
        self.assertFalse(await view.interaction_check(interaction()))

    async def test_library_contents_pages_and_search_limits(self):
        await store.import_articles(100, [article(f"item-{n:02}") for n in range(32)])
        i = interaction()
        self.assertEqual(len(await browser.choices(i, "")), 25)
        self.assertEqual(len(await browser.choices(i, "item-31")), 1)
        embed, view = await browser.render(i, book="citizens", page=2)
        select = next(c for c in view.children if isinstance(c, browser.ArticleSelect))
        self.assertEqual(len(select.options), 10)
        await view.next_page.callback(i)
        next_view = i.edit_original_response.await_args.kwargs["view"]
        self.assertTrue(next_view.next_page.disabled)
        await next_view.contents.callback(i)
        i.edit_original_response.await_args.kwargs["view"].stop()

    async def test_errors_fail_closed_and_open_help_is_always_private(self):
        await store.import_articles(100, [article()])
        i = interaction()
        await SunsetCog(None).help_command.callback(SunsetCog(None), i)
        i.response.defer.assert_awaited_with(ephemeral=True)
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(i.followup.send.await_args.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        view = i.followup.send.await_args.kwargs["view"]
        with patch.object(store, "allowed_books", side_effect=RuntimeError("private diagnostic")):
            self.assertEqual(await browser.choices(i, ""), [])
            await browser.open_help(i)
            self.assertEqual(i.followup.send.await_args.args, (browser.TEMPORARY,))
            await view.navigate(i)
            self.assertEqual(i.edit_original_response.await_args.kwargs, dict(content=browser.TEMPORARY, embed=None, view=None))

    async def test_empty_library_and_long_text_stay_within_discord_limits(self):
        embed, view = await browser.render(interaction())
        self.assertIsNone(view)
        self.assertIn("No handbook articles", embed.description)
        records = [dict(article(f"long-{n}"), title="*" * 80, summary="*" * 90, pages=["😀" * 1750]) for n in range(10)]
        await store.import_articles(100, records)
        for target in ({"book": "citizens"}, {"slug": "long-0"}):
            embed, view = await browser.render(interaction(), **target)
            self.assertLessEqual(store.units(embed.description), 4096)
            self.assertLessEqual(len(embed), 6000)
            view.stop()
        with self.assertRaises(ValueError):
            store.validate(article(pages=["😀" * 1751]))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "needs disposable PostgreSQL")
class HandbookPostgres(StoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(db, "IS_POSTGRES", True))
        self.enterContext(patch.object(db, "DATABASE_URL", os.environ["TEST_DATABASE_URL"]))
        self.enterContext(patch.object(db, "_pool", None))
        self.enterContext(patch.object(db, "_pool_lock", asyncio.Lock()))
        self.guild = uuid4().int >> 66
        await db.init_db()
        await db._get_pool()
        self.addAsyncCleanup(db._pool.close)

    async def test_schema_is_private_with_rls_and_no_public_policies(self):
        async with db.DBConnection() as conn:
            row = await conn.fetchone("SELECT relrowsecurity AS rls,has_schema_privilege('public','sunset_handbooks','USAGE') AS exposed "
                                     "FROM pg_class WHERE oid='sunset_handbooks.articles'::regclass")
        self.assertEqual(row, dict(rls=True, exposed=False))
