"""
Wheel of Macgowans — Game State
Manages all game logic: rounds, turns, wheel, letters, scoring.
"""

import csv
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path


# ── Wheel wedges ────────────────────────────────────────────────────────────

WHEEL_WEDGES = [
    500, 550, 600, 650, 700, 750, 800, 850,
    900, 1000, 1000, 1500, 1500, 2000, 2500,
    3500, 5000,
    "BANKRUPT", "BANKRUPT",
    "LOSE A TURN",
    600, 700, 800, 900,
]
random.shuffle(WHEEL_WEDGES)   # randomize order at startup

VOWEL_COST = 250
VOWELS = set("AEIOU")
CONSONANTS = set("BCDFGHJKLMNPQRSTVWXYZ")
TOTAL_ROUNDS = 5
MAX_PLAYERS = 3


# ── Game phases ──────────────────────────────────────────────────────────────

class Phase(str, Enum):
    LOBBY        = "lobby"
    SPINNING     = "spinning"
    LETTER_PICK  = "letter_pick"
    BUY_VOWEL    = "buy_vowel"
    SOLVING      = "solving"
    ROUND_END    = "round_end"
    GAME_OVER    = "game_over"


# ── Player ───────────────────────────────────────────────────────────────────

@dataclass
class Player:
    name: str
    sid: str            # SocketIO session id
    slot: int           # 0, 1, or 2

    banked: int = 0         # total money banked across all rounds
    round_money: int = 0    # money earned this round (lost on bankrupt)
    connected: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slot": self.slot,
            "banked": self.banked,
            "round_money": self.round_money,
            "connected": self.connected,
        }


# ── Puzzle ───────────────────────────────────────────────────────────────────

@dataclass
class Puzzle:
    text: str       # e.g. "WHEEL OF FORTUNE"
    category: str
    date: str       # original air date


# ── Game ─────────────────────────────────────────────────────────────────────

