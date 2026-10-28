<script>
  // Offline playground for students: the grid game from the lecture against the computer,
  // with the game tree and step-by-step minimax / alpha-beta replays. No server needed;
  // everything runs in the browser (src/lib/game.js), so it can be hosted on GitHub Pages.
  import { tick, untrack } from 'svelte';
  import Grid from '../lib/Grid.svelte';
  import Tree from '../lib/Tree.svelte';
  import { fmtBound, fmtValue } from '../lib/labels.js';
  import {
    MAX,
    MIN,
    SIZES,
    VARIANTS,
    buildTree,
    cellNames,
    legalMoves,
    makeGame,
    nodeId,
    search as runSearch,
    terminalKind,
    toMove,
    validate,
  } from '../lib/game.js';

  const REPO = 'https://github.com/StanMar-bit/robot-minimax';
  const colors = { MAX: '#2563eb', MIN: '#ea580c' };
  const SPEEDS = [1200, 700, 400, 220, 110, 45, 15]; // ms per trace event
  const ZOOMS = [0, 20, 30, 44, 64]; // px per leaf; 0 = fit the tree to the panel
  const ALG = { minimax: 'minimax', alphabeta: 'alpha-beta' };
  const PLAYER_KINDS = ['human', 'cpu'];

  // ---- URL hash: games and settings can be shared as a link ----

  function readHash() {
    const q = new URLSearchParams(location.hash.slice(1));
    let setup = VARIANTS[1];
    if (VARIANTS[q.get('game')]) {
      setup = VARIANTS[q.get('game')];
    } else if (q.has('cups')) {
      const size = SIZES.includes(q.get('size')) ? q.get('size') : '3x3';
      const [rows, cols] = size.split('x').map(Number);
      const letters = (k) => [...new Set((q.get(k) || '').toUpperCase().replace(/[^A-P]/g, ''))].sort();
      const custom = { name: 'custom', rows, cols, start: (q.get('start') || 'A').toUpperCase(), cups: letters('cups'), blocked: letters('blocked') };
      if (!validate(custom).length) setup = custom;
    }
    const kind = (v, dflt) => (PLAYER_KINDS.includes(v) ? v : dflt);
    return {
      setup,
      players: { MAX: kind(q.get('max'), 'human'), MIN: kind(q.get('min'), 'cpu') },
      algorithm: q.get('alg') === 'alphabeta' ? 'alphabeta' : 'minimax',
    };
  }

  function hashFor(setup, players, algorithm) {
    const q = new URLSearchParams();
    if (setup.name === 'custom') {
      q.set('size', `${setup.rows}x${setup.cols}`);
      q.set('start', setup.start);
      q.set('cups', setup.cups.join(''));
      if (setup.blocked.length) q.set('blocked', setup.blocked.join(''));
    } else {
      q.set('game', setup.name);
    }
    if (players.MAX !== 'human') q.set('max', players.MAX);
    if (players.MIN !== 'cpu') q.set('min', players.MIN);
    if (algorithm !== 'minimax') q.set('alg', algorithm);
    return q.toString();
  }

  const initial = readHash();

  // ---- state ----

  let setup = $state.raw(initial.setup);
  let players = $state.raw(initial.players);
  let algorithm = $state(initial.algorithm);
  let animate = $state(true);
  let speed = $state(3);
  let values = $state('hidden');
  let zoom = $state(0);

  let path = $state.raw([initial.setup.start]);
  let paused = $state(false); // the computer waits after an undo or a jump in the tree
  let search = $state.raw(null); // a search result plus kind: 'cpu' | 'replay'
  let step = $state(0);
  let playing = $state(false);
  let replayFrom = $state('here');

  let draft = $state.raw(null); // custom setup being edited
  let tool = $state('cup');
  let copied = $state(false);

  let treeW = $state(800);
  let treeH = $state(500);
  let scroller = $state(null);

  const editing = $derived(draft !== null);
  const draftErrors = $derived(draft ? validate(draft) : []);
  const game = $derived(makeGame(setup));
  const previewGame = $derived(editing && !draftErrors.length ? makeGame(draft) : null);
  const tree = $derived(editing ? (previewGame ? buildTree(previewGame, [draft.start]) : null) : buildTree(game, [setup.start]));
  const byId = $derived(new Map(tree?.nodes.map((n) => [n.id, n]) ?? []));

  const shownPath = $derived(editing ? [draft.start] : path);
  const turn = $derived(toMove(path));
  const terminal = $derived(terminalKind(game, path));
  const legal = $derived(editing ? [] : legalMoves(game, path));
  const humanTurn = $derived(!editing && !terminal && players[turn] === 'human');
  const thinking = $derived(!editing && !terminal && search?.kind === 'cpu' && search.root === nodeId(path));
  const stepMs = $derived(SPEEDS[speed]);

  // A new setup gets a zoom that fits: big trees scroll instead of squeezing.
  $effect(() => {
    if (tree) zoom = tree.leaves * 22 <= untrack(() => treeW) ? 0 : 1;
  });

  // When the tree is wider or taller than its panel, keep the current position in view.
  $effect(() => {
    const id = nodeId(shownPath);
    void [tree, zoom, treeW, treeH];
    tick().then(() => {
      const el = scroller?.querySelector(`g.node[data-id="${id}"]`);
      if (!el) return;
      const r = el.getBoundingClientRect();
      const box = scroller.getBoundingClientRect();
      scroller.scrollBy({
        left: r.left + r.width / 2 - (box.left + box.width / 2),
        top: r.top < box.top || r.bottom > box.bottom ? r.top + r.height / 2 - (box.top + box.height / 3) : 0,
        behavior: 'smooth',
      });
    });
  });

  $effect(() => {
    const hash = hashFor(setup, players, algorithm);
    history.replaceState(null, '', `${location.pathname}${location.search}#${hash}`);
  });

  // A pasted link in the same tab only changes the hash.
  function onhashchange() {
    const h = readHash();
    if (hashFor(h.setup, h.players, h.algorithm) === hashFor(setup, players, algorithm)) return;
    draft = null;
    setup = h.setup;
    players = h.players;
    algorithm = h.algorithm;
    restart();
  }

  // ---- search playback ----

  $effect(() => {
    if (!playing || !search) return;
    const id = setInterval(() => seek(step + 1), stepMs);
    return () => clearInterval(id);
  });

  function seek(k) {
    if (!search) return;
    step = Math.max(0, Math.min(search.trace.length, k));
    if (step >= search.trace.length) {
      playing = false;
      if (search.kind === 'cpu' && search.root === nodeId(path) && !paused) schedule(() => move(search.best, true), 700);
    }
  }

  function togglePlay() {
    if (!search) return;
    if (step >= search.trace.length) step = 0;
    playing = !playing;
  }

  const visitedCount = $derived.by(() => {
    if (!search) return 0;
    let n = 0;
    for (let i = 0; i < step; i++) if (search.trace[i].t === 'enter') n++;
    return n;
  });
  const searchDone = $derived(!!search && step >= search.trace.length);

  // ---- game flow ----

  let timer = null;
  function schedule(fn, ms) {
    clearTimeout(timer);
    timer = setTimeout(fn, ms);
  }
  function stopComputer() {
    clearTimeout(timer);
    if (search?.kind === 'cpu') {
      search = null;
      playing = false;
    }
  }

  function computerTurn() {
    if (editing || paused || terminalKind(game, path) || players[toMove(path)] !== 'cpu') return;
    const r = runSearch(game, path, algorithm);
    search = { ...r, kind: 'cpu', replay: false };
    if (animate) {
      step = 0;
      playing = true;
    } else {
      step = r.trace.length;
      playing = false;
      schedule(() => move(r.best, true), 600);
    }
  }

  function move(cell, byComputer = false) {
    clearTimeout(timer);
    if (!byComputer) {
      search = null;
      playing = false;
      paused = false;
    }
    path = [...path, cell];
    schedule(computerTurn, 750);
  }

  function restart() {
    stopComputer();
    search = null;
    playing = false;
    paused = false;
    path = [setup.start];
    schedule(computerTurn, 500);
  }

  function undo() {
    if (path.length < 2) return;
    stopComputer();
    let p = path.slice(0, -1);
    const anyHuman = players.MAX === 'human' || players.MIN === 'human';
    while (anyHuman && p.length > 1 && players[toMove(p)] === 'cpu') p = p.slice(0, -1);
    path = p;
    paused = players[toMove(p)] === 'cpu';
  }

  function jump(node) {
    if (editing) return;
    stopComputer();
    path = [...node.id];
    paused = !terminalKind(game, path) && players[toMove(path)] === 'cpu';
  }

  function resume() {
    paused = false;
    computerTurn();
  }

  function setPlayer(side, kind) {
    players = { ...players, [side]: kind };
    if (kind === 'human' && turn === side) stopComputer();
    if (kind === 'cpu' && turn === side && !thinking) {
      paused = false;
      schedule(computerTurn, 300);
    }
  }

  function replay(alg) {
    clearTimeout(timer);
    const from = replayFrom === 'start' ? [setup.start] : path;
    search = { ...runSearch(game, from, alg), kind: 'replay', replay: true };
    step = 0;
    playing = true;
    if (!terminal && players[turn] === 'cpu') paused = true;
  }

  function choose(variant) {
    draft = null;
    setup = VARIANTS[variant];
    restart();
  }

  // ---- custom game editor ----

  function startEditing() {
    stopComputer();
    search = null;
    playing = false;
    draft = { ...setup, name: 'custom' };
  }

  function setSize(size) {
    const [rows, cols] = size.split('x').map(Number);
    const cells = cellNames(rows, cols);
    draft = { name: 'custom', rows, cols, start: 'A', cups: [cells[cells.length - 1]], blocked: [] };
  }

  function edit(cell) {
    const d = { ...draft, cups: draft.cups.filter((c) => c !== cell), blocked: draft.blocked.filter((c) => c !== cell) };
    if (tool === 'start') d.start = cell;
    else if (cell === draft.start) return;
    else if (tool === 'cup' && !draft.cups.includes(cell)) d.cups = [...d.cups, cell].sort();
    else if (tool === 'block' && !draft.blocked.includes(cell)) d.blocked = [...d.blocked, cell].sort();
    draft = d;
  }

  function applyDraft() {
    if (draftErrors.length) return;
    setup = draft;
    draft = null;
    restart();
  }

  function cancelEdit() {
    draft = null;
    schedule(computerTurn, 300);
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(location.href);
      copied = true;
      setTimeout(() => (copied = false), 1600);
    } catch {
      prompt('Copy this link:', location.href);
    }
  }

  // ---- wording ----

  const who = (side) =>
    players.MAX === players.MIN ? side : players[side] === 'human' ? `You (${side})` : `Computer (${side})`;

  const status = $derived.by(() => {
    if (editing) return { side: null, text: 'Editing a custom game' };
    if (terminal === 'draw') return { side: null, text: 'Draw: no legal moves left' };
    if (terminal) {
      const w = terminal === 'cup_max' ? MAX : MIN;
      return { side: w, text: `${who(w)} ${players.MAX === players.MIN || players[w] === 'cpu' ? 'wins' : 'win'}!` };
    }
    if (thinking) return { side: turn, text: `${who(turn)} is thinking…` };
    if (players[turn] === 'cpu') return { side: turn, text: `${who(turn)} to move` };
    return { side: turn, text: `${players.MAX === players.MIN ? `${turn} to move` : 'Your move'}: click a highlighted cell` };
  });

  const setupText = (s) =>
    s.name === 'custom'
      ? `Custom ${s.rows}×${s.cols} · start ${s.start} · cups on ${s.cups.join(', ')}${s.blocked.length ? ` · blocked ${s.blocked.join(', ')}` : ''}`
      : `Game ${s.name} · start ${s.start} · ${s.cups.length > 1 ? 'cups' : 'cup'} on ${s.cups.join(' & ')}`;

  const ending = { cup_max: 'MAX reached a cup', cup_min: 'MIN reached a cup', draw: 'no legal moves left, a draw' };
  const ab = (e) => ('alpha' in e ? `, [α, β] = [${fmtBound(e.alpha, true)}, ${fmtBound(e.beta, false)}]` : '');

  function describe(e) {
    const n = byId.get(e.node);
    switch (e.t) {
      case 'enter':
        return n?.terminal ? `Visit ${e.node}` : `Visit ${e.node}: ${n?.player} to move${ab(e)}`;
      case 'leaf':
        return `${e.node} is a terminal position (${ending[n?.terminal]}): value ${fmtValue(e.value)}`;
      case 'update': {
        const p = byId.get(e.node)?.player;
        return `${e.node} (${p}) has seen ${e.child}; best value so far ${fmtValue(e.value)}${ab(e)}`;
      }
      case 'prune':
        return `Prune ${e.node} (${e.size} ${e.size === 1 ? 'node' : 'nodes'}): α ≥ β at ${e.parent}, so ${byId.get(e.parent)?.player === MAX ? MIN : MAX} will never let the game reach ${e.parent}`;
      case 'return':
        return `${e.node} is done: value ${fmtValue(e.value)}`;
      default:
        return '';
    }
  }

  // ---- keyboard: space plays/pauses a search, arrows step through it ----

  function onkey(e) {
    if (!search || e.target.closest?.('input, select, textarea')) return;
    if (e.key === ' ' && e.target.closest?.('button')) return; // the focused button handles space itself
    if (e.key === ' ') {
      e.preventDefault();
      togglePlay();
    } else if (e.key === 'ArrowRight') {
      playing = false;
      seek(step + 1);
    } else if (e.key === 'ArrowLeft') {
      playing = false;
      seek(step - 1);
    }
  }

  // The computer may have to open the game (it plays MAX).
  schedule(computerTurn, 600);
