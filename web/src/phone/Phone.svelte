<script>
  // Student phone: vote for the robot's next move. No login; a random device id is kept in localStorage.
  import Countdown from '../lib/Countdown.svelte';
  import Grid from '../lib/Grid.svelte';
  import { resultLine, who } from '../lib/labels.js';
  import { connect, load, save, serverNow } from '../lib/live.svelte.js';

  let team = $state(load('minimax.team'));
  const { live, call, hello } = connect({ role: 'phone', team: () => team });

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = serverNow(live)), 250);
    return () => clearInterval(id);
  });

  const st = $derived(live.state);
  const colors = $derived(st?.game?.colors ?? { MAX: '#2563eb', MIN: '#ea580c' });
  const round = $derived(st?.round);
  const quiz = $derived(st?.quiz);
  const both = $derived(st?.game?.class_side === 'BOTH');
  const mySide = $derived(both ? (team === 'left' ? 'MAX' : team === 'right' ? 'MIN' : null) : st?.game?.class_side);
  const myOwner = $derived(both ? team : 'class');
  const myTurn = $derived(!!round && round.team === myOwner);

  // My vote in the current round, confirmed by the server.
  let mine = $state({ round_id: null, choice: null, pending: null, error: null });
  let quizMine = $state({ quiz_id: null, choice: null, pending: null, error: null });

  $effect(() => {
    const h = live.hello;
    if (!h) return;
    if (h.round_id) mine = { round_id: h.round_id, choice: h.choice ?? null, pending: null, error: null };
    if (h.quiz_id) quizMine = { quiz_id: h.quiz_id, choice: h.quiz_choice ?? null, pending: null, error: null };
  });
  $effect(() => {
    if (round && round.id !== mine.round_id) mine = { round_id: round.id, choice: null, pending: null, error: null };
  });
  $effect(() => {
    if (quiz && quiz.id !== quizMine.quiz_id) quizMine = { quiz_id: quiz.id, choice: null, pending: null, error: null };
  });

  async function vote(choice) {
    if (!round || round.closed) return;
    const rid = round.id;
    mine.pending = choice;
    mine.error = null;
    const res = await call('vote', { round_id: rid, choice, team });
    if (mine.round_id !== rid) return;
    mine.pending = null;
    if (res.ok) mine.choice = res.choice;
    else mine.error = res.error || 'Vote not received. Try again.';
  }

  async function answer(choice) {
    if (!quiz?.open) return;
    const qid = quiz.id;
    quizMine.pending = choice;
    quizMine.error = null;
    const res = await call('quiz_vote', { quiz_id: qid, choice });
    if (quizMine.quiz_id !== qid) return;
    quizMine.pending = null;
    if (res.ok) quizMine.choice = res.choice;
    else quizMine.error = res.error || 'Answer not received. Try again.';
  }

  function pickTeam(t) {
    team = t;
    save('minimax.team', t);
    hello();
  }

  const outcome = $derived.by(() => {
    const r = st?.result;
    if (!r) return null;
    if (!r.winner) return { title: 'Draw', text: 'No legal moves left.' };
    if (r.owner === myOwner) return { title: 'You win!', text: `${r.winner} reached the cup on ${r.cell}.` };
    if (r.owner === 'robot') return { title: 'The robot wins', text: 'Minimax looked further ahead.' };
    return { title: `${r.winner} wins`, text: resultLine(st) };
  });
</script>

