import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import discord

import database as db
import library_store as store
from cogs.libraries import LibraryCog
from test_bank import BankContract, interaction


class LibraryContract:
    land = BankContract.land

    async def setup_library(self):
        land=await self.land()
        await store.setup(self.guild,10,"Keep Library","Overworld 100 64 200",admin=False,description="Open to all")
        return land

    async def add(self,title="Test Book",**kw):
        return await store.save_book(self.guild,10,admin=False,title=title,**kw)

    async def test_public_reads_and_live_owner_officer_admin_permissions(self):
        land=await self.setup_library()
        book=await self.add()
        await db.add_officer(land["id"],11)
        await store.save_book(self.guild,11,admin=False,book_id=book,copies=4)
        self.assertEqual((await store.view(self.guild,"Test Keep"))["books"][0]["copies"],4)
        with self.assertRaises(ValueError):
            await store.save_book(self.guild,12,admin=False,book_id=book,copies=0)
        with self.assertRaises(ValueError):
            await store.setup(self.guild,12,"Intruder","Elsewhere",admin=False,land_name="Test Keep")
        with self.assertRaises(ValueError):
            await store.remove(self.guild,12,book,admin=False)
        await db.remove_officer(land["id"],11)
        with self.assertRaises(ValueError):
            await store.save_book(self.guild,11,admin=False,book_id=book,copies=3)
        await db.update_land_owner(land["id"],12)
        with self.assertRaises(ValueError):
            await store.remove(self.guild,10,book,admin=False)
        await store.save_book(self.guild,12,admin=False,book_id=book,author="New Curator")
        await store.setup(self.guild,900,"Renamed","Same place",admin=True,land_name="Test Keep")
        self.assertEqual((await store.view(self.guild,"Test Keep"))["description"],"Open to all")
        self.assertEqual((await store.catalog(self.guild))["total"],1)

    async def test_book_edit_duplicates_copy_counts_and_library_updates(self):
        await self.setup_library()
        book=await self.add(title="The Chronicle",author="Enki",copies=2,description="A history")
        with self.assertRaisesRegex(ValueError,"already listed"):
            await self.add(title=" THE   CHRONICLE ",author="ENKI")
        other=await self.add(title="Another",author="Enki")
        with self.assertRaisesRegex(ValueError,"already listed"):
            await store.save_book(self.guild,10,admin=False,book_id=other,title="The Chronicle")
        await store.save_book(self.guild,10,admin=False,book_id=book,title="Revised Chronicle",copies=0,description="")
        await store.setup(self.guild,10,"Moved Library","New district",admin=False)
        rows=(await store.view(self.guild,"Test Keep"))["books"]
        self.assertEqual(len(rows),2)
        updated=next(r for r in rows if r["id"]==book)
        self.assertEqual((updated["author"],updated["copies"],updated["description"]),("Enki",0,""))
        await store.remove(self.guild,10,other,admin=False)
        self.assertEqual((await store.view(self.guild,"Test Keep"))["titles"],1)

    async def test_server_isolation_including_mutations_by_administrators(self):
        await self.setup_library()
        book=await self.add()
        other_guild=self.guild+1
        self.assertEqual((await store.catalog(other_guild))["total"],0)
        self.assertEqual((await store.search(other_guild,"Test"))["total"],0)
        with self.assertRaises(ValueError):
            await store.view(other_guild,"Test Keep")
        with self.assertRaises(ValueError):
            await store.save_book(other_guild,900,admin=True,book_id=book,copies=999)
        with self.assertRaises(ValueError):
            await store.remove(other_guild,900,book,admin=True)
        self.assertEqual((await store.view(self.guild,"Test Keep"))["books"][0]["copies"],1)

    async def test_search_literal_wildcards_unicode_and_pagination(self):
        await self.setup_library()
        for n in range(7):
            await self.add(title=f"Chronicle {n}",author="Scribe")
        results=await store.search(self.guild,"sCrIbE",page=2)
        self.assertEqual(results["total"],7)
        self.assertEqual([b["title"] for b in results["books"]],["Chronicle 3","Chronicle 4","Chronicle 5"])
        self.assertEqual((await store.view(self.guild,"Test Keep",page=3))["books"][0]["title"],"Chronicle 6")
        await self.add(title="100%_history!")
        self.assertEqual((await store.search(self.guild,"%_"))["total"],1)
        await self.add(title="Café")
        with self.assertRaises(ValueError):
            await self.add(title="CAFE\u0301")
        self.assertEqual((await store.search(self.guild,"CAFE\u0301"))["total"],1)
        self.assertEqual((await store.view(self.guild,"Test Keep",page=100))["books"],[])

    async def test_concurrent_duplicate_add_and_deleted_land_cleanup(self):
        land=await self.setup_library()
        results=await asyncio.gather(self.add(),self.add(),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,ValueError) for x in results),1)
        await db.delete_land(land["id"])
        async with db.DBConnection() as conn:
            for name in ("libraries","books"):
                self.assertEqual((await conn.fetchone(f"SELECT COUNT(*) AS n FROM {store.table(name)} WHERE guild_id=?",(self.guild,)))["n"],0)
        await self.land()
        with self.assertRaises(ValueError):
            await store.view(self.guild,"Test Keep")
        self.assertEqual((await store.search(self.guild,"Test"))["total"],0)

    async def test_validation_limits_and_multiple_land_selection(self):
        land=await self.land()
        with self.assertRaisesRegex(ValueError,"set first"):
            await self.add()
        await store.setup(self.guild,10,"Library","Location",admin=False)
        for args in (dict(title=" "),dict(author=""),dict(copies=-1),dict(copies=True),dict(copies=1_000_001),
                     dict(title="😀"*41),dict(description="x"*301)):
            with self.assertRaises(ValueError):
                await self.add(**args)
        with self.assertRaises(ValueError):
            await store.catalog(self.guild,page=0)
        with self.assertRaises(ValueError):
            await store.search(self.guild," ")
        book=await self.add()
        with self.assertRaises(ValueError):
            await store.save_book(self.guild,10,admin=False,book_id=book)
        with patch.object(store,"MAX_BOOKS",1),self.assertRaises(ValueError):
            await self.add(title="Too many")
        other=await self.land(owner=20,name="Second")
        await db.add_officer(other["id"],10)
        with self.assertRaisesRegex(ValueError,"multiple"):
            await store.setup(self.guild,10,"Ambiguous","Somewhere",admin=False)
        await store.setup(self.guild,10,"Second Library","Other place",admin=False,land_name="Second")
        self.assertEqual((await store.catalog(self.guild))["total"],2)