</script>

<svelte:window onkeydown={onkey} {onhashchange} />

{#snippet icon(name)}
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    {#if name === 'first'}<path d="M3 3h2v10H3zM13 3v10L6 8z" />
    {:else if name === 'prev'}<path d="M11 3v10L4 8z" />
    {:else if name === 'next'}<path d="M5 3v10l7-5z" />
    {:else if name === 'last'}<path d="M11 3h2v10h-2zM3 3v10l7-5z" />
    {:else if name === 'play'}<path d="M4 2.5v11l9-5.5z" />
    {:else if name === 'pause'}<path d="M3.5 3h3v10h-3zM9.5 3h3v10h-3z" />
    {/if}
  </svg>
{/snippet}

<main class="play">
  <section class="side">
    <header>
      <div class="title">
        <h1>Minimax playground</h1>
        <a class="repo" href={REPO} target="_blank" rel="noopener" title="Source code on GitHub">
          <svg viewBox="0 0 16 16" width="20" height="20" aria-hidden="true">
            <path
              d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
            />
          </svg>
          <span>GitHub</span>
        </a>
      </div>
      <p class="sub">The robot grid game from the lecture, to play at home. Same rules, same game tree as on the board.</p>
    </header>

    <div class="seg games" role="group" aria-label="Game">
      <button class:on={!editing && setup.name === '1'} onclick={() => choose(1)}>Game 1</button>
      <button class:on={!editing && setup.name === '2'} onclick={() => choose(2)}>Game 2</button>
      <button class:on={editing || setup.name === 'custom'} onclick={() => (editing ? null : startEditing())}>
        {setup.name === 'custom' && !editing ? 'Custom ✎' : 'Custom'}
      </button>
    </div>

    {#if editing}
      <div class="editor">
        <div class="row">
          <label>
            Grid
            <select value="{draft.rows}x{draft.cols}" onchange={(e) => setSize(e.currentTarget.value)}>
              {#each SIZES as s (s)}<option value={s}>{s.replace('x', ' × ')}</option>{/each}
            </select>
          </label>
          <div class="seg small" role="group" aria-label="What a click places">
            <button class:on={tool === 'start'} onclick={() => (tool = 'start')}>Start</button>
            <button class:on={tool === 'cup'} onclick={() => (tool = 'cup')}>Cup</button>
            <button class:on={tool === 'block'} onclick={() => (tool = 'block')}>Block</button>
          </div>
        </div>
        <p class="hint">
          Click cells to {tool === 'start' ? 'move the start' : tool === 'cup' ? 'add or remove cups' : 'block or unblock cells'}.
          {#if tool === 'block'}The robot can never step on a blocked cell.{/if}
        </p>
        <div class="grid-wrap" style:width="min(100%, {draft.cols * 92}px)">
          <Grid
            setup={draft}
            rows={draft.rows}
            cols={draft.cols}
            blocked={draft.blocked}
            path={[draft.start]}
            showPath={false}
            startMark
            onpick={edit}
            eyeColor={colors.MAX}
            moveMs={250}
            label="Custom game editor"
          />
        </div>
        {#if draftErrors.length}
          <ul class="errors">{#each draftErrors as e (e)}<li>{e}</li>{/each}</ul>
        {:else if tree}
          <p class="hint">
            Tree: <b>{tree.stats.nodes_total}</b> nodes, value <b>{fmtValue(tree.value)}</b> for MAX
            ({tree.value > 0 ? 'MAX wins' : tree.value < 0 ? 'MIN wins' : 'a draw'} with optimal play).
            {#if tree.stats.nodes_total > 600}Big tree: scroll and zoom to explore it.{/if}
          </p>
        {/if}
        <div class="row">
          <button class="btn primary" disabled={draftErrors.length > 0} onclick={applyDraft}>Play this game</button>
          <button class="btn" onclick={cancelEdit}>Cancel</button>
        </div>
      </div>
    {:else}
      <p class="setup">{setupText(setup)}</p>
      <div class="grid-wrap" style:width="min(100%, {setup.cols * 100}px)">
        <Grid
          {setup}
          rows={setup.rows}
          cols={setup.cols}
          blocked={setup.blocked}
          {path}
          options={humanTurn ? legal : []}
          optionColor={colors[turn]}
          eyeColor={terminal ? (terminal === 'draw' ? '#9ca3af' : colors[terminal === 'cup_max' ? MAX : MIN]) : colors[turn]}
          onpick={humanTurn ? (c) => move(c) : null}
          pickable={legal}
          moveMs={600}
        />
      </div>

      <div class="turn" style:--turn={status.side ? colors[status.side] : '#6b7280'}>
        <span class="chip">{status.side ?? '–'}</span>
        <span>{status.text}</span>
      </div>

      <div class="row">
        <button class="btn" onclick={undo} disabled={path.length < 2}>Undo</button>
        <button class="btn" onclick={restart}>Restart</button>
        {#if paused && !terminal && players[turn] === 'cpu'}
          <button class="btn primary" onclick={resume}>Let the computer move</button>
        {:else if thinking && !searchDone}
          <button class="btn" onclick={() => seek(search.trace.length)}>Skip thinking</button>
        {/if}
      </div>
    {/if}

    <fieldset>
      <legend>Players</legend>
      {#each [MAX, MIN] as side (side)}
        <div class="player">
          <span class="chip" style:--turn={colors[side]}>{side}</span>
          <span class="who">{side === MAX ? 'moves first' : 'moves second'}</span>
          <div class="seg small" role="group" aria-label="{side} is played by">
            <button class:on={players[side] === 'human'} onclick={() => setPlayer(side, 'human')}>Human</button>
            <button class:on={players[side] === 'cpu'} onclick={() => setPlayer(side, 'cpu')}>Computer</button>
          </div>
        </div>
      {/each}
      <div class="player">
        <span class="label">Computer uses</span>
        <div class="seg small" role="group" aria-label="Computer's algorithm">
          <button class:on={algorithm === 'minimax'} onclick={() => (algorithm = 'minimax')}>Minimax</button>
          <button class:on={algorithm === 'alphabeta'} onclick={() => (algorithm = 'alphabeta')}>Alpha-beta</button>
        </div>
      </div>
      <label class="check"><input type="checkbox" bind:checked={animate} /> Show the computer's search in the tree</label>
    </fieldset>

    <div class="row">
      <button class="btn" onclick={copyLink}>{copied ? 'Link copied ✓' : 'Copy link to this game'}</button>
    </div>

    <details>
      <summary>How the game works</summary>
      <ul>
        <li>Two players, MAX and MIN, take turns moving the robot one cell up, down, left or right.</li>
        <li>The robot may never return to a cell it has already visited.</li>
        <li>Whoever moves the robot onto a cup wins: +1 if MAX wins, −1 if MIN wins.</li>
        <li>If there is no legal move left, the game is a draw (0).</li>
        <li>MAX always moves first. Moves are tried in alphabetical order, as in the lecture.</li>
      </ul>
      <p>
        In the tree, each row is one player's turn. Click any node to jump to that position. Use <b>Replay search</b> to watch
        minimax or alpha-beta work through the tree step by step; with alpha-beta each node shows its [α, β] window and pruned
        branches are crossed out. Keys: space plays or pauses, ← and → step.
      </p>
    </details>
  </section>

  <section class="main">
    <div class="toolbar">
      <div class="group">
        <span class="label">Values</span>
        <div class="seg small" role="group" aria-label="Which values the tree shows">
          <button class:on={values === 'hidden'} onclick={() => (values = 'hidden')}>Hidden</button>
          <button class:on={values === 'leaves'} onclick={() => (values = 'leaves')}>Leaves</button>
          <button class:on={values === 'all'} onclick={() => (values = 'all')}>All</button>
        </div>
      </div>
      <div class="group">
        <span class="label">Replay search</span>
        <select bind:value={replayFrom} aria-label="Replay the search from" disabled={editing}>
          <option value="here">from this position</option>
          <option value="start">from the start</option>
        </select>
        <button class="btn small" onclick={() => replay('minimax')} disabled={editing || (replayFrom === 'here' && !!terminal)}>Minimax</button>
        <button class="btn small" onclick={() => replay('alphabeta')} disabled={editing || (replayFrom === 'here' && !!terminal)}>Alpha-beta</button>
      </div>
      <div class="group">
        <span class="label">Zoom</span>
        <input type="range" min="0" max={ZOOMS.length - 1} bind:value={zoom} aria-label="Zoom the tree" />
      </div>
    </div>

    <div class="tree-scroll" bind:this={scroller} bind:clientWidth={treeW} bind:clientHeight={treeH}>
      {#if tree}
        {@const px = ZOOMS[zoom]}
        <div
          class="tree-inner"
          style:width="{px ? Math.max(treeW, tree.leaves * px + 180) : treeW}px"
          style:height="{px ? Math.max(treeH, (tree.max_depth + 1) * px * 1.7) : treeH}px"
        >
          <Tree
            {tree}
            path={shownPath}
            {search}
            step={search ? step : null}
            {values}
            {colors}
            toMove={editing || terminal ? null : turn}
            onnode={editing ? null : jump}
          />
        </div>
      {:else}
        <p class="empty">Fix the setup to see its game tree.</p>
      {/if}
    </div>

    {#if search}
      <div class="player-bar" style:--accent={search.kind === 'cpu' ? colors[search.player] : 'var(--search)'}>
        <div class="controls" role="group" aria-label="Search playback">
          <button class="icon" onclick={() => ((playing = false), seek(0))} title="Back to the first step" aria-label="First step">{@render icon('first')}</button>
          <button class="icon" onclick={() => ((playing = false), seek(step - 1))} title="Step back (←)" aria-label="Step back">{@render icon('prev')}</button>
          <button class="icon big" onclick={togglePlay} title="Play or pause (space)" aria-label={playing ? 'Pause' : 'Play'}>{@render icon(playing ? 'pause' : 'play')}</button>
          <button class="icon" onclick={() => ((playing = false), seek(step + 1))} title="Step forward (→)" aria-label="Step forward">{@render icon('next')}</button>
          <button class="icon" onclick={() => seek(search.trace.length)} title="Jump to the end" aria-label="Last step">{@render icon('last')}</button>
          <label class="speed">Speed <input type="range" min="0" max={SPEEDS.length - 1} bind:value={speed} /></label>
          {#if search.kind === 'replay'}
            <button class="btn small" onclick={() => ((search = null), (playing = false))}>Close replay</button>
          {/if}
        </div>
        <p class="what">
          <b>{search.kind === 'cpu' ? `Computer (${search.player})` : 'Replay'} · {ALG[search.algorithm]} from {search.root}</b>
          · step {step} of {search.trace.length} · visited <b>{visitedCount}</b> of {search.stats.nodes_total} nodes{#if
            searchDone}{#if search.stats.nodes_pruned}, pruned {search.stats.nodes_pruned}{/if} · value
            <b>{fmtValue(search.value)}</b> · best move <b>{search.best}</b>{/if}
        </p>
        <p class="event">{step > 0 ? describe(search.trace[step - 1]) : 'Press play or step forward to start the search.'}</p>
      </div>
    {/if}

    <div class="legend">
      <span><i class="sw path"></i>played path</span>
      <span><i class="sw search"></i>search</span>
      <span><i class="sw best"></i>best move</span>
      {#if search?.algorithm === 'alphabeta'}<span><i class="sw pruned"></i>pruned</span>{/if}
      <span class="spacer"></span>
      {#if tree}
        <span>Full tree <b>{tree.stats.nodes_total}</b> nodes · minimax visits <b>{tree.stats.minimax_visited}</b> · alpha-beta visits
          <b>{tree.stats.alphabeta_visited}</b> (prunes {tree.stats.alphabeta_pruned})</span>
      {/if}
    </div>
  </section>
</main>

<style>
  .play {
    display: grid;
    grid-template-columns: minmax(320px, 400px) minmax(0, 1fr);
    height: 100vh;
    height: 100dvh;
  }
  .side {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 20px 22px 24px;
    border-right: 2px solid var(--line);
    overflow-y: auto;
  }
  .side > * {
    flex-shrink: 0;
  }
  .title {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .repo {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border: 1px solid #d4d4d8;
    border-radius: 999px;
    color: var(--ink);
    fill: var(--ink);
    font-size: 14px;
    font-weight: 600;
    text-decoration: none;
  }
  .repo:hover,
  .repo:focus-visible {
    background: var(--panel);
  }
  header h1 {
    margin: 0;
    font-size: 27px;
    white-space: nowrap;
    font-weight: 500;
    color: var(--accent);
    letter-spacing: -0.01em;
  }
  .sub,
  .setup,
  .hint {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: 14px;
    line-height: 1.4;
  }
  .setup {
    margin: 0;
  }
  .grid-wrap {
    align-self: center;
    width: 100%;
  }
  .turn {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 18px;
    font-weight: 600;
    line-height: 1.25;
    min-height: 2.5em;
  }
  .chip {
    flex: none;
    background: var(--turn);
    color: #fff;
    border-radius: 6px;
    padding: 0.2em 0.5em;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.05em;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .btn {
    border: 1px solid #d4d4d8;
    background: #fff;
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 15px;
    cursor: pointer;
  }
  .btn:hover:not(:disabled) {
    background: var(--panel);
  }
  .btn:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .btn.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  .btn.primary:hover:not(:disabled) {
    background: #312e81;
  }
  .btn.small {
    padding: 5px 10px;
    font-size: 14px;
  }
  .seg {
    display: inline-flex;
    border: 1px solid #d4d4d8;
    border-radius: 8px;
    overflow: hidden;
    background: #fff;
  }
  .seg button {
    border: 0;
    background: none;
    padding: 8px 14px;
    font-size: 15px;
    cursor: pointer;
  }
  .seg button + button {
    border-left: 1px solid #d4d4d8;
  }
  .seg button:hover {
    background: var(--panel);
  }
  .seg button.on {
    background: var(--accent);
    color: #fff;
  }
  .seg.small button {
    padding: 5px 10px;
    font-size: 14px;
  }
  .games {
    align-self: stretch;
  }
  .games button {
    flex: 1;
  }
  .editor {
    display: flex;
    flex-direction: column;
    gap: 10px;
    background: var(--panel);
    border-radius: 10px;
    padding: 12px;
  }
  .editor label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 14px;
  }
  select {
    font: inherit;
    font-size: 14px;
    padding: 4px 6px;
    border: 1px solid #d4d4d8;
    border-radius: 6px;
    background: #fff;
    color: inherit;
  }
  .errors {
    margin: 0;
    padding-left: 1.2em;
    color: var(--bad);
    font-size: 14px;
  }
  fieldset {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 8px 12px 12px;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  legend {
    font-size: 13px;
    font-weight: 600;
    color: var(--muted);
    padding: 0 4px;
  }
  .player {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .player .who {
    flex: 1;
    font-size: 14px;
    color: var(--muted);
  }
  .player .label {
    flex: 1;
  }
  .label {
    font-size: 14px;
    color: var(--muted);
  }
  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
  }
  details {
    font-size: 14px;
    line-height: 1.45;
    color: #374151;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
  }
  details ul {
    padding-left: 1.2em;
  }
  .main {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    padding: 14px 16px 12px;
    gap: 10px;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 22px;
    align-items: center;
  }
  .group {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .tree-scroll {
    flex: 1;
    min-height: 0;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 10px;
  }
  .tree-inner {
    min-width: 100%;
    min-height: 100%;
  }
  .empty {
    color: var(--muted);
    padding: 20px;
  }
  .player-bar {
    border-left: 4px solid var(--accent);
    background: var(--panel);
    border-radius: 8px;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .icon {
    display: inline-grid;
    place-items: center;
    width: 34px;
    height: 30px;
    border: 1px solid #d4d4d8;
    border-radius: 6px;
    background: #fff;
    cursor: pointer;
    fill: #374151;
  }
  .icon svg {
    display: block;
  }
  .icon.big {
    width: 44px;
    background: var(--accent);
    border-color: var(--accent);
    fill: #fff;
  }
  .speed {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 14px;
    color: var(--muted);
    margin: 0 8px;
  }
  .what,
  .event {
    margin: 0;
    font-size: 14px;
  }
  .event {
    color: var(--search);
    font-weight: 600;
    min-height: 1.3em;
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    align-items: center;
    font-size: 13px;
    color: var(--muted);
  }
  .legend .spacer {
    flex: 1;
  }
  .legend b {
    color: var(--ink);
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
  .sw.best {
    background: #bbf7d0;
    border-color: #15803d;
  }
  .sw.pruned {
    background: #d4d4d8;
    border-style: dashed;
    border-color: #9ca3af;
  }
  @media (max-width: 370px) {
    .repo span {
      display: none;
    }
    header h1 {
      font-size: 24px;
    }
  }
  @media (max-width: 900px) {
    .play {
      display: block;
      height: auto;
    }
    .side {
      border-right: 0;
      border-bottom: 2px solid var(--line);
      padding: 16px;
      overflow: visible;
    }
    .grid-wrap {
      max-width: 340px;
    }
    .main {
      height: 85vh;
      height: 85dvh;
      min-height: 480px;
      padding: 12px 16px;
    }
  }
</style>
