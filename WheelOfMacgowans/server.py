"""
Wheel of Macgowans — Game Server
Run this to start the game:
    python server.py

TV board:  http://localhost:5000/board
Phones:    http://<your-local-ip>:5000/controller
"""

import os
import socket
import threading
import webbrowser
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

from game import Game, Phase, MAX_PLAYERS

app = Flask(__name__)
app.config["SECRET_KEY"] = "wheel-of-macgowans-secret"
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

game = Game()

BOARD_ROOM = "board"
CTRL_ROOM  = "controllers"


# ── HTTP routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("controller.html")

@app.route("/board")
def board():
    local_ip = get_local_ip()
    controller_url = f"http://{local_ip}:5000/controller"
    return render_template("board.html", controller_url=controller_url)

@app.route("/controller")
def controller():
    return render_template("controller.html")

@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Emergency stop — called by the kill button on the board page."""
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return "Stopping..."


# ── Socket events — connection ───────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    # Send current state to whoever just connected
    emit("state_update", game.board_state())

@socketio.on("disconnect")
def on_disconnect():
    game.remove_player(request.sid)
    _broadcast_state()

@socketio.on("join_board")
def on_join_board():
    join_room(BOARD_ROOM)
    emit("state_update", game.board_state())

@socketio.on("join_controller")
def on_join_controller():
    join_room(CTRL_ROOM)
    emit("state_update", game.board_state())


# ── Socket events — lobby ────────────────────────────────────────────────────

@socketio.on("join_game")
def on_join_game(data):
    name = data.get("name", "").strip()
    if not name:
        emit("error", {"reason": "Please enter a name."})
        return

    # Always try reconnect first (covers mid-game phone screen timeouts)
    reconnected = game.reconnect_player(name, request.sid)
    if reconnected:
        emit("joined", {"slot": reconnected.slot, "name": reconnected.name})
        _broadcast_state()
        return

    if game.phase != Phase.LOBBY:
        emit("error", {"reason": "Game already in progress."})
        return
    if len(game.players) >= 3:
        emit("error", {"reason": "Game is full (3 players max)."})
        return

    player = game.add_player(name, request.sid)
    if player is None:
        emit("error", {"reason": "Could not join — game is full."})
        return

    emit("joined", {"slot": player.slot, "name": player.name})
    _broadcast_state()

    # Auto-start as soon as 3 players have joined
    if len(game.players) == MAX_PLAYERS:
        game.start_game()
        _broadcast_state()
        socketio.emit("game_started", {})

@socketio.on("start_game")
def on_start_game():
    if not game.can_start():
        emit("error", {"reason": f"Need {3} players to start."})
        return
    game.start_game()
    _broadcast_state()
    socketio.emit("game_started", {})


# ── Socket events — gameplay ─────────────────────────────────────────────────

@socketio.on("spin")
def on_spin():
    result = game.spin(request.sid)
    if not result["ok"]:
        emit("error", {"reason": result.get("reason", "Cannot spin now.")})
        return

    # Tell the board to animate the wheel to this wedge
    socketio.emit("wheel_spin", {
        "wedge": result["wedge"],
        "wedge_index": result["wedge_index"],
        "event": result["event"],
    }, to=BOARD_ROOM)

    # Tell controllers to show "spinning..." state
    socketio.emit("spinning", {}, to=CTRL_ROOM)

    # Fallback: if the board never emits spin_done (tab throttled, etc.)
    # the server fires it automatically after 6 s so controllers always unblock.
    def _fallback_spin_done():
        socketio.emit("spin_done", {})
    threading.Timer(6.0, _fallback_spin_done).start()

    # After animation delay (handled client-side), board emits spin_complete
    # We just broadcast full state now — board will use it after animation
    _broadcast_state()

@socketio.on("pick_letter")
def on_pick_letter(data):
    letter = data.get("letter", "").upper()
    result = game.pick_letter(request.sid, letter)
    if not result["ok"]:
        emit("error", {"reason": result.get("reason", "Invalid letter.")})
        return

    _emit_letter_result(result)
    _broadcast_state()

@socketio.on("buy_vowel")
def on_buy_vowel():
    result = game.buy_vowel(request.sid)
    if not result["ok"]:
        emit("error", {"reason": result.get("reason", "Cannot buy vowel.")})
        return
    _broadcast_state()

@socketio.on("pick_vowel")
def on_pick_vowel(data):
    letter = data.get("letter", "").upper()
    result = game.pick_vowel(request.sid, letter)
    if not result["ok"]:
        emit("error", {"reason": result.get("reason", "Invalid vowel.")})
        return

    _emit_letter_result(result)
    _broadcast_state()

@socketio.on("solve_attempt")
def on_solve_attempt(data):
    answer = data.get("answer", "")
    result = game.solve_attempt(request.sid, answer)
    if not result["ok"]:
        emit("error", {"reason": result.get("reason", "Cannot solve now.")})
        return

    if result["correct"]:
        socketio.emit("puzzle_solved", {
            "solver": result["solver"],
        })
    else:
        socketio.emit("wrong_solve", {
            "player": game.player_by_sid(request.sid).to_dict()
            if game.player_by_sid(request.sid) else {}
        })

    _broadcast_state()

@socketio.on("next_round")
def on_next_round():
    result = game.next_round()
    if not result["ok"]:
        emit("error", {"reason": result.get("reason", "Cannot advance round.")})
        return
    _broadcast_state()
    if game.phase == Phase.GAME_OVER:
        socketio.emit("game_over", {"players": [p.to_dict() for p in game.players]})

@socketio.on("spin_done")
def on_spin_done():
    """Board signals that the wheel animation has finished — relay to controllers."""
    socketio.emit("spin_done", {})

@socketio.on("reset_game")
def on_reset_game():
    """Full reset — back to lobby."""
    global game
    game = Game()
    _broadcast_state()
    socketio.emit("game_reset", {})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _broadcast_state():
    socketio.emit("state_update", game.board_state())

def _emit_letter_result(result: dict):
    event = result.get("event", "")
    if event in ("letter_correct", "vowel_revealed"):
        socketio.emit("letter_correct", {
            "letter": result["letter"],
            "count": result["count"],
            "earnings": result.get("earnings", 0),
        }, to=BOARD_ROOM)
    elif event == "letter_wrong":
        socketio.emit("letter_wrong", {
            "letter": result["letter"],
        }, to=BOARD_ROOM)
    if event == "puzzle_solved":
        socketio.emit("puzzle_solved", {}, to=BOARD_ROOM)


# ── Entry point ──────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    local_ip = get_local_ip()
    print("=" * 55)
    print("  Wheel of Macgowans is running!")
    print("=" * 55)
    print(f"\n  TV Board  ->  http://localhost:5000/board")
    print(f"  Phones    ->  http://{local_ip}:5000/controller")
    print(f"\n  (All devices must be on the same Wi-Fi)")
    print(f"\n  Press Ctrl+C to stop the server.\n")
    # Open the board in the default browser after a short delay
    # (delay ensures the server is ready before the browser loads)
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000/board")).start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