class LibrarySQLite(LibraryContract,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(db,"IS_POSTGRES",False))
        self.enterContext(patch.object(db,"DB_PATH",str(Path(temp.name)/"test.db")))
        self.guild=100
        await db.init_db()

    async def test_public_browsing_private_management_and_mentions_disabled(self):
        await self.land()
        cog=LibraryCog()
        i=interaction(self.guild)
        await cog.set_library.callback(cog,i,"Local Library","Town square")
        i.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        await cog.add.callback(cog,i,"@everyone")
        reader=interaction(self.guild,user=99)
        await cog.view.callback(cog,reader,"Test Keep")
        reader.response.defer.assert_awaited_once_with(ephemeral=False)
        self.assertFalse(reader.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(reader.followup.send.await_args.kwargs["allowed_mentions"].to_dict(),discord.AllowedMentions.none().to_dict())
        await cog.edit.callback(cog,reader,1,copies=0)
        self.assertIn("Only this land",reader.followup.send.await_args.kwargs["content"])

    async def test_maximum_catalogue_text_fits_discord_embeds(self):
        await self.land()
        await store.setup(self.guild,10,"*"*80,"*"*200,admin=False,description="*"*300)
        for n in range(3):
            await self.add(title=str(n)+"*"*79,author="*"*80,description="*"*300,copies=1_000_000)
        cog=LibraryCog()
        i=interaction(self.guild,user=99)
        for callback,args in ((cog.view.callback,("Test Keep",)),(cog.search.callback,("*",)),(cog.list_libraries.callback,())):
            await callback(cog,i,*args)
            embed=i.followup.send.await_args.kwargs["embed"]
            self.assertLessEqual(len(embed),6000)
            self.assertLessEqual(len(embed.description or ""),4096)
            for field in embed.fields:
                self.assertLessEqual(len(field.name),256)
                self.assertLessEqual(len(field.value),1024)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),"needs disposable PostgreSQL")
class LibraryPostgres(LibraryContract,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(db,"IS_POSTGRES",True))
        self.enterContext(patch.object(db,"DATABASE_URL",os.environ["TEST_DATABASE_URL"]))
        self.enterContext(patch.object(db,"_pool",None))
        self.enterContext(patch.object(db,"_pool_lock",asyncio.Lock()))
        self.guild=uuid4().int>>66
        await db.init_db()
        self.addAsyncCleanup(db._pool.close)

    async def test_catalogue_storage_is_private_and_rls_enabled(self):
        async with db.DBConnection() as conn:
            row=await conn.fetchone("SELECT bool_and(relrowsecurity) AS rls,count(*) AS tables,"
                "has_schema_privilege('public','sunset_libraries','USAGE') AS public_access "
                "FROM pg_class WHERE relnamespace='sunset_libraries'::regnamespace AND relkind='r'")
        self.assertEqual(row,dict(rls=True,tables=2,public_access=False))
