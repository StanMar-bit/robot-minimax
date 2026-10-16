<script>
  // TA control panel. Every automatic step has a manual override here.
  import Countdown from '../lib/Countdown.svelte';
  import Grid from '../lib/Grid.svelte';
  import { banner, fmtValue, setupLabel } from '../lib/labels.js';
  import { connect, load, save, serverNow } from '../lib/live.svelte.js';

  // The token can be passed once as /admin#token=..., then it is kept in localStorage.
  const fromHash = location.hash.match(/token=([^&]+)/);
  if (fromHash) {
    save('minimax.admin', decodeURIComponent(fromHash[1]));
    history.replaceState(null, '', location.pathname);
  }
  let token = $state(load('minimax.admin') || '');
  let tokenInput = $state('');
  const conn = token ? connect({ role: 'admin', token }) : null;
  const live = conn?.live;

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = live ? serverNow(live) : Date.now()), 250);
    return () => clearInterval(id);
  });

  const st = $derived(live?.state);
  const status = $derived(live?.status);
  const bridge = $derived(status?.bridge);
  const colors = $derived(st?.game?.colors ?? { MAX: '#2563eb', MIN: '#ea580c' });
  const round = $derived(st?.round);
  const settings = $derived(st?.settings);

  let log = $state([]);
  async function cmd(name, args = {}) {
    const res = await conn.call(`admin:${name}`, args);
    const time = new Date().toLocaleTimeString('en-GB', { hour12: false });
    log = [{ time, name, ok: res.ok, msg: res.ok ? '' : res.error }, ...log].slice(0, 8);
    return res;
  }
  const set = (key, value) => cmd('settings', { [key]: value });
  const robot = (c, args = {}) => cmd('robot', { cmd: c, ...args });

  function saveToken(e) {
    e.preventDefault();
    save('minimax.admin', tokenInput.trim());
    location.reload();
  }
  function forgetToken() {
    save('minimax.admin', null);
    location.reload();
  }

  // New game form
  let variant = $state('1');
  let side = $state('MAX');
  let customCups = $state(['I']);
  let customStart = $state('A');
  let editing = $state('cups');
  function pickCustom(cell) {
    if (editing === 'start') {
      customStart = cell;
      customCups = customCups.filter((c) => c !== cell);
    } else if (cell !== customStart) {
      customCups = customCups.includes(cell) ? customCups.filter((c) => c !== cell) : [...customCups, cell].sort();
    }
  }
  async function newGame() {
    if (st && st.phase !== 'LOBBY' && st.phase !== 'GAME_OVER' && !confirm('A game is in progress. Start a new one?')) return;
    const args = { class_side: side };
    if (variant === 'custom') Object.assign(args, { variant: null, cups: customCups, start: customStart });
    else args.variant = variant;
    await cmd('new_game', args);
  }

  // Quiz form
  let qText = $state('');
  let qOptions = $state('');
  let qAnswer = $state('');
  function openQuiz() {
    const options = qOptions.split(',').map((s) => s.trim()).filter(Boolean);
    cmd('quiz_open', { question: qText, options, answer: qAnswer.trim() || null });
  }

  let sayText = $state('');
  async function deleteVotes() {
    if (confirm('Delete all raw votes? The counts per round are kept.')) await cmd('delete_votes');
  }
  function exportStats() {
    window.open(`/admin/api/export?token=${encodeURIComponent(token)}`, '_blank');
  }

  const ago = (t) => (t ? `${Math.max(0, Math.round((Date.now() - t * 1000) / 1000))} s ago` : 'never');
</script>