<div class="app">
  <header>
    <h1>Minimax vs NAO</h1>
    <span class="dot" class:on={live.connected} title={live.connected ? 'connected' : 'offline'}></span>
  </header>

  {#if !st}
    <section class="card center">
      <p class="lead">{live.error ? 'Cannot reach the server. Retrying…' : 'Connecting…'}</p>
    </section>
  {:else}
    {#if both && !team}
      <section class="card">
        <h2>Which half of the hall are you in?</h2>
        <div class="teams">
          <button class="team" style:--c={colors.MAX} onclick={() => pickTeam('left')}>Left half<small>you play MAX</small></button>
          <button class="team" style:--c={colors.MIN} onclick={() => pickTeam('right')}>Right half<small>you play MIN</small></button>
        </div>
      </section>
    {:else if mySide}
      <p class="side" style:--c={colors[mySide]}>
        You are <b>{mySide}</b>{both ? ` (${team} half)` : ''}
        {#if both}<button class="link" onclick={() => pickTeam(team === 'left' ? 'right' : 'left')}>switch</button>{/if}
      </p>
    {/if}

    {#if quiz && (quiz.open || quiz.revealed || quizMine.choice)}
      <section class="card">
        <h2>{quiz.question}</h2>
        <div class="options" class:many={quiz.options.length > 4}>
          {#each quiz.options as o (o)}
            <button
              class="opt"
              class:chosen={quizMine.choice === o}
              class:pending={quizMine.pending === o}
              class:answer={quiz.revealed && quiz.answer === o}
              style:--c="#3730a3"
              disabled={!quiz.open || !live.connected}
              onclick={() => answer(o)}
            >
              {o}{#if quizMine.choice === o}<span class="tick">✓</span>{/if}
            </button>
          {/each}
        </div>
        <p class="status" class:err={quizMine.error}>
          {#if quizMine.error}{quizMine.error}
          {:else if quiz.revealed}{quizMine.choice === quiz.answer ? 'Correct!' : `The answer is ${quiz.answer}.`}
          {:else if !quiz.open}Answers are closed. Look at the board.
          {:else if quizMine.choice}Answer received. You can still change it.
          {:else}Tap an answer.{/if}
        </p>
      </section>
    {:else if st.phase === 'VOTING' && round && myTurn && !round.closed}
      <section class="card">
        <div class="vote-head">
          <h2>Where should the robot go?</h2>
          <Countdown {round} {now} total={st.settings.vote_seconds} color={colors[round.player]} size={64} />
        </div>
        <div class="options">
          {#each round.options as o (o)}
            <button
              class="opt"
              class:chosen={mine.choice === o}
              class:pending={mine.pending === o}
              style:--c={colors[round.player]}
              disabled={!live.connected}
              onclick={() => vote(o)}
              aria-pressed={mine.choice === o}
            >
              {o}{#if mine.choice === o}<span class="tick">✓</span>{/if}
            </button>
          {/each}
        </div>
        <p class="status" class:err={mine.error} aria-live="polite">
          {#if mine.error}{mine.error}
          {:else if mine.pending}Sending…
          {:else if mine.choice}Vote received: {mine.choice}. You can change it until time is up.
          {:else if !live.connected}Offline. Reconnecting…
          {:else}{round.total} {round.total === 1 ? 'vote' : 'votes'} so far.{/if}
        </p>
      </section>
    {:else}
      <section class="card center">
        {#if st.phase === 'LOBBY'}
          <h2>Get ready</h2>
          <p class="lead">The game starts soon. Take turns with the robot; the first to reach a cup wins.</p>
        {:else if st.phase === 'VOTING' && round?.tie}
          <h2>It's a tie</h2>
          <p class="lead">{round.tie.join(' and ')} got the same number of votes. The TA decides.</p>
        {:else if st.phase === 'VOTING' && round?.closed}
          <h2>{round.team === myOwner ? 'You' : who(round.team, round.player)} chose {round.winner}</h2>
          {#if mine.choice}<p class="lead">Your vote: {mine.choice}</p>{/if}
        {:else if st.phase === 'VOTING'}
          <h2>{who(round.team, round.player)} is voting</h2>
          <p class="lead">Wait for your turn.</p>
        {:else if st.phase === 'THINKING'}
          <h2>The robot is thinking…</h2>
          <p class="lead">Watch the board: it searches the game tree with minimax.</p>
        {:else if st.phase === 'ROBOT_MOVING'}
          <h2>Moving to {st.moving?.to}</h2>
          <p class="lead">Watch the robot.</p>
        {:else if st.phase === 'GAME_OVER' && outcome}
          <h2 class="outcome">{outcome.title}</h2>
          <p class="lead">{outcome.text}</p>
        {/if}
      </section>
    {/if}

    <div class="mini">
      <Grid
        setup={st.game.setup}
        path={st.path}
        robotCell={st.cell}
        options={st.phase === 'VOTING' && round && !round.closed ? round.options : []}
        optionColor={round ? colors[round.player] : colors.MAX}
        winner={round?.winner}
        eyeColor={st.to_move ? colors[st.to_move] : '#9ca3af'}
        moveMs={st.moving?.duration_ms || 1200}
        label="Current position"
      />
    </div>
  {/if}

  <footer>Anonymous: no login, no personal data. Votes are counted once per phone.</footer>
</div>

<style>
  :global(:root) {
    color-scheme: light dark;
  }
  @media (prefers-color-scheme: dark) {
    :global(:root:not([data-theme='light'])) {
      --bg: #0f1115;
      --panel: #1a1d24;
      --ink: #f3f4f6;
      --muted: #9ca3af;
      --line: #2a2f3a;
      --cell: #1f232b;
      --visited: #3a3f4b;
      --trail: #9ca3af;
      --hover: #312e81;
    }
  }
  .app {
    max-width: 520px;
    margin: 0 auto;
    min-height: 100vh;
    min-height: 100dvh;
    padding: max(12px, env(safe-area-inset-top)) 16px max(16px, env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 600;
    color: var(--accent);
  }
  @media (prefers-color-scheme: dark) {
    h1 {
      color: #a5b4fc;
    }
  }
  .dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--bad);
  }
  .dot.on {
    background: var(--ok);
  }
  .side {
    margin: 0;
    font-size: 17px;
    border-left: 6px solid var(--c);
    padding: 2px 0 2px 10px;
  }
  .side b {
    color: var(--c);
  }
  .link {
    background: none;
    border: none;
    color: var(--muted);
    text-decoration: underline;
    padding: 0 0 0 8px;
    cursor: pointer;
  }
  .card {
    background: var(--panel);
    border-radius: 14px;
    padding: 18px 16px;
  }
  .card.center {
    text-align: center;
    padding: 26px 16px;
  }
  h2 {
    margin: 0 0 6px;
    font-size: 22px;
    line-height: 1.25;
  }
  .outcome {
    font-size: 32px;
  }
  .lead {
    margin: 0;
    color: var(--muted);
    font-size: 16px;
    line-height: 1.45;
  }
  .vote-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 14px;
  }
  .vote-head h2 {
    margin: 0;
  }
  .options {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .options.many {
    grid-template-columns: 1fr 1fr 1fr;
  }
  .opt {
    position: relative;
    min-height: 92px;
    font-size: 40px;
    font-weight: 700;
    border-radius: 12px;
    border: 3px solid var(--c);
    background: var(--bg);
    color: var(--c);
    cursor: pointer;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
  }
  .opt:active {
    transform: scale(0.98);
  }
  .opt.chosen {
    background: var(--c);
    color: #fff;
  }
  .opt.pending {
    opacity: 0.6;
  }
  .opt.answer {
    outline: 4px solid var(--ok);
    outline-offset: 2px;
  }
  .opt:disabled {
    cursor: default;
    opacity: 0.55;
  }
  .opt.chosen:disabled {
    opacity: 0.85;
  }
  .tick {
    position: absolute;
    top: 6px;
    right: 10px;
    font-size: 22px;
  }
  .status {
    margin: 14px 0 0;
    font-size: 15px;
    color: var(--muted);
    min-height: 1.4em;
  }
  .status.err {
    color: var(--bad);
    font-weight: 600;
  }
  .teams {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 12px;
  }
  .team {
    border: 3px solid var(--c);
    background: var(--bg);
    color: var(--c);
    border-radius: 12px;
    padding: 18px 8px;
    font-size: 20px;
    font-weight: 700;
    display: flex;
    flex-direction: column;
    gap: 4px;
    cursor: pointer;
  }
  .team small {
    font-size: 13px;
    font-weight: 500;
    color: var(--muted);
  }
  .mini {
    width: min(100%, 260px);
    align-self: center;
  }
  footer {
    margin-top: auto;
    text-align: center;
    color: var(--muted);
    font-size: 12px;
  }
</style>
