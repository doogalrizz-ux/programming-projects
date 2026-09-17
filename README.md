# Programming Projects

A collection of small, self-contained projects — games, trainers, and one
household automation bot. Each project lives in its own folder and is
independent of the others; see each folder's own README for setup and usage.

## Projects

- **[HomeGhost](HomeGhost/)** — a household automation Discord bot: laundry
  timers, a family scheduler, a shared shopping list, a chore tracker, and an
  optional natural-language assistant (`@HG`) layered on top of all of them.
- **[homeghost-mcp](homeghost-mcp/)** — a practice MCP (Model Context
  Protocol) server exposing HomeGhost's SQLite database read-only, written
  to be studied as a minimal, well-commented example of the three MCP
  primitives (tools, resources, prompts).
- **[WheelOfMacgowans](WheelOfMacgowans/)** — a Wheel-of-Fortune-style party
  game (Flask + Socket.IO) with a scraped puzzle bank, a shared board display,
  and phone-based controllers for players.
- **[Simple web games](Simple%20web%20games/)** — five self-contained,
  single-file HTML5 games: Minesweeper, Missile Command, Solitaire, and two
  versions of Video Poker. No build step — just open the file.

## Notes

- Python projects each manage their own dependencies (`requirements.txt`
  where present) and expect a local virtual environment — never committed.
- Secrets (`.env` files) are gitignored everywhere; only `.env.example`
  templates are tracked.
