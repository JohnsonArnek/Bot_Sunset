# Discord handbooks

`/sunset help` opens a private library. Choose a handbook and article, then use
Previous, Next, Contents and Library. `topic` searches article titles, summaries
and slugs; autocomplete only suggests articles the member can read.

Access follows existing server permissions:

| Handbook | Readers |
| --- | --- |
| Citizens | All server members |
| Settlement leadership | Registered owners, registered officers, staff, administrators |
| Tribunes | Appointed Tribunes and administrators |
| Hands | Appointed Hands and administrators |
| Civil administration | Configured staff and administrators |
| War and logistics | Administrators |
| Bot administration | Administrators |

Staff uses `staff_role_id`, with the existing server-owner fallback when it is
unset. Appointments and settlement leadership come from this server's bot
records. A Hand appointment does not confer staff or war access. Administrators
can read every handbook; each underlying command still enforces its own rules.
For example, an officer can read settlement guidance while owner-only actions
remain owner-only.

Every open, search and navigation request checks current access. Menus belong to
the reader, server and channel that opened them; an old menu cannot keep grants
after an appointment is dismissed. The browser keeps IDs rather than cached
article bodies or permissions. Restricted titles and summaries are also filtered.
Database failures deny access. Menus expire after ten minutes of inactivity;
run the command again to reopen them. Already-read text cannot be recalled.

## Private article storage

Article titles, summaries and pages live in `sunset_handbooks.articles`, a private
PostgreSQL schema with RLS enabled and no grants to PUBLIC, anon or authenticated.
The bot's existing owner connection reads it. SQLite uses `handbook_articles`.
Startup creates the empty schema; it does not overwrite articles or install a
shared library across servers. Each article belongs to one Discord server.

Keep the content export outside the public repository. No actual handbook text
is bundled in source, fixtures or migrations. The repository contains the
browser, permission policy, schema and this operator guide. Existing README
documentation and bot source remain public; the handbook protects new article
text, not facts already published elsewhere.

## Publishing or updating articles

Use a trusted operator environment with the target `DATABASE_URL` set, or SQLite
for local work. This script has database-owner authority and is not a Discord
command. It validates every article before one atomic upsert; matching slugs are
updated and unmentioned articles remain untouched. Keep a private backup of the
current export before replacing an article.

```text
python import_handbooks.py /private/path/library.handbooks.private.json --guild SERVER_ID --check
python import_handbooks.py /private/path/library.handbooks.private.json --guild SERVER_ID
```

The file is a JSON array. Each object has `slug`, `book`, `title`, `summary`, and
`pages` (a list of Markdown strings). `book` is one of `citizens`, `settlements`,
`tribunes`, `hands`, `civil`, `war`, `admin`. Slugs contain lowercase letters,
digits and hyphens (maximum 64 characters). Titles allow 80 characters,
summaries 90, and each of 1–20 pages 3,500. Limits count UTF-16 units, so an emoji
can count twice. Titles and summaries are single lines. The importer records
the update date. Article bodies should explain purpose, eligible operators,
steps, an example, side effects and common mistakes. Label example defaults as
defaults and point to the relevant command for current settings.

Publishing content does not send messages to Discord. It becomes available when
an eligible member next opens the handbook. To remove an article, the database
operator deletes its row by **both guild_id and slug**; existing menus re-read
the database and cannot continue opening it.

When changing a command, update its affected private articles alongside the code.
Check example option names against the current command definitions. Do not add
live inventory, credentials or private server data to the public README or tests.
