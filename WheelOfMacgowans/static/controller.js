// Wheel of Macgowans — Phone Controller

const socket = io();

let mySlot = null;
let myName = '';
let state  = null;
let joined = false;
let spinBlocked = false; // true while wheel animation plays on the board

const VOWELS     = new Set(['A','E','I','O','U']);
const CONSONANTS = 'BCDFGHJKLMNPQRSTVWXYZ'.split('');
const ALL_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');

// ── Screen helpers ────────────────────────────────────────────────────────────

function showScreen(id) {
  ['join-screen','waiting-screen','controller-screen'].forEach(s => {
    document.getElementById(s).style.display = 'none';
  });
  document.getElementById(id).style.display = 'flex';
}

// ── Build letter grid ─────────────────────────────────────────────────────────

function buildLetterGrid() {
  const grid = document.getElementById('letter-grid');
  grid.innerHTML = '';
  ALL_LETTERS.forEach(letter => {
    const btn = document.createElement('button');
    btn.className = 'letter-btn' + (VOWELS.has(letter) ? ' vowel' : '');
    btn.id = `lb-${letter}`;
    btn.textContent = letter;
    btn.dataset.letter = letter;
    btn.addEventListener('click', () => onLetterClick(letter));
    grid.appendChild(btn);
  });
}

buildLetterGrid();

// ── Apply game state ──────────────────────────────────────────────────────────

function applyState(s) {
  state = s;

  if (!joined) return;
  if (spinBlocked) return; // hold display until wheel animation finishes

  const phase = s.phase;
  const isMyTurn = (mySlot === s.current_slot);
  const usedSet = new Set(s.used_letters);

  // Switch screens
  if (phase === 'lobby') {
    showScreen('waiting-screen');
    return;
  }
  if (phase === 'game_over') {
    showScreen('waiting-screen');
    document.getElementById('waiting-name').textContent = 'Game Over!';
    document.getElementById('waiting-msg').textContent = 'Thanks for playing!';
    document.getElementById('next-round-btn').style.display = 'none';
    return;
  }

  if (phase === 'round_end') {
    showScreen('waiting-screen');
    document.getElementById('waiting-name').textContent = 'Round Complete!';
    document.getElementById('waiting-msg').textContent = isMyTurn ? 'You solved it!' : 'Next round starting soon...';
    document.getElementById('next-round-btn').style.display = isMyTurn ? 'block' : 'none';
    return;
  }

  showScreen('controller-screen');

  // Status bar
  document.getElementById('ctrl-player-name').textContent = myName;
  document.getElementById('ctrl-round').textContent =
    `Round ${s.round_num} of ${s.total_rounds}`;

  // Turn banner
  const banner = document.getElementById('turn-banner');
  if (isMyTurn) {
    banner.className = 'turn-banner your-turn';
    banner.textContent = "🎯 It's your turn!";
  } else {
    const currentPlayer = s.players.find(p => p.slot === s.current_slot);
    banner.className = 'turn-banner not-your-turn';
    banner.textContent = currentPlayer
      ? `Waiting for ${currentPlayer.name}...`
      : 'Waiting...';
  }

  // Spin result display
  const spinEl = document.getElementById('spin-result');
  if (s.last_spin === 'BANKRUPT') {
    spinEl.textContent = '💥 BANKRUPT!';
    spinEl.className = 'bankrupt';
  } else if (s.last_spin === 'LOSE A TURN') {
    spinEl.textContent = '⛔ Lose a Turn';
    spinEl.className = 'lose-turn';
  } else if (s.last_spin) {
    spinEl.textContent = `💰 $${s.last_spin.toLocaleString()}`;
    spinEl.className = '';
  } else {
    spinEl.textContent = '';
    spinEl.className = '';
  }

  // ── Button states ──

  // SPIN: only on your turn, in spinning phase
  const canSpin = isMyTurn && phase === 'spinning';
  setBtn('spin-btn', canSpin);

  // BUY VOWEL: your turn, spinning or letter_pick phase, have enough money
  const myPlayer = s.players.find(p => p.slot === mySlot);
  const myMoney = myPlayer ? myPlayer.round_money : 0;
  const unusedVowels = ['A','E','I','O','U'].filter(v => !usedSet.has(v));
  const canBuyVowel = isMyTurn
    && (phase === 'spinning' || phase === 'letter_pick')
    && myMoney >= 250
    && unusedVowels.length > 0;
  setBtn('buy-vowel-btn', canBuyVowel);

  // SOLVE: your turn, most phases except buy_vowel
  const canSolve = isMyTurn
    && (phase === 'spinning' || phase === 'letter_pick' || phase === 'buy_vowel');
  setBtn('solve-btn', canSolve);

  // ── Letter grid ──
  ALL_LETTERS.forEach(letter => {
    const btn = document.getElementById(`lb-${letter}`);
    if (!btn) return;
    const used = usedSet.has(letter);

    if (used) {
      btn.disabled = true;
      btn.classList.remove('selectable','vowel-selectable');
      return;
    }

    // In buy_vowel phase: only vowels selectable (for this player)
    if (phase === 'buy_vowel' && isMyTurn) {
      if (VOWELS.has(letter)) {
        btn.disabled = false;
        btn.classList.add('vowel-selectable');
        btn.classList.remove('selectable');
      } else {
        btn.disabled = true;
        btn.classList.remove('selectable','vowel-selectable');
      }
      return;
    }

    // In letter_pick phase: only consonants selectable for this player
    if (phase === 'letter_pick' && isMyTurn) {
      if (!VOWELS.has(letter)) {
        btn.disabled = false;
        btn.classList.add('selectable');
        btn.classList.remove('vowel-selectable');
      } else {
        btn.disabled = true;
        btn.classList.remove('selectable','vowel-selectable');
      }
      return;
    }

    // Otherwise not selectable
    btn.disabled = true;
    btn.classList.remove('selectable','vowel-selectable');
  });

  // ── Mini scoreboard ──
  const scoresEl = document.getElementById('mini-scores');
  scoresEl.innerHTML = '';
  s.players.forEach(p => {
    const div = document.createElement('div');
    div.className = 'mini-score' + (p.slot === s.current_slot ? ' active' : '');
    div.innerHTML = `
      <div class="mini-score-name">${escHtml(p.name)}</div>
      <div class="mini-score-val">$${p.banked.toLocaleString()}</div>
      <div style="color:#aaffaa;font-size:0.75rem;">+$${p.round_money.toLocaleString()}</div>
    `;
    scoresEl.appendChild(div);
  });

  // Reset panels if not in the right phase
  if (phase !== 'buy_vowel') hidePanels();
}

