<script>
  // Game tree laid out like the slides: MAX/MIN per row, children in alphabetical order.
  // Node ids are paths ("ABE"), so the played path and any search share the same ids.
  import { hierarchy, tree as d3tree } from 'd3-hierarchy';
  import { fmtBound, fmtValue } from './labels.js';

  let {
    tree,
    path = [],
    phase = null,
    round = null,
    search = null,
    now = 0,
    values = 'hidden',
    colors = { MAX: '#2563eb', MIN: '#ea580c' },
    toMove = null,
    step = null, // show the first `step` trace events instead of following the clock
    onnode = null, // click handler for nodes, called with the node
  } = $props();

  let width = $state(1000);
  let height = $state(700);

  const pathId = $derived(path.join(''));

  const layout = $derived.by(() => {
    if (!tree?.nodes?.length) return null;
    const byId = new Map(tree.nodes.map((n) => [n.id, n]));
    const build = (id) => {
      const n = byId.get(id);
      return { ...n, kids: n.children.map(build) };
    };
    const root = hierarchy(build(tree.root), (d) => d.kids);
    const padL = 70;
    const padR = 110;
    const padT = 36;
    const padB = 52;
    const innerW = Math.max(100, width - padL - padR);
    const innerH = Math.max(100, height - padT - padB);
    d3tree()
      .size([innerW, innerH])
      .separation((a, b) => (a.parent === b.parent ? 1 : 1.3))(root);
    const levels = root.height;
    const levelH = levels > 0 ? innerH / levels : innerH;
    const rows = new Map();
    for (const n of root.descendants()) {
      if (!rows.has(n.depth)) rows.set(n.depth, []);
      rows.get(n.depth).push(n.x);
    }
    let minGap = innerW;
    for (const xs of rows.values()) {
      xs.sort((a, b) => a - b);
      for (let i = 1; i < xs.length; i++) minGap = Math.min(minGap, xs[i] - xs[i - 1]);
    }
    const size = Math.max(16, Math.min(54, levelH * 0.5, minGap * 0.74));
    const nodes = root.descendants().map((n) => ({ ...n.data, kids: undefined, x: n.x + padL, y: n.y + padT }));
    const pos = new Map(nodes.map((n) => [n.id, n]));
    const links = root.links().map(({ source, target }) => ({
      id: target.data.id,
      s: pos.get(source.data.id),
      t: pos.get(target.data.id),
    }));
    const depth0 = root.data.depth;
    const rowLabels = [...rows.keys()].map((d) => ({
      y: padT + d * levelH,
      player: (depth0 + d) % 2 === 0 ? 'MAX' : 'MIN',
    }));
    return { nodes, links, pos, size, rowLabels, labelX: width - padR + 30 };
  });

  // Replay the search trace up to "now".
  const anim = $derived.by(() => {
    if (!search?.trace?.length) return null;
    const tr = search.trace;
    const elapsed = Math.max(0, now - search.started_at);
    const upto =
      step !== null ? Math.min(tr.length, step) : Math.min(tr.length, Math.floor(elapsed / Math.max(1, search.step_ms)) + 1);
    const st = { vals: new Map(), ab: new Map(), visited: new Set(), pruned: [], stack: [], last: null };
    for (let i = 0; i < upto; i++) {
      const e = tr[i];
      if (e.t === 'enter') {
        st.visited.add(e.node);
        st.stack.push(e.node);
        if ('alpha' in e) st.ab.set(e.node, [e.alpha, e.beta]);
      } else if (e.t === 'leaf') {
        st.vals.set(e.node, e.value);
        st.stack.pop();
      } else if (e.t === 'update') {
        st.vals.set(e.node, e.value);
        if ('alpha' in e) st.ab.set(e.node, [e.alpha, e.beta]);
      } else if (e.t === 'return') {
        st.vals.set(e.node, e.value);
        st.stack.pop();
      } else if (e.t === 'prune') {
        st.pruned.push(e.node);
      }
      st.last = e;
    }
    st.done = upto >= tr.length;
    st.onStack = new Set(st.stack);
    st.active = st.done ? null : st.stack[st.stack.length - 1] ?? null;
    st.fresh = st.done ? null : st.last?.node;
    return st;
  });

  const isPruned = (id) => !!anim && anim.pruned.some((p) => id.startsWith(p));
  const prunedRoot = (id) => !!anim && anim.pruned.includes(id);
  const inSearch = (id) => !!search && id.startsWith(search.root);

  function shownValue(n) {
    if (anim?.vals.has(n.id)) return anim.vals.get(n.id);
    if (values === 'all') return n.value;
    if (values === 'leaves' && n.terminal) return n.value;
    return null;
  }

  const votingChildren = $derived.by(() => {
    if (phase !== 'VOTING' || !round || round.ply !== path.length - 1) return new Map();
    const total = round.counts ? Object.values(round.counts).reduce((a, b) => a + b, 0) : 0;
    return new Map(
      round.options.map((cell) => [
        pathId + cell,
        { cell, count: round.counts?.[cell] ?? null, share: total ? (round.counts[cell] || 0) / total : 0 },
      ]),
    );
  });
