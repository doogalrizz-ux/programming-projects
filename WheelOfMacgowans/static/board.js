// Wheel of Macgowans — Board (TV Display)

const socket = io();
let state = null;
let wheelSpinning = false;  // blocks applyState display updates during animation

// ── Sound Engine ─────────────────────────────────────────────────────────────

const AudioCtx = window.AudioContext || window.webkitAudioContext;
const ctx = new AudioCtx();

// Unlock AudioContext on first user interaction (browser requirement)
document.addEventListener('click', () => { if (ctx.state === 'suspended') ctx.resume(); }, { once: true });

function playTone(freq, duration, type = 'sine', gain = 0.4) {
  const play = () => {
    const osc = ctx.createOscillator();
    const gainNode = ctx.createGain();
    osc.connect(gainNode);
    gainNode.connect(ctx.destination);
    osc.type = type;
    osc.frequency.setValueAtTime(freq, ctx.currentTime);
    gainNode.gain.setValueAtTime(gain, ctx.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + duration);
  };
  if (ctx.state === 'suspended') {
    ctx.resume().then(play);
  } else {
    play();
  }
}

function soundCorrectLetter() {
  playTone(880, 0.12, 'sine', 0.3);
  setTimeout(() => playTone(1320, 0.18, 'sine', 0.3), 80);
}

function soundWrongLetter() {
  playTone(180, 0.4, 'sawtooth', 0.35);
}

function soundBankrupt() {
  playTone(220, 0.2, 'sawtooth', 0.5);
  setTimeout(() => playTone(110, 0.6, 'sawtooth', 0.5), 150);
}

function soundLoseTurn() {
  playTone(330, 0.15, 'square', 0.3);
  setTimeout(() => playTone(220, 0.3, 'square', 0.3), 120);
}

function soundSolve() {
  const notes = [523, 659, 784, 1047];
  notes.forEach((f, i) => setTimeout(() => playTone(f, 0.25, 'sine', 0.4), i * 120));
}

function soundWheelTick() {
  playTone(800, 0.03, 'square', 0.2);
}

// ── Wheel Drawing ─────────────────────────────────────────────────────────────

const WEDGE_COLORS = [
  '#e74c3c','#e67e22','#f1c40f','#2ecc71','#1abc9c',
  '#3498db','#9b59b6','#e91e63','#00bcd4','#8bc34a',
  '#ff5722','#607d8b','#795548','#ffc107','#03a9f4',
  '#673ab7','#4caf50','#f44336','#ff9800','#9c27b0',
  '#2196f3','#009688','#cddc39','#ff4444','#ff6600',
];
const BANKRUPT_COLOR   = '#1a0000';
const LOSE_TURN_COLOR  = '#001a00';

let wheelAngle = 0;
let spinning = false;
let spinStartTime = null;
let spinStartAngle = 0;
let spinEndAngle = 0;
const SPIN_DURATION = 4500; // ms
let spinCallback = null;
let lastTickIndex = -1;

function getWedgeColor(wedge, i) {
  if (wedge === 'BANKRUPT') return BANKRUPT_COLOR;
  if (wedge === 'LOSE A TURN') return LOSE_TURN_COLOR;
  return WEDGE_COLORS[i % WEDGE_COLORS.length];
}

function getWedgeLabel(wedge) {
  if (wedge === 'BANKRUPT') return 'BANKRUPT';
  if (wedge === 'LOSE A TURN') return 'LOSE A TURN';
  return '$' + wedge.toLocaleString();
}