{#if !conn}
  <main class="login">
    <form onsubmit={saveToken}>
      <h1>Minimax admin</h1>
      <label>Admin token <input type="password" bind:value={tokenInput} autocomplete="current-password" required /></label>
      <button class="primary" type="submit">Connect</button>
    </form>
  </main>
{:else}
  <header class="top">
    <h1>Minimax admin</h1>
    <span class="badge" class:good={live.connected} class:bad={!live.connected}>{live.connected ? 'server connected' : live.error || 'offline'}</span>
    {#if st}<span class="badge">{st.phase}</span>{/if}
    {#if bridge}
      <span class="badge" class:good={bridge.online} class:bad={!bridge.online}>bridge {bridge.online ? 'online' : 'offline'}</span>
      {#if bridge.battery !== null}<span class="badge" class:bad={bridge.battery < 30}>battery {bridge.battery}%</span>{/if}
      <span class="badge" class:warn={st?.robot?.mode === 'sim'}>{st?.robot?.mode === 'robot' ? 'robot mode' : 'no-robot mode'}</span>
    {/if}
    {#if status}<span class="badge">{status.clients.phones} phones · {status.clients.boards} boards</span>{/if}
    <span class="spacer"></span>
    <button class="stop" onclick={() => robot('stop')}>STOP ROBOT</button>
  </header>

  {#if live.error === 'bad admin token'}
    <p class="alert">The admin token was rejected. <button onclick={forgetToken}>Enter another token</button></p>
  {/if}
  {#if bridge?.warnings?.length}
    <p class="alert">Robot warnings: {bridge.warnings.join(' · ')}</p>
  {/if}
  {#if st?.robot?.error}
    <p class="alert">{st.robot.error}</p>
  {/if}

  {#if st}
    <main class="cards">
      <!-- Now -->
      <section class="card now">
        <h2>Now</h2>
        <p class="banner" style:--c={st.to_move ? colors[st.to_move] : '#6b7280'}>{banner(st)}</p>
        <p class="muted">{setupLabel(st.game.setup)} · class plays {st.game.class_side === 'BOTH' ? 'both sides (hall vs hall)' : st.game.class_side} · root value {fmtValue(st.game.root_value)} · path {st.path.join(' → ')}</p>
        <div class="now-row">
          <div class="mini">
            <Grid
              setup={st.game.setup}
              path={st.path}
              robotCell={st.robot.cell}
              options={st.phase === 'VOTING' && round ? round.options : []}
              optionColor={round ? colors[round.player] : colors.MAX}
              counts={status?.round_counts}
              winner={round?.winner}
              eyeColor={st.to_move ? colors[st.to_move] : '#9ca3af'}
            />
          </div>
          <div class="now-actions">
            {#if st.phase === 'LOBBY'}
              <button class="primary big" onclick={() => cmd('start')}>{st.game.restored ? 'Resume restored game' : 'Start game'}</button>
            {:else if st.phase === 'VOTING' && round && !round.closed}
              <div class="row">
                <Countdown {round} {now} total={settings.vote_seconds} color={colors[round.player]} size={70} />
                <div>
                  <b>{status?.round_votes ?? round.total}</b> votes<br />
                  {#each round.options as o (o)}<span class="count">{o}: {status?.round_counts?.[o] ?? '–'}</span>{/each}
                </div>
              </div>
              <div class="row">
                <button class="primary" onclick={() => cmd('close')}>Close now</button>
                <button onclick={() => cmd('extend', { seconds: 10 })}>+10 s</button>
                {#if round.paused}<button onclick={() => cmd('resume')}>Resume timer</button>
                {:else}<button onclick={() => cmd('pause')}>Pause timer</button>{/if}
              </div>
              <div class="row">
                <span class="muted">Override (show of hands):</span>
                {#each round.options as o (o)}<button onclick={() => cmd('override', { cell: o })}>Pick {o}</button>{/each}
              </div>
            {:else if st.phase === 'VOTING' && round?.tie}
              <p><b>Tie.</b> Pick the move:</p>
              <div class="row">
                {#each round.tie as o (o)}<button class="primary" onclick={() => cmd('override', { cell: o })}>Pick {o}</button>{/each}
              </div>
            {:else if st.phase === 'THINKING'}
              <p>Robot thinking with {live.search?.algorithm ?? settings.algorithm}. Best move: <b>{live.search?.best}</b> (value {fmtValue(live.search?.value)}).</p>
              <div class="row">
                <button class="primary" onclick={() => cmd('skip')}>Skip the wait</button>
                {#each st.options as o (o)}<button onclick={() => cmd('override', { cell: o })}>Robot goes to {o}</button>{/each}
              </div>
            {:else if st.phase === 'ROBOT_MOVING' && st.moving}
              <p>Walking {st.moving.from} → {st.moving.to} ({st.moving.mode === 'robot' ? 'robot' : 'board only'}).</p>
              <div class="row">
                <button onclick={() => cmd('robot_at', { cell: st.moving.to })}>Robot is at {st.moving.to}</button>
                {#if st.moving.failed}<button class="primary" onclick={() => cmd('retry_move')}>Retry move</button>{/if}
                {#if st.robot.mode === 'robot'}<button onclick={() => cmd('mode', { mode: 'sim' })}>Continue without robot</button>{/if}
              </div>
            {:else if st.phase === 'GAME_OVER'}
              <p class="result">{banner(st)}</p>
              <button class="primary" onclick={() => cmd('reset')}>Play again (same setup)</button>
            {/if}
          </div>
        </div>
      </section>

      <!-- New game -->
      <section class="card">
        <h2>New game</h2>
        <div class="seg">
          <label><input type="radio" bind:group={variant} value="1" /> Game 1 (cups C, H)</label>
          <label><input type="radio" bind:group={variant} value="2" /> Game 2 (cup I)</label>
          <label><input type="radio" bind:group={variant} value="custom" /> Custom</label>
        </div>
        {#if variant === 'custom'}
          <div class="custom">
            <div class="mini small">
              <Grid setup={{ cups: customCups, start: customStart }} path={[customStart]} robotCell={customStart} onpick={pickCustom} showPath={false} label="Custom setup" />
            </div>
            <div>
              <div class="seg vertical">
                <label><input type="radio" bind:group={editing} value="cups" /> Click to toggle cups</label>
                <label><input type="radio" bind:group={editing} value="start" /> Click to set the start</label>
              </div>
              <p class="muted">Start {customStart} · cups {customCups.join(', ') || 'none'}</p>
            </div>
          </div>
        {/if}
        <div class="seg">
          <label><input type="radio" bind:group={side} value="MAX" /> Class is MAX</label>
          <label><input type="radio" bind:group={side} value="MIN" /> Class is MIN</label>
          <label><input type="radio" bind:group={side} value="BOTH" /> Hall vs hall</label>
        </div>
        <button class="primary" onclick={newGame} disabled={variant === 'custom' && !customCups.length}>New game</button>
        <button onclick={() => cmd('reset')}>Reset current game</button>
      </section>

      <!-- Voting -->
      <section class="card">
        <h2>Voting</h2>
        <label class="field">Seconds per round
          <input type="number" min="3" max="600" value={settings.vote_seconds} onchange={(e) => set('vote_seconds', +e.currentTarget.value)} /></label>
        <label class="check"><input type="checkbox" checked={settings.auto_timer} onchange={(e) => set('auto_timer', e.currentTarget.checked)} /> Start the countdown automatically</label>
        <label class="check"><input type="checkbox" checked={settings.live_votes} onchange={(e) => set('live_votes', e.currentTarget.checked)} /> Show vote counts during voting (default: blind)</label>
        <label class="field">On a tie
          <select value={settings.tie_break} onchange={(e) => set('tie_break', e.currentTarget.value)}>
            <option value="random">random pick</option>
            <option value="first">first alphabetically</option>
            <option value="admin">I decide</option>
          </select></label>
      </section>

      <!-- Board and search -->
      <section class="card">
        <h2>Board and search</h2>
        <label class="field">Robot's algorithm
          <select value={settings.algorithm} onchange={(e) => set('algorithm', e.currentTarget.value)}>
            <option value="minimax">minimax</option>
            <option value="alphabeta">alpha-beta</option>
          </select></label>
        <label class="field">Values on the tree
          <select value={settings.values} onchange={(e) => set('values', e.currentTarget.value)}>
            <option value="hidden">hidden (fog)</option>
            <option value="leaves">leaves only</option>
            <option value="all">all values</option>
          </select></label>
        <label class="field">Animation step: {settings.step_ms} ms
          <input type="range" min="40" max="1500" step="10" value={settings.step_ms} onchange={(e) => set('step_ms', +e.currentTarget.value)} /></label>
        <label class="field">Max thinking time (s)
          <input type="number" min="1" max="120" value={settings.think_max_s} onchange={(e) => set('think_max_s', +e.currentTarget.value)} /></label>
        <label class="check"><input type="checkbox" checked={settings.speech} onchange={(e) => set('speech', e.currentTarget.checked)} /> Robot speaks</label>
        <label class="check"><input type="checkbox" checked={settings.hall_audio} onchange={(e) => set('hall_audio', e.currentTarget.checked)} /> Also play the robot's voice on the board (hall speakers)</label>
        <div class="row">
          <button onclick={() => cmd('replay', { algorithm: 'minimax', from_root: true })}>Replay minimax from root</button>
          <button onclick={() => cmd('replay', { algorithm: 'alphabeta', from_root: true })}>Replay alpha-beta from root</button>
          <button onclick={() => cmd('replay', { from_root: false })}>Replay from current position</button>
          <button onclick={() => cmd('clear_search')}>Clear search</button>
        </div>
      </section>

      <!-- Robot -->
      <section class="card robot">
        <h2>Robot</h2>
        <div class="seg">
          <label><input type="radio" name="mode" checked={st.robot.mode === 'robot'} onchange={() => cmd('mode', { mode: 'robot' })} /> Robot</label>
          <label><input type="radio" name="mode" checked={st.robot.mode === 'sim'} onchange={() => cmd('mode', { mode: 'sim' })} /> No robot (board only)</label>
        </div>
        {#if bridge}
          <dl>
            <dt>Bridge</dt><dd class:bad={!bridge.online}>{bridge.online ? 'online' : 'offline'} · last seen {ago(bridge.last_seen)}{bridge.driver_ok === false ? ' · driver down' : ''}</dd>
            <dt>Battery</dt><dd class:bad={bridge.battery !== null && bridge.battery < 30}>{bridge.battery ?? '–'}%</dd>
            <dt>Running</dt><dd>{bridge.busy ? `${bridge.busy.cmd} (#${bridge.busy.cmd_id})` : '–'} · {bridge.queued} queued</dd>
            <dt>Last</dt><dd>{bridge.last_result ? `#${bridge.last_result.cmd_id} ${bridge.last_result.status}${bridge.last_result.error ? `: ${bridge.last_result.error}` : ''}` : '–'}</dd>
            <dt>Position</dt><dd>server thinks the robot is on {st.robot.cell}</dd>
          </dl>
        {/if}
        <div class="row">
          <button onclick={() => robot('posture', { posture: 'stand' })}>Stand</button>
          <button onclick={() => robot('posture', { posture: 'rest' })}>Rest</button>
          <button onclick={() => robot('posture', { posture: 'lie' })}>Lie down</button>
          {#each ['think', 'win', 'lose', 'wave', 'dance', 'handshake'] as a (a)}<button onclick={() => robot('animate', { name: a })}>{a}</button>{/each}
        </div>
        <div class="row">
          <span class="muted">Eyes:</span>
          <button style:--c={colors.MAX} class="swatch" onclick={() => robot('eyes', { color: colors.MAX })}>MAX</button>
          <button style:--c={colors.MIN} class="swatch" onclick={() => robot('eyes', { color: colors.MIN })}>MIN</button>
          <button onclick={() => robot('eyes', { color: '#ffffff' })}>white</button>
        </div>
        <form class="row" onsubmit={(e) => { e.preventDefault(); if (sayText.trim()) robot('say', { text: sayText }); }}>
          <input class="grow" placeholder="Say something…" bind:value={sayText} maxlength="300" />
          <button type="submit">Say</button>
        </form>
        <div class="nudge">
          <span class="muted">Nudge (robot's frame):</span>
          <div class="pad">
            <button onclick={() => robot('nudge', { dtheta: 10 })} title="turn left 10°">⟲ 10°</button>
            <button onclick={() => robot('nudge', { dx: 0.05 })} title="forward 5 cm">▲ 5 cm</button>
            <button onclick={() => robot('nudge', { dtheta: -10 })} title="turn right 10°">10° ⟳</button>
            <button onclick={() => robot('nudge', { dy: 0.05 })} title="robot's left 5 cm">◀ 5 cm</button>
            <button onclick={() => robot('nudge', { dx: -0.05 })} title="back 5 cm">▼ 5 cm</button>
            <button onclick={() => robot('nudge', { dy: -0.05 })} title="robot's right 5 cm">5 cm ▶</button>
          </div>
        </div>
        <div class="custom">
          <div class="mini small">
            <Grid setup={st.game.setup} path={st.path} robotCell={st.robot.cell} onpick={(c) => cmd('robot_at', { cell: c })} showPath={false} label="Robot is at" />
          </div>
          <p class="muted">Click a cell to tell the server where the robot really is ("Robot is at X"). If a walk is pending to that cell, the game continues.</p>
        </div>
      </section>

      <!-- Quiz -->
      <section class="card">
        <h2>Quiz</h2>
        {#if st.quiz}
          <p><b>{st.quiz.question}</b> · {status?.quiz_votes ?? st.quiz.total} answers · {st.quiz.open ? 'open' : 'closed'}{st.quiz.revealed ? ', revealed' : ''}</p>
          <p>{#each st.quiz.options as o (o)}<span class="count">{o}: {status?.quiz_counts?.[o] ?? '–'}</span>{/each}</p>
          <div class="row">
            <button onclick={() => cmd('quiz_close')} disabled={!st.quiz.open}>Close</button>
            <button class="primary" onclick={() => cmd('quiz_reveal')}>Reveal answer</button>
            <button onclick={() => cmd('quiz_clear')}>Remove from screens</button>
          </div>
        {:else}
          <div class="row">
            <button onclick={() => cmd('quiz_open', { preset: 'root_value' })}>"Value of the root?"</button>
            <button onclick={() => cmd('quiz_open', { preset: 'best_move' })} disabled={!st.options.length}>"Best move now?"</button>
          </div>
          <label class="field">Question <input bind:value={qText} maxlength="200" /></label>
          <label class="field">Options (comma separated) <input bind:value={qOptions} placeholder="-1, 0, +1" /></label>
          <label class="field">Correct answer (optional) <input bind:value={qAnswer} /></label>
          <button onclick={openQuiz} disabled={!qText.trim() || qOptions.split(',').filter((s) => s.trim()).length < 2}>Open custom quiz</button>
        {/if}
      </section>

      <!-- Data -->
      <section class="card">
        <h2>Data</h2>
        <p class="muted">Votes are stored per round as an HMAC of a random device id. No IPs or names. After the lecture, export the counts and delete the raw votes.</p>
        <div class="row">
          <button onclick={exportStats}>Download vote stats (JSON)</button>
          <button class="danger" onclick={deleteVotes}>Delete raw votes</button>
        </div>
        <p class="muted">Database: {status?.db_path}</p>
        <button class="link" onclick={forgetToken}>Log out of admin</button>
      </section>

      <section class="card log">
        <h2>Log</h2>
        {#each log as l, i (i)}
          <p class:bad={!l.ok}><span class="muted">{l.time}</span> {l.name} {l.ok ? 'ok' : `failed: ${l.msg}`}</p>
        {:else}<p class="muted">Commands you send appear here.</p>{/each}
      </section>
    </main>
  {:else}
    <p class="muted pad-left">Waiting for the server…</p>
  {/if}
{/if}

<style>
  :global(body) {
    background: #f4f4f5;
  }
  .login {
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 16px;
  }
  .login form {
    background: #fff;
    padding: 28px;
    border-radius: 12px;
    display: grid;
    gap: 14px;
    width: min(100%, 360px);
  }
  .login input {
    width: 100%;
  }
  .top {
    position: sticky;
    top: 0;
    z-index: 5;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    padding: 10px 16px;
    background: #fff;
    border-bottom: 1px solid var(--line);
  }
  h1 {
    margin: 0 8px 0 0;
    font-size: 20px;
    color: var(--accent);
  }
  h2 {
    margin: 0 0 10px;
    font-size: 16px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }
  .spacer {
    flex: 1;
  }
  .badge {
    font-size: 13px;
    padding: 3px 9px;
    border-radius: 999px;
    background: #f4f4f5;
    border: 1px solid var(--line);
  }
  .badge.good {
    background: #dcfce7;
    border-color: #86efac;
  }
  .badge.bad {
    background: #fee2e2;
    border-color: #fca5a5;
  }
  .badge.warn {
    background: #fef3c7;
    border-color: #fcd34d;
  }
  .stop {
    background: var(--bad);
    color: #fff;
    border: none;
    font-weight: 800;
    padding: 10px 18px;
    border-radius: 8px;
    letter-spacing: 0.04em;
    cursor: pointer;
  }
  .alert {
    margin: 10px 16px 0;
    padding: 10px 12px;
    background: #fef3c7;
    border: 1px solid #fcd34d;
    border-radius: 8px;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 360px), 1fr));
    gap: 14px;
    padding: 14px 16px 40px;
  }
  .card {
    background: #fff;
    border-radius: 12px;
    padding: 16px;
    border: 1px solid var(--line);
    min-width: 0;
  }
  .card.now {
    grid-column: 1 / -1;
  }
  .banner {
    margin: 0;
    font-size: 22px;
    font-weight: 700;
    border-left: 6px solid var(--c);
    padding-left: 10px;
  }
  .muted {
    color: var(--muted);
    font-size: 14px;
  }
  .now-row {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
    align-items: flex-start;
  }
  .mini {
    width: 210px;
    flex: none;
  }
  .mini.small {
    width: 150px;
  }
  .now-actions {
    flex: 1;
    min-width: 260px;
    display: grid;
    gap: 10px;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin: 6px 0;
  }
  .count {
    display: inline-block;
    margin-right: 12px;
    font-weight: 600;
  }
  .result {
    font-size: 20px;
    font-weight: 700;
    margin: 0;
  }
  button {
    border: 1px solid #d4d4d8;
    background: #fff;
    border-radius: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 14px;
  }
  button:hover:not(:disabled) {
    background: #f4f4f5;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  button.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
  }
  button.primary:hover:not(:disabled) {
    background: #312e81;
  }
  button.big {
    font-size: 18px;
    padding: 14px 22px;
  }
  button.danger {
    border-color: var(--bad);
    color: var(--bad);
  }
  button.swatch {
    border: 2px solid var(--c);
    color: var(--c);
    font-weight: 700;
  }
  button.link {
    border: none;
    background: none;
    color: var(--muted);
    text-decoration: underline;
    padding: 0;
  }
  .seg {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    margin: 8px 0 12px;
  }
  .seg.vertical {
    flex-direction: column;
  }
  .field {
    display: grid;
    gap: 4px;
    margin: 0 0 10px;
    font-size: 14px;
  }
  .check {
    display: flex;
    gap: 8px;
    align-items: center;
    margin: 0 0 10px;
    font-size: 14px;
  }
  input:not([type='checkbox']):not([type='radio']),
  select {
    font: inherit;
    padding: 7px 9px;
    border: 1px solid #d4d4d8;
    border-radius: 7px;
    background: #fff;
    min-width: 0;
  }
  .grow {
    flex: 1;
  }
  .custom {
    display: flex;
    gap: 14px;
    align-items: center;
    margin: 6px 0 10px;
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 3px 12px;
    font-size: 14px;
    margin: 0 0 10px;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
  }
  .bad {
    color: var(--bad);
  }
  .nudge {
    margin: 10px 0;
  }
  .pad {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 92px));
    gap: 6px;
    margin-top: 6px;
  }
  .log p {
    margin: 0 0 4px;
    font-size: 14px;
  }
  .pad-left {
    padding: 16px;
  }
</style>