function setBtn(id, enabled) {
  const btn = document.getElementById(id);
  if (btn) btn.disabled = !enabled;
}

function hidePanels() {
  document.getElementById('vowel-panel').classList.remove('visible');
  document.getElementById('solve-panel').classList.remove('visible');
}

// ── Letter click ──────────────────────────────────────────────────────────────

function onLetterClick(letter) {
  if (!state) return;
  const phase = state.phase;

  if (phase === 'buy_vowel' && VOWELS.has(letter)) {
    socket.emit('pick_vowel', { letter });
    hidePanels();
    return;
  }

  if (phase === 'letter_pick' && !VOWELS.has(letter)) {
    socket.emit('pick_letter', { letter });
    return;
  }
}

// ── Spin ──────────────────────────────────────────────────────────────────────

document.getElementById('spin-btn').addEventListener('click', () => {
  socket.emit('spin');
  setBtn('spin-btn', false); // prevent double-tap
});

// ── Buy vowel ─────────────────────────────────────────────────────────────────

document.getElementById('buy-vowel-btn').addEventListener('click', () => {
  socket.emit('buy_vowel');
  // Show vowel panel — applyState will enable only unused vowels
  document.getElementById('vowel-panel').classList.add('visible');
  document.getElementById('solve-panel').classList.remove('visible');
});

// Vowel panel buttons
document.querySelectorAll('.vowel-pick-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const letter = btn.dataset.letter;
    socket.emit('pick_vowel', { letter });
    hidePanels();
  });
});

// ── Solve ─────────────────────────────────────────────────────────────────────