function drawWheel(wedges, angle) {
  const canvas = document.getElementById('wheel-canvas');
  const c = canvas.getContext('2d');
  const cx = canvas.width / 2;
  const cy = canvas.height / 2;
  const r = cx - 10;
  const n = wedges.length;
  const arc = (2 * Math.PI) / n;

  c.clearRect(0, 0, canvas.width, canvas.height);

  for (let i = 0; i < n; i++) {
    const startAngle = angle + i * arc - Math.PI / 2;
    const endAngle   = startAngle + arc;

    // Wedge fill
    c.beginPath();
    c.moveTo(cx, cy);
    c.arc(cx, cy, r, startAngle, endAngle);
    c.closePath();
    c.fillStyle = getWedgeColor(wedges[i], i);
    c.fill();

    // Border
    c.strokeStyle = '#000';
    c.lineWidth = 2;
    c.stroke();

    // Label
    c.save();
    c.translate(cx, cy);
    c.rotate(startAngle + arc / 2);
    c.textAlign = 'right';
    const label = getWedgeLabel(wedges[i]);
    const fontSize = label.length > 9 ? 11 : 14;
    c.font = `bold ${fontSize}px Georgia`;
    c.fillStyle = '#fff';
    c.shadowColor = '#000';
    c.shadowBlur = 4;
    c.fillText(label, r - 8, 5);
    c.restore();
  }

  // Center hub
  c.beginPath();
  c.arc(cx, cy, 28, 0, 2 * Math.PI);
  c.fillStyle = '#FFD700';
  c.fill();
  c.strokeStyle = '#8B6500';
  c.lineWidth = 3;
  c.stroke();

  // Outer ring
  c.beginPath();
  c.arc(cx, cy, r, 0, 2 * Math.PI);
  c.strokeStyle = '#FFD700';
  c.lineWidth = 6;
  c.stroke();
}

function easeOutQuart(t) {
  return 1 - Math.pow(1 - t, 4);
}

