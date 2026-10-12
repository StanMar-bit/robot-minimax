<script>
  // Projector view (1920×1080): grid mirror and turn on the left, game tree on the right.
  import Countdown from '../lib/Countdown.svelte';
  import Grid from '../lib/Grid.svelte';
  import Qr from '../lib/Qr.svelte';
  import Tree from '../lib/Tree.svelte';
  import { banner, fmtValue, setupLabel, who } from '../lib/labels.js';
  import { connect, serverNow } from '../lib/live.svelte.js';

  const { live } = connect({ role: 'board' });

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = serverNow(live)), 50);
    return () => clearInterval(id);
  });

  const st = $derived(live.state);
  const colors = $derived(st?.game?.colors ?? { MAX: '#2563eb', MIN: '#ea580c' });
  const round = $derived(st?.round);
  const joinUrl = $derived(live.hello?.public_url || `${location.origin}/`);
  const shortUrl = $derived(joinUrl.replace(/^https?:\/\//, '').replace(/\/$/, ''));
  const turnPlayer = $derived(
    st?.phase === 'ROBOT_MOVING' ? st?.moving?.player : st?.phase === 'GAME_OVER' ? st?.result?.winner : st?.to_move,
  );
  const eyeColor = $derived(turnPlayer ? colors[turnPlayer] : '#9ca3af');

  // How far the robot's search animation has got.
  const progress = $derived.by(() => {
    const s = live.search;
    if (!s?.trace?.length) return null;
    const upto = Math.min(s.trace.length, Math.floor(Math.max(0, now - s.started_at) / s.step_ms) + 1);
    let visited = 0;
    for (let i = 0; i < upto; i++) if (s.trace[i].t === 'enter') visited++;
    return { visited, done: upto >= s.trace.length };
  });

  const treeStats = $derived(live.tree?.stats);
  const quiz = $derived(st?.quiz);
  const quizTotal = $derived(quiz?.counts ? Object.values(quiz.counts).reduce((a, b) => a + b, 0) : 0);
</script>

<main class="board">
  <section class="left">
    <header>
      <h1>Minimax vs NAO</h1>
      {#if st}<p class="setup">{setupLabel(st.game.setup)}{#if st.robot.mode === 'sim'}<span class="mode">no-robot mode</span>{/if}</p>{/if}
    </header>

    <div class="grid-wrap">
      {#if st}
        <Grid
          setup={st.game.setup}
          path={st.path}
          robotCell={st.cell}
          options={st.phase === 'VOTING' && round ? round.options : []}
          optionColor={round ? colors[round.player] : colors.MAX}
          winner={st.phase === 'VOTING' ? round?.winner : null}
          counts={st.phase === 'VOTING' ? round?.counts : null}
          {eyeColor}
          moveMs={st.moving?.duration_ms || 1200}
        />
      {/if}
    </div>

    <div class="turn" style:--turn={turnPlayer ? colors[turnPlayer] : '#6b7280'}>
      <span class="chip">{turnPlayer ?? '–'}</span>
      <span class="text">{banner(st)}</span>
    </div>

    <div class="info">
      {#if st?.phase === 'VOTING' && round && !round.closed}
        <Countdown {round} {now} total={st.settings.vote_seconds} color={colors[round.player]} size={130} />
        <div class="votes">
          <span class="big">{round.total}</span>
          <span>{round.total === 1 ? 'vote' : 'votes'}{round.counts ? '' : ' (hidden until time is up)'}</span>
        </div>
      {:else if st?.phase === 'VOTING' && round?.closed}
        <div class="tally">
          {#each round.options as o (o)}
            <span class:win={round.winner === o}>{o}: <b>{round.counts?.[o] ?? 0}</b></span>
          {/each}
        </div>
      {:else if st?.phase === 'THINKING' && progress}
        <div class="thinking">
          <span class="big">{progress.visited}</span>
          <span>positions evaluated with {live.search.algorithm === 'alphabeta' ? 'alpha-beta' : 'minimax'}</span>
        </div>
      {:else if st?.phase === 'GAME_OVER'}
        <div class="result" style:--res={st.result?.winner ? colors[st.result.winner] : '#6b7280'}>
          {st.result?.winner ? `${st.result.winner} wins` : 'Draw'}
          <small>value {fmtValue(st.result?.value)} for MAX</small>
        </div>
      {:else if st?.phase === 'LOBBY'}
        <p class="hint">Scan the QR code to vote for the robot's moves.</p>
      {/if}
    </div>

    <footer class="join">
      <Qr text={joinUrl} size={190} />
      <div>
        <p class="url">{shortUrl}</p>
        <p class="sub">
          {st?.clients?.phones ?? 0} phones connected
          {#if st?.game?.class_side === 'BOTH'}<br />Left half = MAX · right half = MIN{/if}
        </p>
      </div>
    </footer>
  </section>

  <section class="right">
    <div class="legend">
      <span><i class="sw path"></i>played path</span>
      <span><i class="sw search"></i>robot's search</span>
      {#if live.search?.algorithm === 'alphabeta'}<span><i class="sw pruned"></i>pruned</span>{/if}
      <span class="spacer"></span>
      {#if st && !live.search && st.settings.values === 'hidden'}<span class="fog">values hidden</span>{/if}
    </div>
    <div class="tree-wrap">
      {#if live.tree && st}
        <Tree
          tree={live.tree}
          path={st.path}
          phase={st.phase}
          {round}
          search={live.search}
          {now}
          values={st.settings.values}
          {colors}
          toMove={st.to_move}
        />
      {/if}
      {#if quiz}
        <div class="quiz">
          <h2>{quiz.question}</h2>
          <ul>
            {#each quiz.options as o (o)}
              {@const c = quiz.counts?.[o] ?? 0}
              <li class:answer={quiz.answer === o}>
                <span class="opt">{o}</span>
                <span class="bar"><i style:width="{quizTotal ? (100 * c) / quizTotal : 0}%"></i></span>
                <span class="n">{quiz.counts ? c : ''}</span>
              </li>
            {/each}
          </ul>
          <p>{quiz.total} {quiz.total === 1 ? 'answer' : 'answers'}{quiz.open ? ' so far' : ''}</p>
        </div>
      {/if}
    </div>
    <div class="stats">
      {#if treeStats}
        <span>Full tree: <b>{treeStats.nodes_total}</b> nodes</span>
        <span>Minimax visits <b>{treeStats.minimax_visited}</b></span>
        <span>Alpha-beta visits <b>{treeStats.alphabeta_visited}</b> (prunes {treeStats.alphabeta_pruned})</span>
      {/if}
      {#if live.search?.stats}
        <span class="search-stat">
          {live.search.replay ? 'Replay' : 'Robot'} · {live.search.algorithm === 'alphabeta' ? 'alpha-beta' : 'minimax'} from
          {live.search.root}: <b>{progress?.visited ?? 0}</b> of {live.search.stats.nodes_total} nodes{#if
            progress?.done}{#if live.search.stats.nodes_pruned}, {live.search.stats.nodes_pruned} pruned{/if} · value
            <b>{fmtValue(live.search.value)}</b>{#if !live.search.replay} · best move <b>{live.search.best}</b>{/if}{/if}
        </span>
      {/if}
    </div>
  </section>

  {#if !live.connected}
    <div class="offline">{live.everConnected ? 'Reconnecting…' : 'Connecting…'}</div>
  {/if}
</main>

<style>
  :global(body) {
    overflow: hidden;
  }
  .board {
    display: grid;
    grid-template-columns: minmax(0, 35fr) minmax(0, 65fr);
    height: 100vh;
    width: 100vw;
    background: #fff;
  }
  .left {
    display: flex;
    flex-direction: column;
    gap: 1.6vh;
    padding: 2.6vh 2.4vw 2.4vh 2.6vw;
    border-right: 2px solid var(--line);
    min-height: 0;
  }
  header h1 {
    margin: 0;
    font-size: clamp(28px, 3.3vw, 64px);
    font-weight: 500;
    color: #3730a3;
    letter-spacing: -0.01em;
    line-height: 1.05;
  }
  .setup {
    margin: 0.4vh 0 0;
    font-size: clamp(14px, 1.15vw, 22px);
    color: var(--muted);
  }
  .grid-wrap {
    width: min(100%, 40vh);
    align-self: center;
  }
  .turn {
    display: flex;
    align-items: center;
    gap: 0.8vw;
    font-size: clamp(18px, 1.55vw, 30px);
    font-weight: 600;
    line-height: 1.2;
  }
  .chip {
    flex: none;
    background: var(--turn);
    color: #fff;
    border-radius: 6px;
    padding: 0.25em 0.55em;
    font-size: 0.8em;
    letter-spacing: 0.05em;
  }
  .info {
    flex: 1;
    min-height: 0;
    display: flex;
    align-items: center;
    gap: 1.4vw;
    font-size: clamp(14px, 1.15vw, 22px);
  }
  .votes,
  .thinking {
    display: flex;
    flex-direction: column;
  }
  .big {
    font-size: clamp(36px, 3.4vw, 64px);
    font-weight: 700;
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .tally {
    display: flex;
    gap: 1.6vw;
    font-size: clamp(20px, 1.8vw, 34px);
  }
  .tally .win {
    text-decoration: underline 4px;
    text-underline-offset: 6px;
  }
  .result {
    font-size: clamp(32px, 3vw, 58px);
    font-weight: 700;
    color: var(--res);
    display: flex;
    flex-direction: column;
  }
  .result small {
    font-size: 0.4em;
    color: var(--muted);
    font-weight: 500;
  }
  .hint {
    color: var(--muted);
    margin: 0;
  }
  .join {
    display: flex;
    align-items: center;
    gap: 1.2vw;
  }
  .url {
    margin: 0;
    font-size: clamp(18px, 1.6vw, 32px);
    font-weight: 700;
    word-break: break-all;
  }
  .sub {
    margin: 0.4vh 0 0;
    color: var(--muted);
    font-size: clamp(13px, 1vw, 19px);
  }
  .right {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    padding: 2vh 1.4vw 1.4vh;
  }
  .legend {
    display: flex;
    gap: 1.6vw;
    align-items: center;
    font-size: clamp(12px, 0.95vw, 18px);
    color: var(--muted);
  }
  .legend .spacer {
    flex: 1;
  }
  .sw {
    display: inline-block;
    width: 1.1em;
    height: 0.75em;
    margin-right: 0.4em;
    vertical-align: -0.05em;
    border: 2px solid #111827;
  }
  .sw.path {
    background: #fde68a;
  }
  .sw.search {
    background: #ddd6fe;
    border-color: var(--search);
  }
  .sw.pruned {
    background: #d4d4d8;
    border-style: dashed;
    border-color: #9ca3af;
  }
  .fog {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.1em 0.7em;
  }
  .tree-wrap {
    position: relative;
    flex: 1;
    min-height: 0;
  }
  .stats {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4vh 2vw;
    font-size: clamp(13px, 1.05vw, 20px);
    color: #374151;
    border-top: 2px solid var(--line);
    padding-top: 1vh;
  }
  .search-stat {
    color: var(--search);
  }
  .quiz {
    position: absolute;
    inset: 8% 10%;
    background: rgba(255, 255, 255, 0.97);
    border: 3px solid #3730a3;
    border-radius: 14px;
    padding: 4vh 3vw;
    box-shadow: 0 20px 60px rgba(17, 24, 39, 0.18);
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  .quiz h2 {
    margin: 0 0 3vh;
    font-size: clamp(26px, 2.4vw, 46px);
    font-weight: 600;
  }
  .quiz ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 1.6vh;
  }
  .quiz li {
    display: grid;
    grid-template-columns: 5em 1fr 3em;
    align-items: center;
    gap: 1vw;
    font-size: clamp(22px, 2vw, 40px);
    font-weight: 600;
  }
  .quiz li.answer .opt {
    color: var(--ok);
  }
  .quiz li.answer .opt::after {
    content: ' ✓';
  }
  .quiz .bar {
    height: 1.1em;
    background: var(--panel);
    border-radius: 4px;
    overflow: hidden;
  }
  .quiz .bar i {
    display: block;
    height: 100%;
    background: #3730a3;
    transition: width 0.5s;
  }
  .quiz li.answer .bar i {
    background: var(--ok);
  }
  .quiz p {
    margin: 3vh 0 0;
    color: var(--muted);
    font-size: clamp(16px, 1.2vw, 24px);
  }
  .offline {
    position: fixed;
    right: 12px;
    bottom: 12px;
    background: var(--bad);
    color: #fff;
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 600;
  }
  .mode {
    margin-left: 0.8em;
    font-size: 0.75em;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.1em 0.7em;
    vertical-align: 0.1em;
  }
</style>