class Game:
    def __init__(self, puzzle_path: str = "puzzles.csv"):
        self._all_puzzles: list[Puzzle] = _load_puzzles(puzzle_path)
        self._used_puzzle_indices: set[int] = set()

        self.players: list[Player] = []       # in slot order
        self._sid_to_slot: dict[str, int] = {}

        self.phase: Phase = Phase.LOBBY
        self.round_num: int = 0               # 1-5 during play

        # Current puzzle state
        self.puzzle: Puzzle | None = None
        self.revealed: set[int] = set()       # indices of revealed letters
        self.used_letters: set[str] = set()   # letters already guessed

        # Turn state
        self.current_slot: int = 0
        self.last_spin: int | str | None = None   # wedge value or "BANKRUPT" etc.

    # ── Lobby ────────────────────────────────────────────────────────────────

    def add_player(self, name: str, sid: str) -> Player | None:
        """Add a player during lobby phase. Returns Player or None if full."""
        if len(self.players) >= MAX_PLAYERS:
            return None
        available_slots = [i for i in range(MAX_PLAYERS)
                           if i not in {p.slot for p in self.players}]
        slot = random.choice(available_slots)
        p = Player(name=name.strip()[:20], sid=sid, slot=slot)
        self.players.append(p)
        self.players.sort(key=lambda x: x.slot)
        self._sid_to_slot[sid] = slot
        return p

    def remove_player(self, sid: str) -> None:
        """Mark player as disconnected but keep them in the game for reconnection."""
        player = self.player_by_sid(sid)
        if player:
            player.connected = False

    def reconnect_player(self, name: str, new_sid: str) -> 'Player | None':
        """Find a player by name and update their sid (handles phone reconnects)."""
        name_clean = name.strip().lower()
        for p in self.players:
            if p.name.lower() == name_clean:
                if p.sid in self._sid_to_slot:
                    del self._sid_to_slot[p.sid]
                p.sid = new_sid
                p.connected = True
                self._sid_to_slot[new_sid] = p.slot
                return p
        return None

    def player_by_sid(self, sid: str) -> Player | None:
        slot = self._sid_to_slot.get(sid)
        if slot is None:
            return None
        return next((p for p in self.players if p.slot == slot), None)

    def can_start(self) -> bool:
        return len(self.players) == MAX_PLAYERS and self.phase == Phase.LOBBY

    # ── Round management ─────────────────────────────────────────────────────

    def start_game(self) -> bool:
        if not self.can_start():
            return False
        self.round_num = 0
        self._advance_round()
        return True

    def _advance_round(self) -> bool:
        self.round_num += 1
        if self.round_num > TOTAL_ROUNDS:
            self.phase = Phase.GAME_OVER
            return False

        # Reset round money for all players
        for p in self.players:
            p.round_money = 0

        # Pick a new puzzle
        self.puzzle = self._pick_puzzle()
        self.revealed = set()
        self.used_letters = set()
        self.last_spin = None

        # Starting player rotates each round
        self.current_slot = (self.round_num - 1) % len(self.players)
        self.phase = Phase.SPINNING
        return True

    def _pick_puzzle(self) -> Puzzle:
        available = [i for i in range(len(self._all_puzzles))
                     if i not in self._used_puzzle_indices]
        if not available:
            # All puzzles used — reset (shouldn't happen with 10k+ puzzles)
            self._used_puzzle_indices.clear()
            available = list(range(len(self._all_puzzles)))
        idx = random.choice(available)
        self._used_puzzle_indices.add(idx)
        return self._all_puzzles[idx]

    # ── Turn actions ─────────────────────────────────────────────────────────

    def spin(self, sid: str) -> dict:
        """Player spins the wheel. Returns result dict."""
        if not self._is_current_player(sid):
            return {"ok": False, "reason": "not_your_turn"}
        if self.phase != Phase.SPINNING:
            return {"ok": False, "reason": "wrong_phase"}

        wedge_idx = random.randrange(len(WHEEL_WEDGES))
        wedge = WHEEL_WEDGES[wedge_idx]
        self.last_spin = wedge

        if wedge == "BANKRUPT":
            player = self.player_by_sid(sid)
            player.round_money = 0
            self._next_turn()
            return {"ok": True, "wedge": wedge, "wedge_index": wedge_idx, "event": "bankrupt"}

        if wedge == "LOSE A TURN":
            self._next_turn()
            return {"ok": True, "wedge": wedge, "wedge_index": wedge_idx, "event": "lose_a_turn"}

        # Normal dollar wedge — player picks a letter
        self.phase = Phase.LETTER_PICK
        return {"ok": True, "wedge": wedge, "wedge_index": wedge_idx, "event": "pick_letter"}

    def pick_letter(self, sid: str, letter: str) -> dict:
        """Player picks a consonant after spinning."""
        if not self._is_current_player(sid):
            return {"ok": False, "reason": "not_your_turn"}
        if self.phase != Phase.LETTER_PICK:
            return {"ok": False, "reason": "wrong_phase"}

        letter = letter.upper()
        if letter not in CONSONANTS:
            return {"ok": False, "reason": "not_a_consonant"}
        if letter in self.used_letters:
            return {"ok": False, "reason": "already_used"}

        self.used_letters.add(letter)
        count = self._reveal_letter(letter)

        player = self.player_by_sid(sid)
        if count > 0:
            earnings = count * self.last_spin
            player.round_money += earnings
            if self._puzzle_solved():
                return {"ok": True, "letter": letter, "count": count,
                        "earnings": earnings, "event": "puzzle_solved"}
            # Player can spin again or solve
            self.phase = Phase.SPINNING
            return {"ok": True, "letter": letter, "count": count,
                    "earnings": earnings, "event": "letter_correct"}
        else:
            # No match — next player
            self._next_turn()
            return {"ok": True, "letter": letter, "count": 0,
                    "earnings": 0, "event": "letter_wrong"}

    def buy_vowel(self, sid: str) -> dict:
        """Initiate vowel purchase. Deducts $250, transitions to vowel pick phase."""
        if not self._is_current_player(sid):
            return {"ok": False, "reason": "not_your_turn"}
        if self.phase not in (Phase.SPINNING, Phase.LETTER_PICK):
            return {"ok": False, "reason": "wrong_phase"}

        player = self.player_by_sid(sid)
        if player.round_money < VOWEL_COST:
            return {"ok": False, "reason": "not_enough_money"}

        remaining_vowels = VOWELS - self.used_letters
        # Check there are actually vowels left in the puzzle
        puzzle_vowels = {c for c in self.puzzle.text if c in VOWELS}
        buyable = remaining_vowels & puzzle_vowels
        if not buyable and not remaining_vowels:
            return {"ok": False, "reason": "no_vowels_left"}

        player.round_money -= VOWEL_COST
        self.phase = Phase.BUY_VOWEL
        return {"ok": True, "event": "pick_vowel",
                "available_vowels": sorted(remaining_vowels)}

    def pick_vowel(self, sid: str, letter: str) -> dict:
        """Player picks a vowel after buying."""
        if not self._is_current_player(sid):
            return {"ok": False, "reason": "not_your_turn"}
        if self.phase != Phase.BUY_VOWEL:
            return {"ok": False, "reason": "wrong_phase"}

        letter = letter.upper()
        if letter not in VOWELS:
            return {"ok": False, "reason": "not_a_vowel"}
        if letter in self.used_letters:
            return {"ok": False, "reason": "already_used"}

        self.used_letters.add(letter)
        count = self._reveal_letter(letter)

        if self._puzzle_solved():
            return {"ok": True, "letter": letter, "count": count,
                    "event": "puzzle_solved"}

        # After buying a vowel, player must spin next (standard WoF rules)
        self.phase = Phase.SPINNING
        return {"ok": True, "letter": letter, "count": count,
                "event": "vowel_revealed"}

    def solve_attempt(self, sid: str, answer: str) -> dict:
        """Player attempts to solve the puzzle."""
        if not self._is_current_player(sid):
            return {"ok": False, "reason": "not_your_turn"}
        if self.phase not in (Phase.SPINNING, Phase.LETTER_PICK, Phase.BUY_VOWEL):
            return {"ok": False, "reason": "wrong_phase"}

        answer_clean = answer.strip().upper()
        correct = answer_clean == self.puzzle.text.upper()

        if correct:
            # Reveal entire puzzle
            self.revealed = set(range(len(self.puzzle.text)))
            player = self.player_by_sid(sid)
            player.banked += player.round_money
            player.round_money = 0
            self.phase = Phase.ROUND_END
            return {"ok": True, "correct": True, "event": "puzzle_solved",
                    "solver": player.to_dict()}
        else:
            self._next_turn()
            return {"ok": True, "correct": False, "event": "wrong_solve"}

    def next_round(self) -> dict:
        """Advance to the next round (called after round_end)."""
        if self.phase != Phase.ROUND_END:
            return {"ok": False, "reason": "wrong_phase"}
        self._advance_round()
        return {"ok": True, "round": self.round_num, "phase": self.phase}

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _is_current_player(self, sid: str) -> bool:
        player = self.player_by_sid(sid)
        return player is not None and player.slot == self.current_slot

    def _next_turn(self) -> None:
        n = len(self.players)
        self.current_slot = (self.current_slot + 1) % n
        self.phase = Phase.SPINNING

    def _reveal_letter(self, letter: str) -> int:
        """Reveal all instances of letter in puzzle. Returns count revealed."""
        count = 0
        for i, ch in enumerate(self.puzzle.text):
            if ch == letter and i not in self.revealed:
                self.revealed.add(i)
                count += 1
        return count

    def _puzzle_solved(self) -> bool:
        """True if all non-space, non-special characters are revealed."""
        for i, ch in enumerate(self.puzzle.text):
            if ch.isalpha() and i not in self.revealed:
                return False
        return True

    def board_state(self) -> dict:
        """Full state snapshot sent to clients."""
        puzzle_display = []
        if self.puzzle:
            for i, ch in enumerate(self.puzzle.text):
                if ch == " ":
                    puzzle_display.append({"char": " ", "revealed": True, "index": i})
                elif not ch.isalpha():
                    # Punctuation (hyphens, apostrophes, & etc.) always shown
                    puzzle_display.append({"char": ch, "revealed": True, "index": i})
                elif i in self.revealed:
                    puzzle_display.append({"char": ch, "revealed": True, "index": i})
                else:
                    puzzle_display.append({"char": ch, "revealed": False, "index": i})

        current_player = next(
            (p.to_dict() for p in self.players if p.slot == self.current_slot),
            None
        )

        return {
            "phase": self.phase,
            "round_num": self.round_num,
            "total_rounds": TOTAL_ROUNDS,
            "players": [p.to_dict() for p in self.players],
            "current_slot": self.current_slot,
            "current_player": current_player,
            "puzzle_display": puzzle_display,
            "category": self.puzzle.category if self.puzzle else "",
            "air_date": self.puzzle.date if self.puzzle else "",
            "used_letters": sorted(self.used_letters),
            "last_spin": self.last_spin,
            "vowel_cost": VOWEL_COST,
            "wheel_wedges": WHEEL_WEDGES,
        }


# ── Puzzle loader ────────────────────────────────────────────────────────────

def _load_puzzles(path: str) -> list[Puzzle]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Puzzle file '{path}' not found.\n"
            "Please run:  python scraper.py\n"
            "to build the puzzle database first."
        )
    puzzles = []
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = row.get("puzzle", "").strip().upper()
            category = row.get("category", "").strip()
            date = row.get("date", "").strip()
            if text and category:
                puzzles.append(Puzzle(text=text, category=category, date=date))

    if not puzzles:
        raise ValueError("puzzles.csv exists but contains no valid puzzles.")

    random.shuffle(puzzles)
    return puzzles
