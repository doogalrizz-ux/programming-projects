# homeghost-mcp

A practice MCP (Model Context Protocol) server, built on top of HomeGhost's
own SQLite database. Read-only, single-file, meant to be studied.

## What MCP actually is, in one paragraph

MCP is a standard protocol for connecting an AI assistant (an "MCP client"
— Claude Desktop, Claude Code, etc.) to external tools and data, over a
well-defined JSON-RPC interface, instead of every integration being a
one-off custom API. You write a server that exposes some combination of
three primitive types; the client discovers what's available and calls
into your server as needed. That's the whole idea — everything else is
detail.

## The three primitives, mapped to this code

- **Tools** — actions/queries the model can actively invoke, each with a
  typed signature. `get_laundry_status`, `get_todays_schedule`,
  `get_week_schedule`, and `search_events` in `server.py` are all tools
  (`@server.tool()`). The function's type hints and docstring *are* the
  interface — the SDK generates the JSON schema the client sees from
  them, so a good docstring isn't just nice manners here, it's the
  primary thing the model reads to decide when and how to call it.
- **Resources** — passive, addressable data the client can pull in as
  context, identified by a URI, without an explicit function-call
  round-trip. `household_status` (`@server.resource("homeghost://status")`)
  is the one example here — think of it as "a document," not "an action."
- **Prompts** — reusable prompt templates the client can offer to the
  user (e.g. as a slash command or menu item). `daily_briefing` is the
  one example — it doesn't touch the database itself, it just returns
  instruction text pointing the model at the tools/resources above.

## Key design decisions worth noticing

- **Read-only, enforced structurally, not by convention.** `_connect_ro()`
  opens the database via a SQLite URI with `?mode=ro` — this process is
  physically incapable of writing to HomeGhost's live data, not just
  "supposed to" avoid it. Worth doing any time an MCP server sits on top
  of another system's data store that you don't want to risk corrupting.
- **Safe to run alongside the real bot.** HomeGhost's `database.py` puts
  the DB in WAL mode specifically so concurrent readers don't block the
  writer (or vice versa) — this server is exactly that kind of reader.
- **stdio transport.** `server.run(transport="stdio")` — the client
  launches this script as a subprocess and talks to it over stdin/stdout.
  This is the simplest transport and what both Claude Desktop and the
  Inspector use for local servers. (The other options, `sse` and
  `streamable-http`, are for a server running as a separate network
  service instead of a local subprocess — not needed here.)
- **Duplicated date/recurrence logic.** The occurrence-expansion logic
  here is a deliberately standalone copy of what's in
  `HomeGhost/cogs/scheduler.py`, not an import from it — kept this a
  self-contained file to read top-to-bottom. In a real project you'd
  factor shared logic like this into a common package instead.

## Setup

```powershell
cd "D:\Programing Projects\homeghost-mcp"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```
The defaults in `.env.example` already point at `../HomeGhost/homeghost.db`,
so if this folder sits next to `HomeGhost/` (it does by default), you
likely don't need to change anything.

## Testing it — three ways, easiest first

**1. MCP Inspector** (no client needed, fastest feedback loop):
```powershell
mcp dev server.py
```
Opens a local web UI where you can call each tool individually, read the
resource, and see the exact JSON going over the wire. This is the fastest
way to iterate while building — start here.

**2. Install into Claude Desktop:**
```powershell
mcp install server.py --name homeghost
```
Restart Claude Desktop afterward. You should be able to ask it things
like "what's on the schedule today?" or "is anything in the laundry
waiting to be moved?" and see it call these tools.

**3. Direct run** (what a client actually launches under the hood):
```powershell
python server.py
```
This just sits waiting for JSON-RPC messages on stdin — not useful to
run by hand, but it's exactly what `mcp install` configures Claude
Desktop to execute for you.

## Suggested next reps, roughly in order of difficulty

1. Add a `get_person_schedule(person: str)` tool — practice a second
   parameterized tool.
2. Make a tool return structured data (a `dict`/Pydantic model) instead
   of a formatted string, and see how that shows up differently in the
   Inspector than plain text does.
3. Add a genuine **write** tool (e.g. `acknowledge_laundry(appliance: str)`)
   — this means dropping the read-only URI for a real connection, so it's
   a good forcing function to think about validation and error handling
   before you let a model write to real data.
4. Add basic logging so you can see exactly what a client called and
   when — useful both for debugging and for talking through "how would
   you observe this in production" in an interview.
5. Try the `streamable-http` transport instead of `stdio` and run it as
   a standalone service rather than a subprocess — this is the shape a
   "real" deployed MCP server takes.