</script>

<div class="tree" bind:clientWidth={width} bind:clientHeight={height}>
  {#if layout}
    {@const s = layout.size}
    {@const fs = Math.max(11, s * 0.36)}
    <svg {width} {height} role="img" aria-label="Game tree">
      {#each layout.rowLabels as row (row.y)}
        <line x1="20" x2={layout.labelX - 18} y1={row.y} y2={row.y} class="row-rule" />
        <text x={layout.labelX} y={row.y + 7} class="row-label" fill={colors[row.player]}>{row.player}</text>
      {/each}

      {#each layout.links as l (l.id)}
        {@const onPath = pathId.startsWith(l.id)}
        {@const reachable = l.id.startsWith(pathId) || onPath}
        {@const searched = anim?.visited.has(l.id)}
        <line
          x1={l.s.x}
          y1={l.s.y + s / 2}
          x2={l.t.x}
          y2={l.t.y - s / 2}
          class="edge"
          class:path={onPath}
          class:off={!reachable}
          class:searched
          class:stack={anim?.onStack.has(l.id)}
          class:pruned={isPruned(l.id)}
        />
        {#if prunedRoot(l.id)}
          {@const mx = (l.s.x + l.t.x) / 2}
          {@const my = (l.s.y + s / 2 + l.t.y - s / 2) / 2}
          <g class="cross" transform="translate({mx} {my})">
            <line x1={-s * 0.22} y1={-s * 0.22} x2={s * 0.22} y2={s * 0.22} />
            <line x1={-s * 0.22} y1={s * 0.22} x2={s * 0.22} y2={-s * 0.22} />
          </g>
        {/if}
      {/each}

      {#each layout.nodes as n (n.id)}
        {@const onPath = pathId.startsWith(n.id)}
        {@const current = n.id === pathId}
        {@const reachable = n.id.startsWith(pathId) || onPath}
        {@const v = shownValue(n)}
        {@const vote = votingChildren.get(n.id)}
        {@const pruned = isPruned(n.id)}
        {@const waiting = anim && !anim.done && inSearch(n.id) && !anim.visited.has(n.id) && !pruned}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (role is "button" exactly when nodes are clickable) -->
        <g
          class="node"
          data-id={n.id}
          class:pick={!!onnode}
          role={onnode ? 'button' : undefined}
          tabindex={onnode ? 0 : undefined}
          aria-label={onnode ? `Position ${n.id}` : undefined}
          onclick={() => onnode?.(n)}
          onkeydown={(e) => onnode && (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onnode(n))}
          class:off={!reachable}
          class:path={onPath}
          class:pruned
          class:waiting
          class:visited={anim?.visited.has(n.id)}
          class:active={anim?.active === n.id}
          class:best={anim?.done && search?.best_node === n.id && !search.replay}
          transform="translate({n.x} {n.y})"
        >
          <title>{n.id}{n.terminal ? ` · ${n.terminal.replace('_', ' ')}` : ''}</title>
          {#if current && toMove}
            <rect x={-s / 2 - 7} y={-s / 2 - 7} width={s + 14} height={s + 14} rx="6" class="ring" stroke={colors[toMove]} />
          {/if}
          {#if vote && !round.counts}
            <rect x={-s / 2 - 6} y={-s / 2 - 6} width={s + 12} height={s + 12} rx="5" class="vote-ring" stroke={colors[round.player]} />
          {/if}
          <rect x={-s / 2} y={-s / 2} width={s} height={s} rx="2" class="box" />
          <text y={s * 0.2} class="cell" style:font-size="{s * 0.56}px" text-anchor="middle">{n.cell}</text>
          {#if v !== null && v !== undefined}
            <text
              x={s / 2 + 4}
              y={s / 2 + fs * 0.95}
              class="value"
              class:fresh={anim?.fresh === n.id}
              style:font-size="{fs}px">{fmtValue(v)}</text
            >
          {/if}
          {#if search?.algorithm === 'alphabeta' && anim?.ab.has(n.id) && !n.terminal}
            {@const [a, b] = anim.ab.get(n.id)}
            <text x={-s / 2 - 5} y={-s / 2 + fs * 0.25} class="ab" text-anchor="end" style:font-size="{Math.max(13, fs * 0.95)}px"
              >[{fmtBound(a, true)}, {fmtBound(b, false)}]</text
            >
          {/if}
          {#if vote && round.counts}
            {@const w = Math.max(s * 1.6, 60)}
            <g transform="translate({-w / 2} {s / 2 + 6})">
              <rect width={w} height={fs * 1.25} rx="3" class="bar-bg" />
              <rect width={w * vote.share} height={fs * 1.25} rx="3" fill={colors[round.player]} class:winner={round.winner === vote.cell} />
              <text x={w / 2} y={fs * 0.98} text-anchor="middle" class="bar-label" style:font-size="{fs * 0.9}px">{vote.count}</text>
            </g>
          {/if}
        </g>
      {/each}
    </svg>
  {/if}
</div>

<style>
  .tree {
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: hidden;
  }
  svg {
    display: block;
    font-family: var(--font, system-ui, sans-serif);
  }
  .row-rule {
    stroke: var(--rule, #e5e7eb);
    stroke-width: 1;
    stroke-dasharray: 3 6;
  }
  .row-label {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .edge {
    stroke: #374151;
    stroke-width: 1.6;
    transition: stroke 0.2s, stroke-width 0.2s, opacity 0.2s;
  }
  .edge.off {
    opacity: 0.2;
  }
  .edge.searched {
    stroke: var(--search, #7c3aed);
    stroke-width: 2.6;
  }
  .edge.stack {
    stroke-width: 4.5;
  }
  .edge.path {
    stroke: var(--path-edge, #111827);
    stroke-width: 5;
    opacity: 1;
  }
  .edge.pruned {
    stroke: #9ca3af;
    stroke-dasharray: 5 5;
    stroke-width: 1.4;
    opacity: 0.6;
  }
  .cross line {
    stroke: #dc2626;
    stroke-width: 4;
    stroke-linecap: round;
  }
  .node .box {
    fill: #d4d4d8;
    stroke: #3f3f46;
    stroke-width: 1.6;
    transition: fill 0.2s, stroke 0.2s;
  }
  .node .cell {
    fill: #3f3f46;
    font-weight: 400;
  }
  .node.pick {
    cursor: pointer;
  }
  .node.pick:hover .box,
  .node.pick:focus-visible .box {
    stroke: var(--accent, #3730a3);
    stroke-width: 3.5;
  }
  .node:focus-visible {
    outline: none;
  }
  .node.off {
    opacity: 0.22;
  }
  .node.path .box {
    fill: var(--path, #fde68a);
    stroke: #111827;
    stroke-width: 3;
  }
  .node.path .cell {
    fill: #111827;
    font-weight: 600;
  }
  .node.waiting {
    opacity: 0.45;
  }
  .node.visited .box {
    stroke: var(--search, #7c3aed);
    stroke-width: 2.6;
  }
  .node.active .box {
    fill: #ddd6fe;
    stroke: var(--search, #7c3aed);
    stroke-width: 4;
  }
  .node.best .box {
    fill: #bbf7d0;
    stroke: #15803d;
    stroke-width: 4;
  }
  .node.pruned {
    opacity: 0.3;
  }
  .node.pruned .box {
    stroke-dasharray: 4 3;
  }
  .ring {
    fill: none;
    stroke-width: 4;
  }
  .vote-ring {
    fill: none;
    stroke-width: 4;
    animation: pulse 1.4s ease-in-out infinite;
  }
  .value {
    font-weight: 700;
    fill: #111827;
  }
  .value.fresh {
    fill: #dc2626;
  }
  .ab {
    fill: #475569;
    font-weight: 600;
  }
  .bar-bg {
    fill: #e5e7eb;
  }
  .bar-label {
    font-weight: 700;
    fill: #111827;
    paint-order: stroke;
    stroke: #fff;
    stroke-width: 3px;
  }
  rect.winner {
    stroke: #111827;
    stroke-width: 2;
  }
  @keyframes pulse {
    50% {
      stroke-opacity: 0.25;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .vote-ring {
      animation: none;
    }
  }
</style>