document.getElementById('solve-btn').addEventListener('click', () => {
  document.getElementById('solve-panel').classList.add('visible');
  document.getElementById('vowel-panel').classList.remove('visible');
  document.getElementById('solve-input').value = '';
  document.getElementById('solve-input').focus();
});

document.getElementById('solve-cancel-btn').addEventListener('click', () => {
  hidePanels();
});

document.getElementById('solve-submit-btn').addEventListener('click', () => {
  const answer = document.getElementById('solve-input').value.trim();
  if (!answer) return;
  socket.emit('solve_attempt', { answer });
  hidePanels();
});

document.getElementById('solve-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('solve-submit-btn').click();
});

// ── Join ──────────────────────────────────────────────────────────────────────

document.getElementById('next-round-btn').addEventListener('click', () => {
  document.getElementById('next-round-btn').style.display = 'none';
  socket.emit('next_round');
});

document.getElementById('join-btn').addEventListener('click', doJoin);
document.getElementById('name-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') doJoin();
});

function doJoin() {
  const name = document.getElementById('name-input').value.trim();
  if (!name) {
    document.getElementById('join-error').textContent = 'Please enter a name.';
    return;
  }
  document.getElementById('join-error').textContent = '';
  socket.emit('join_game', { name });
}

// ── Socket events ─────────────────────────────────────────────────────────────

socket.on('connect', () => {
  socket.emit('join_controller');
  // Auto-rejoin if we were previously in a game (handles phone screen timeout)
  const storedName = sessionStorage.getItem('wom_name');
  if (storedName && !joined) {
    socket.emit('join_game', { name: storedName });
  }
});

socket.on('joined', (data) => {
  joined = true;
  mySlot = data.slot;
  myName = data.name;
  // Persist name so we can auto-rejoin if the phone screen times out
  sessionStorage.setItem('wom_name', data.name);
  document.getElementById('waiting-name').textContent = `Welcome, ${myName}!`;
  document.getElementById('waiting-msg').textContent = '';
  document.getElementById('waiting-msg').innerHTML =
    'Waiting for other players<span class="dot-anim"></span>';
  showScreen('waiting-screen');
  if (state) applyState(state);
});

socket.on('state_update', (s) => {
  applyState(s);
});

let _spinBlockedTimer = null;

socket.on('spinning', () => {
  // Block display updates and disable buttons while wheel animates
  spinBlocked = true;
  setBtn('spin-btn', false);
  setBtn('buy-vowel-btn', false);
  setBtn('solve-btn', false);
  // Safety: auto-unblock after 8 s in case spin_done is lost
  clearTimeout(_spinBlockedTimer);
  _spinBlockedTimer = setTimeout(() => {
    if (spinBlocked) {
      spinBlocked = false;
      if (state) applyState(state);
    }
  }, 8000);
});

socket.on('spin_done', () => {
  // Wheel has settled — now show the spin result
  clearTimeout(_spinBlockedTimer);
  spinBlocked = false;
  if (state) applyState(state);
});

socket.on('error', (data) => {
  if (!joined) {
    document.getElementById('join-error').textContent = data.reason || 'Error';
  } else {
    // Flash a brief message in turn banner
    const banner = document.getElementById('turn-banner');
    const prev = banner.textContent;
    banner.textContent = '⚠️ ' + (data.reason || 'Error');
    setTimeout(() => { if (state) applyState(state); }, 2000);
  }
});

socket.on('wrong_solve', () => {
  const banner = document.getElementById('turn-banner');
  banner.textContent = '❌ Wrong answer — turn passed';
  setTimeout(() => { if (state) applyState(state); }, 2000);
});

socket.on('puzzle_solved', () => {
  showScreen('waiting-screen');
  document.getElementById('waiting-name').textContent = '🎉 Puzzle Solved!';
  document.getElementById('waiting-msg').textContent = 'Next round coming up...';
});

socket.on('game_reset', () => {
  joined = false;
  mySlot = null;
  myName = '';
  state = null;
  sessionStorage.removeItem('wom_name');
  showScreen('join-screen');
  document.getElementById('name-input').value = '';
});

// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