function animateSpin(wedges, timestamp) {
  if (!spinning) return;

  if (spinStartTime === null) spinStartTime = timestamp;
  const elapsed = timestamp - spinStartTime;
  const t = Math.min(elapsed / SPIN_DURATION, 1);
  const eased = easeOutQuart(t);

  wheelAngle = spinStartAngle + (spinEndAngle - spinStartAngle) * eased;
  drawWheel(wedges, wheelAngle);

  // Tick sound at each wedge boundary
  const arc = (2 * Math.PI) / wedges.length;
  const norm = ((wheelAngle % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
  const tickIndex = Math.floor(norm / arc);
  if (tickIndex !== lastTickIndex) {
    soundWheelTick();
    lastTickIndex = tickIndex;
  }

  if (t >= 1) {
    wheelAngle = spinEndAngle;
    drawWheel(wedges, wheelAngle);
    spinning = false;
    if (spinCallback) spinCallback();
    return;
  }

  requestAnimationFrame((ts) => animateSpin(wedges, ts));
}

function spinWheelTo(wedges, wedgeIdx, onComplete) {
  const n = wedges.length;
  const arc = (2 * Math.PI) / n;

  // Target: center of wedge[wedgeIdx] aligns with the top pointer
  // In drawWheel: center of wedge i = wheelAngle + i*arc + arc/2 - PI/2
  // We want that canvas angle to equal -PI/2 (top), so:
  //   wheelAngle + wedgeIdx*arc + arc/2 - PI/2 = -PI/2 + 2k*PI
  //   wheelAngle = -wedgeIdx*arc - arc/2 + 2k*PI
  const wedgeCenterOffset = wedgeIdx * arc + arc / 2;
  const desiredFinal = ((-(wedgeCenterOffset)) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI);
  const currentNorm  = ((wheelAngle % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);

  let delta = desiredFinal - currentNorm;
  if (delta <= 0) delta += 2 * Math.PI; // always spin forward

  const extraSpins = (Math.floor(Math.random() * 4) + 5) * 2 * Math.PI; // 5–8 full extra rotations

  spinStartAngle = wheelAngle;
  spinEndAngle   = wheelAngle + extraSpins + delta;
  spinStartTime  = null;
  spinning       = true;
  lastTickIndex  = -1;
  spinCallback   = onComplete;

  requestAnimationFrame((ts) => animateSpin(wedges, ts));
}


// ── Puzzle Board ──────────────────────────────────────────────────────────────

const BOARD_COLS = 14;
const BOARD_ROWS = 4;

function buildPuzzleRows(puzzleDisplay) {
  // Split into words, wrap at BOARD_COLS
  const words = [];
  let current = [];
  for (const tile of puzzleDisplay) {
    if (tile.char === ' ') {
      if (current.length) { words.push(current); current = []; }
      words.push([tile]);
    } else {
      current.push(tile);
    }
  }
  if (current.length) words.push(current);

  const rows = [[]];
  for (const word of words) {
    const row = rows[rows.length - 1];
    if (row.length + word.length > BOARD_COLS && row.length > 0) {
      rows.push([...word]);
    } else {
      rows[rows.length - 1].push(...word);
    }
  }
  return rows;
}

function renderPuzzle(puzzleDisplay) {
  const area = document.getElementById('puzzle-area');
  area.innerHTML = '';

  // Build word-wrapped rows, then map onto fixed BOARD_ROWS × BOARD_COLS grid
  const puzzleRows = puzzleDisplay && puzzleDisplay.length
    ? buildPuzzleRows(puzzleDisplay)
    : [];

  for (let r = 0; r < BOARD_ROWS; r++) {
    const rowTiles = puzzleRows[r] || [];
    const padLeft = Math.floor((BOARD_COLS - rowTiles.length) / 2);

    const rowEl = document.createElement('div');
    rowEl.className = 'puzzle-row';

    for (let c = 0; c < BOARD_COLS; c++) {
      const tileIdx = c - padLeft;
      const div = document.createElement('div');

      if (tileIdx >= 0 && tileIdx < rowTiles.length) {
        const tile = rowTiles[tileIdx];
        if (tile.char === ' ') {
          // Word space → green square
          div.className = 'letter-tile green';
        } else if (tile.revealed) {
          div.className = 'letter-tile revealed';
          div.textContent = tile.char;
        } else {
          // Letter exists but not yet revealed → blank cream tile
          div.className = 'letter-tile blank';
        }
      } else {
        // Filler position → green square
        div.className = 'letter-tile green';
      }

      rowEl.appendChild(div);
    }
    area.appendChild(rowEl);
  }
}

function renderUsedLetters(usedLetters) {
  const el = document.getElementById('used-letters');
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
  el.innerHTML = alphabet.map(l => {
    const used = usedLetters.includes(l);
    return `<span class="${used ? 'used' : ''}">${l}</span>`;
  }).join(' ');
}

function renderScoreboard(players, currentSlot) {
  const sb = document.getElementById('scoreboard');
  sb.innerHTML = '';
  players.forEach(p => {
    const card = document.createElement('div');
    card.className = 'score-card' + (p.slot === currentSlot ? ' active' : '');
    card.innerHTML = `
      <div class="score-name">${escHtml(p.name)}</div>
      <div class="score-label">This Round</div>
      <div class="score-round">$${p.round_money.toLocaleString()}</div>
      <div class="score-label">Banked Total</div>
      <div class="score-banked">$${p.banked.toLocaleString()}</div>
    `;
    sb.appendChild(card);
  });
}

function renderSpinValue(lastSpin) {
  const el = document.getElementById('spin-value');
  if (!lastSpin) {
    el.textContent = 'Spin the wheel!';
    el.className = '';
    return;
  }
  if (lastSpin === 'BANKRUPT') {
    el.textContent = '💥 BANKRUPT!';
    el.className = 'bankrupt';
  } else if (lastSpin === 'LOSE A TURN') {
    el.textContent = '⛔ LOSE A TURN';
    el.className = 'lose-turn';
  } else {
    el.textContent = `💰 $${lastSpin.toLocaleString()} — Pick a consonant!`;
    el.className = 'dollar';
  }
}

// ── Screen management ─────────────────────────────────────────────────────────

function showScreen(id) {
  ['lobby-screen','wheel-screen','round-end-screen','gameover-screen'].forEach(s => {
    document.getElementById(s).classList.remove('visible');
  });
  if (id) document.getElementById(id).classList.add('visible');
}

function announce(msg, duration = 2500) {
  const el = document.getElementById('announcement');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(window._announceTimer);
  window._announceTimer = setTimeout(() => { el.style.display = 'none'; }, duration);
}

// ── Round end ─────────────────────────────────────────────────────────────────

function showRoundEnd(state) {
  document.getElementById('round-end-puzzle').textContent =
    state.puzzle_display.map(t => t.char).join('');
  const solver = state.players.find(p => p.slot === state.current_slot);
  if (solver) {
    document.getElementById('round-end-solver').textContent =
      `${solver.name} solved it and banked $${(solver.banked).toLocaleString()}!`;
  }

  const scoresEl = document.getElementById('round-end-scores');
  scoresEl.innerHTML = '';
  state.players.forEach(p => {
    const card = document.createElement('div');
    card.className = 'score-card';
    card.innerHTML = `
      <div class="score-name">${escHtml(p.name)}</div>
      <div class="score-label">Total Banked</div>
      <div class="score-banked">$${p.banked.toLocaleString()}</div>
    `;
    scoresEl.appendChild(card);
  });

  showScreen('round-end-screen');
}

function showGameOver(players) {
  const sorted = [...players].sort((a, b) => b.banked - a.banked);
  const winner = sorted[0];

  document.getElementById('gameover-winner').textContent =
    `🏆 ${winner.name} wins with $${winner.banked.toLocaleString()}!`;

  const scoresEl = document.getElementById('gameover-scores');
  scoresEl.innerHTML = '';
  sorted.forEach((p, i) => {
    const card = document.createElement('div');
    card.className = 'score-card';
    card.innerHTML = `
      <div style="font-size:1.5rem;color:#FFD700">${['🥇','🥈','🥉'][i] || ''}</div>
      <div class="score-name">${escHtml(p.name)}</div>
      <div class="score-banked">$${p.banked.toLocaleString()}</div>
    `;
    scoresEl.appendChild(card);
  });

  showScreen('gameover-screen');
  soundSolve();
}

function resetGame() {
  socket.emit('reset_game');
}

// ── Apply state ───────────────────────────────────────────────────────────────

let pendingWheelSpin = null; // { wedge, event } — queued while wheel animates

function applyState(s) {
  state = s;
  if (wheelSpinning) return;  // keep latest state but don't touch the display

  // Update lobby slots
  if (s.phase === 'lobby') {
    showScreen('lobby-screen');
    for (let i = 0; i < 3; i++) {
      const slot = document.getElementById(`lobby-slot-${i}`);
      const player = s.players.find(p => p.slot === i);
      if (player) {
        slot.className = 'lobby-slot filled';
        slot.textContent = player.name;
      } else {
        slot.className = 'lobby-slot';
        slot.textContent = 'Waiting...';
      }
    }
    return;
  }

  if (s.phase === 'round_end') {
    showRoundEnd(s);
    return;
  }

  if (s.phase === 'game_over') {
    showGameOver(s.players);
    return;
  }

  // Normal game phases
  showScreen(null); // hide overlays

  document.getElementById('round-label').textContent =
    `Round ${s.round_num} of ${s.total_rounds}`;
  document.getElementById('category-label').textContent =
    s.category.toUpperCase();
  document.getElementById('air-date-label').textContent =
    s.air_date ? `Originally aired: ${s.air_date}` : '';

  renderPuzzle(s.puzzle_display);
  renderUsedLetters(s.used_letters);
  renderScoreboard(s.players, s.current_slot);
  renderSpinValue(s.last_spin);
}

// ── Socket events ─────────────────────────────────────────────────────────────

socket.on('connect', () => {
  socket.emit('join_board');
  // Try to get local IP shown in lobby
  fetch('/controller').then(() => {}).catch(() => {});
});

socket.on('state_update', (s) => {
  applyState(s);
});

socket.on('wheel_spin', (data) => {
  if (!state) return;
  wheelSpinning = true;
  const wedges = state.wheel_wedges;
  const playerName = state.current_player ? state.current_player.name : '';

  // Show wheel screen
  document.getElementById('wheel-spinner-name').textContent =
    `${playerName} is spinning...`;
  showScreen('wheel-screen');
  drawWheel(wedges, wheelAngle);

  // Animate using the exact wedge index (avoids duplicate-value indexOf problem)
  spinWheelTo(wedges, data.wedge_index, () => {
    // Notify controllers the wheel has settled
    socket.emit('spin_done');
    // Brief pause showing result on wheel before returning to board
    setTimeout(() => {
      if (data.event === 'bankrupt') {
        soundBankrupt();
      } else if (data.event === 'lose_a_turn') {
        soundLoseTurn();
      }
      wheelSpinning = false;
      showScreen(null); // back to board
      applyState(state); // now apply the latest state
    }, 1200);
  });
});

socket.on('letter_correct', (data) => {
  soundCorrectLetter();
  announce(`✅ ${data.letter} — ${data.count}x — +$${data.earnings.toLocaleString()}`, 2000);
});

socket.on('letter_wrong', (data) => {
  soundWrongLetter();
  announce(`❌ No ${data.letter}`, 1800);
});

socket.on('puzzle_solved', () => {
  soundSolve();
});

socket.on('wrong_solve', (data) => {
  soundWrongLetter();
  const name = data.player ? data.player.name : 'Player';
  announce(`❌ ${name}'s solve was wrong!`, 2000);
});

socket.on('game_over', (data) => {
  showGameOver(data.players);
});

socket.on('game_reset', () => {
  showScreen('lobby-screen');
});


// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
