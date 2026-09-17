# HomeGhost

A household automation Discord bot with three skills so far:

- **Laundry** — start a timer on the washer/dryer, get pinged when it's
  done, and get nagged (with escalating pings) until someone acknowledges it.
- **Family scheduler** — a shared calendar for appointments, practices,
  work shifts, and anything else, entered entirely through slash commands
  in one dedicated channel. Flags scheduling conflicts and can ping
  whoever needs a reminder before an event starts.
- **Shopping list** — one shared, per-store list (replacing a shared
  spreadsheet), added to via slash commands and checked off with a
  reaction or a button.
- **Chore list** — assign chores to people and track completion, in a
  dedicated channel. `/chlist` shows your own list by default (or
  anyone else's by name) — unlike the shopping list, this one's personal.
- **@HG assistant** (optional) — a natural-language bridge on top of
  the other skills. @-mention the bot in plain English ("HG, add milk
  to the list") and it translates that into the same actions the slash
  commands run, using Gemini. Strictly a translator, not a participant
  — see [Using @HG](#using-hg) below for what that means in practice.

Built to be extensible — each skill lives in its own cog under `cogs/` and
shares one bot instance and one SQLite database (`database.py`), so
chess / etc. can be added later as new files without touching what's
already here. `nlp_registry.py` extends that same idea to @HG: a skill
opts a few of its own functions into natural-language control without
the assistant needing to know that skill exists.

## Project layout

```
HomeGhost/
  bot.py            entry point — builds the bot, loads cogs, syncs commands
  config.py         loads settings from .env
  database.py       shared SQLite connection, used by every skill
  nlp_registry.py   lets any skill opt into @HG without @HG knowing about it
  cogs/
    laundry.py      the laundry skill (timers, reminders, escalation)
    scheduler.py    the family scheduler (events, conflicts, reminders)
    shopping.py     the shopping list (per-store items, reaction/button checkoff)
    chores.py       the chore list (per-person assignments, button checkoff)
    assistant.py    @HG — the natural-language bridge (optional)
  .env.example      copy to .env and fill in
  requirements.txt
```

## 1. Create the bot in Discord's Developer Portal

1. Go to https://discord.com/developers/applications → **New Application**.
   Name it whatever you like (e.g. "HomeGhost").
2. Open the **Bot** tab. Click **Reset Token** (or **Copy** if a token is
   already shown) and save it somewhere safe — you'll put it in `.env` as
   `DISCORD_TOKEN`. Treat it like a password; anyone with it controls the
   bot.
3. **Privileged Gateway Intents**: leave all three toggles (Presence,
   Server Members, Message Content) **off**. This bot only uses slash
   commands and reactions, neither of which needs a privileged intent.
4. Open the **OAuth2 → URL Generator** tab:
   - **Scopes**: check `bot` and `applications.commands`.
   - **Bot Permissions**: check `Send Messages`, `Read Message History`,
     `Add Reactions`, `Embed Links`, `View Channels`. (Pinging a specific
     person doesn't need the "Mention Everyone" permission — that's only
     for `@everyone`/`@here`.)
   - Copy the generated URL at the bottom, open it in a browser, pick your
     server, and authorize it.

## 2. Get the IDs you need

Enable Developer Mode first: **User Settings → Advanced → Developer Mode**.

- **REMINDER_CHANNEL_ID**: right-click the channel reminders should post
  in → **Copy Channel ID**.
- **ESCALATION_USER_ID**: right-click the person who should get pulled in
  after unacknowledged reminders (e.g. a parent) → **Copy User ID**.
- **GUILD_ID** (optional but recommended while developing): right-click
  your server's icon → **Copy Server ID**. When set, slash commands sync
  to that server instantly; otherwise global sync can take up to an hour
  to show up.
- **SCHEDULE_CHANNEL_ID**: right-click the channel scheduling commands
  should live in → **Copy Channel ID**.
- **SHOPPING_CHANNEL_ID**: right-click the channel the shopping list
  should live in → **Copy Channel ID**.
- **CHORE_CHANNEL_ID**: right-click the channel chores should live in
  → **Copy Channel ID**.

You'll also set `FAMILY_MEMBERS` (a plain comma-separated list of names —
no Discord IDs needed, this just drives a dropdown), `TIMEZONE` (an
IANA name like `America/New_York`), `SHOPPING_STORES` (comma-separated,
first one is the default), and optionally `KIDS` (a subset of
`FAMILY_MEMBERS`, so @HG can expand "assign X to the kids" on its own)
directly in `.env`, no portal lookup required.

**Optional — only if you want @HG:** get a `GEMINI_API_KEY` from
[aistudio.google.com](https://aistudio.google.com/) (sidebar → "Get API
key" → "Create API key" — the free tier needs no billing setup at all).
Leave it blank in `.env` and HomeGhost skips loading @HG entirely; every
other skill works the same either way. If you do set it, also flip on
**Message Content** under Privileged Gateway Intents in the Bot tab —
@HG needs to read actual message text, unlike everything else in this
bot, which only needed slash commands and reactions.

## 3. Install and configure

```bash
cd "HomeGhost"
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt

copy .env.example .env          # Windows
# cp .env.example .env          # macOS/Linux
```

Edit `.env` and fill in `DISCORD_TOKEN`, `REMINDER_CHANNEL_ID`,
`ESCALATION_USER_ID`, `SCHEDULE_CHANNEL_ID`, `FAMILY_MEMBERS`, `TIMEZONE`,
`SHOPPING_CHANNEL_ID`, `SHOPPING_STORES`, `CHORE_CHANNEL_ID`, and
(recommended for dev) `GUILD_ID`. `REMINDER_INTERVAL_MINUTES` (default 5)
and `ESCALATION_THRESHOLD` (default 3) can stay at their defaults.
`KIDS` and `GEMINI_API_KEY` are both optional — leave either blank to
skip what they enable.

## 4. Run it

```bash
python bot.py
```

You should see log lines for connecting to SQLite, loading the laundry,
scheduler, and shopping cogs, syncing slash commands, and logging in. In
Discord, typing `/` in a channel the bot can see should show `/washer`,
`/dryer`, `/timer`, `/done`, `/laundry status`, `/schedtoday`,
`/schedweek`, `/schedule` (with `add`, `add-recurring`, `edit`,
`edit-recurring`, `remove`, `remove-recurring`, `list-recurring`, and
`person` subcommands), and `/shop` (with `add`, `move`, `got`, `remove`,
`clear-checked`, and `list` subcommands).

## Using it

- `/washer 45` / `/dryer 60` — start a timer, tagged to you. Starting a
  new timer on an appliance that already has one running **replaces** the
  old one (no confirmation prompt — kept simple for v1).
- `/timer appliance:dishwasher minutes:90` — same thing for any other
  appliance name.
- When a timer finishes, the bot posts in the configured reminder channel,
  pings whoever started it, and reacts with ✅ on its own message.
- If nobody acknowledges, it reposts every `REMINDER_INTERVAL_MINUTES`
  (default 5). Once the reminder count hits `ESCALATION_THRESHOLD`
  (default 3), the escalation contact gets pinged too, on every reminder
  after that.
- Acknowledge either by running `/done` (or `/done washer` if more than
  one load is waiting) or by reacting ✅ on the reminder message.
- `/laundry status` — lists every running timer (with time remaining) and
  every finished-but-unacknowledged load (with how many reminders have
  gone out).

## Using the family scheduler

All scheduling commands only work in the channel set as
`SCHEDULE_CHANNEL_ID` — used anywhere else, the bot politely redirects
you there instead of running.

- `/schedule add person:Sam category:Doctor title:"Dentist" date:2026-09-15 start:3:00pm end:4:00pm`
  — a one-off event. `date`/`start`/`end` accept flexible formats
  (`3pm`, `15:00`, `2026-09-15`), not just ISO.
- `/schedule add-recurring person:Sam category:Karate title:"Karate class" weekday:Tuesday start:5:00pm end:6:00pm`
  — a standing weekly event. Optional `starts_on`/`ends_on` bound it (e.g.
  a season that ends in December).
- Add `remind_minutes_before:15 remind_users:"@Mom @Dad"` to either command
  to get a ping before it starts — mention anywhere from 0 to several
  people; if you set one of `remind_minutes_before`/`remind_users` you
  must set the other.
- `/schedtoday` / `/schedweek` — fast views of today's or this week's
  full family schedule.
- `/schedule person person:Sam` — just one person's next 30 days.
- `/schedule list-recurring` — see every standing weekly event and its
  `R#id` (needed to edit/remove one).
- `/schedule edit id:<#>` / `/schedule edit-recurring id:<R#>` — change
  any field on an existing event; add `clear_reminder:true` to remove a
  reminder from it.
- `/schedule remove id:<#>` / `/schedule remove-recurring id:<R#>` —
  delete one. One-off events use plain `#id`, standing weekly events use
  `R#id` — both ids are shown in every listing.

Adding an event that overlaps another (regardless of which family member
each is for — the point is whether the household can be in two places at
once) gets a warning in the response, but it's never blocked; sometimes
two overlapping things are genuinely fine (parents splitting up).

## Using the shopping list

All `/shop` commands only work in the channel set as `SHOPPING_CHANNEL_ID`.
Each active item is its own message in that channel — no reaction is
pre-attached when you add something, since there's nothing to check off
the moment you've just decided you need it.

- `/shop add item:milk quantity:5` — adds to the default store (the
  first one listed in `SHOPPING_STORES`, e.g. ACME). Adding an item
  that's already active on that store's list (matched case-insensitively)
  **replaces** its quantity and edits the existing message in place,
  rather than creating a duplicate line — `/shop add item:milk quantity:5`
  then later `/shop add item:milk quantity:2` leaves you with milk x2,
  not x7.
- `/shop add item:beer quantity:2 store:BOOZESTORE` — same thing, on a
  specific store's list instead of the default.
- `/shop move item:milk store:SHOPRITE` — moves an item to a different
  store's list. If the same item name happens to be active on more than
  one list at once, it'll ask you to specify `from_store`.
- `/shop list` — an interactive view of the default store's list: each
  active item gets its own tap-to-check-off button, and checked-off
  items show struck-through at the bottom instead of disappearing.
  `/shop list store:"All Stores"` groups everything by store. You can
  also react ✅ directly on an item's own message, or run
  `/shop got item:milk` — either works without needing the list view open.
- Checked-off items stay struck-through and visible, not deleted —
  `/shop clear-checked` removes them (optionally scoped to one store)
  once you're done reviewing what got grabbed.
- `/shop remove item:milk` — deletes an item outright (added by
  mistake, no longer needed) without marking it checked off.

## Using the chore list

All chore commands only work in the channel set as `CHORE_CHANNEL_ID`.
Structurally this is the shopping list's pattern reused — no reaction
on assignment, an interactive list view, completed entries stay
struck-through until cleared — with one real difference: a chore list
is personal, so `/chlist` shows *your own* chores by default rather
than one shared household list.

- `/chore assign chore:"mow the lawn" person:Daddy` — assigns it.
  Assigning the exact same chore to the same person again while it's
  still active is a no-op, not a duplicate.
- `/chlist` — your own chores (matched via your server nickname against
  `FAMILY_MEMBERS`), with tap-to-complete buttons on each active one.
  `/chlist person:Mommy` shows someone else's; `/chlist person:Everyone`
  groups the whole household's board by person.
- `/chore done chore:"mow the lawn"` — marks it done; defaults to your
  own chores the same way `/chlist` does, or specify `person` to mark
  someone else's on their behalf.
- `/chore remove` / `/chore clear-done` — delete an assignment outright,
  or clear out completed ones (optionally scoped to one person).
- Via @HG: `@HomeGhost assign mow lawn to Daddy`, or `@HomeGhost assign
  laundry to each of the kids` — the latter needs `KIDS` set in `.env`
  so @HG knows who "the kids" are; it then creates one separate,
  independently-completable chore per kid, not one shared entry.

## Using @HG

Only active if `GEMINI_API_KEY` is set. Unlike every other command in
this bot, @HG isn't scoped to one channel — @-mention the bot anywhere
it can see:

```
@HomeGhost add milk 2 to the list
@HomeGhost thanks!
```

**Important:** typing `@HomeGhost` (or its server nickname) isn't enough
by itself — Discord shows a member popup as you type; you have to
actually **click that entry** (or arrow-down + Tab/Enter) before
continuing your sentence. If you type through the popup without
selecting it, Discord leaves it as plain text that merely *looks*
similar, and the bot never sees a real mention at all — it'll just sit
there silently, since correctly ignoring non-mentions is the intended
behavior everywhere else. Look for the mention to render as a solid
highlighted pill before you hit enter.

What actually happens under the hood, so a wrong answer is debuggable
rather than mysterious:

1. One Gemini call turns your message into zero or more actions (or,
   for pure chit-chat, straight into a reply — nothing gets touched).
   Naming several items in one message becomes several actions, not one.
2. Each action really runs, via the exact same code its slash command
   uses — there's no separate, second implementation to drift out of
   sync.
3. A second Gemini call turns the *real* outcome into a short, in-
   character reply — it only runs after the real result is known, so
   it can't confirm something that actually failed or hit a conflict.

**@HG is a translator, never a participant.** It only ever runs actions
a skill explicitly opted into exposing via `nlp_registry.register(...)`
(see `cogs/shopping.py`'s `cog_load` for the pattern) — currently
`shop_add_item`, `shop_check_off_item`, `chore_assign`, and
`chore_complete`. It will never make a decision on anyone's behalf; a
future skill like chess would register "start a game" but deliberately
not "make a move."

**Channel scoping prevents cross-skill mix-ups.** A message sent in a
skill's own dedicated channel (the chore channel, the shopping channel)
only ever offers that skill's actions to the model — a chore-shaped
request in the chore channel can't get misread as a shopping-list item,
because shopping's actions are never even shown as an option there.
This is enforced structurally (`nlp_registry.actions_for_channel`), not
just by hoping the wording disambiguates correctly. Outside any skill's
dedicated channel, every action stays available, same as before.

**Resolving "me"/"my"**: @HG matches your server nickname against
`FAMILY_MEMBERS` to know who's speaking. If your nickname is `Dad` and
`Dad` is in your roster, "add a doctor's appointment for me" resolves
correctly; if it doesn't match anything, it just won't have a name to
attribute the action to.

**A daily cap** (`ASSISTANT_MAX_DAILY_REQUESTS`, default 500) exists
purely as a safety net against a bug or abuse causing runaway paid API
calls — not a realistic ceiling for normal household use.

## Notes on persistence and restarts

Timers are stored in SQLite as an absolute start time + duration, not a
countdown. That means restarting the bot mid-timer loses nothing — on its
next check (every 20 seconds), the background loop just compares the
current time against what's already in the database. No special recovery
code needed.

## What's intentionally not here yet

- No natural-language parsing — slash commands only.
- No smart-plug / WiFi appliance integration. The timer-start logic is
  factored into `Laundry.start_timer()` specifically so a future
  sensor-based trigger can call straight into it instead of going through
  a slash command.
- No other skills yet beyond laundry, scheduling, shopping, and chores.
  Chess, etc. would each be a new file in `cogs/`, added to
  `INITIAL_EXTENSIONS` in `bot.py`, using `bot.db` for their own tables.
- @HG currently only has shopping's and chores' actions wired in —
  laundry and the scheduler aren't registered with `nlp_registry` yet.
  Same pattern, just not done yet.
- No recurring chores (a standing "take out trash every Tuesday") or due
  dates — every chore is a one-off assignment, matching how the shopping
  list launched before scheduling-style features were added.
- No email/text ingestion for the scheduler — everything is entered via
  slash command, by design (no NLP to translate freeform text).
- No skipping a single occurrence of a recurring event (e.g. "karate's
  cancelled just this one week") — you'd currently remove and re-add the
  whole series. An exceptions table would be a clean follow-up.

## Running it long-term

For now this is meant to run locally with `python bot.py` in a terminal
you keep open. When you're ready to move it to a home server for 24/7
uptime, wrap it in a process manager (e.g. a Windows Scheduled Task /
service, `systemd`, or `pm2`) so it restarts automatically — no code
changes needed for that since state already survives restarts.
