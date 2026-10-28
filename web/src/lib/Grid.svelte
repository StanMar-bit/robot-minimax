<script>
  // Grid mirror: visited cells, cups, the path so far and the robot.
  // 3×3 on the board; the playground also uses other sizes and blocked cells.
  let {
    setup = { cups: [], start: 'A' },
    rows = 3,
    cols = 3,
    blocked = [],
    startMark = false,
    path = [],
    robotCell = null,
    options = [],
    optionColor = '#2563eb',
    eyeColor = '#2563eb',
    moveMs = 1200,
    winner = null,
    counts = null,
    marked = [],
    markColor = '#16a34a',
    onpick = null,
    pickable = null,
    showPath = true,
    label = 'Game grid',
  } = $props();

  const CELLS = $derived(Array.from({ length: rows * cols }, (_, i) => String.fromCharCode(65 + i)));
  const pos = (cell) => {
    const i = CELLS.indexOf(cell);
    return { x: (i % cols) * 100, y: Math.floor(i / cols) * 100 };
  };
  const center = (cell) => {
    const p = pos(cell);
    return { x: p.x + 50, y: p.y + 50 };
  };

  const visited = $derived(new Set(path));
  const current = $derived(robotCell ?? path[path.length - 1]);
  const robot = $derived(current ? center(current) : null);
  const robotOnCup = $derived(current && setup.cups.includes(current));
  const pathPoints = $derived(path.map((c) => center(c)).map((p) => `${p.x},${p.y}`).join(' '));
  const canPick = (cell) => onpick && (!pickable || pickable.includes(cell));

  function key(e, cell) {
    if ((e.key === 'Enter' || e.key === ' ') && canPick(cell)) {
      e.preventDefault();
      onpick(cell);
    }
  }
</script>

<svg viewBox="-4 -4 {cols * 100 + 8} {rows * 100 + 8}" role="img" aria-label={label} class="grid">
  {#each CELLS as cell (cell)}
    {@const p = pos(cell)}
    {@const isOption = options.includes(cell)}
    {@const isMarked = marked.includes(cell)}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (role is "button" exactly when the cell is clickable) -->
    <g
      class="cell"
      class:pick={canPick(cell)}
      role={canPick(cell) ? 'button' : undefined}
      tabindex={canPick(cell) ? 0 : undefined}
      aria-label={canPick(cell) ? `Cell ${cell}` : undefined}
      onclick={() => canPick(cell) && onpick(cell)}
      onkeydown={(e) => key(e, cell)}
    >
      <rect
        x={p.x}
        y={p.y}
        width="100"
        height="100"
        class="bg"
        class:visited={visited.has(cell) && cell !== current}
        class:blocked={blocked.includes(cell)}
        fill={isMarked ? markColor : isOption ? optionColor : undefined}
        fill-opacity={isMarked ? 0.25 : isOption ? (winner === cell ? 0.45 : 0.16) : undefined}
      />
      {#if isOption}
        <rect
          x={p.x + 4}
          y={p.y + 4}
          width="92"
          height="92"
          fill="none"
          stroke={optionColor}
          stroke-width={winner === cell ? 8 : 5}
          rx="3"
          class:pulse={!winner}
        />
      {/if}
      {#if blocked.includes(cell)}
        <path d="M{p.x + 22},{p.y + 22} L{p.x + 78},{p.y + 78} M{p.x + 78},{p.y + 22} L{p.x + 22},{p.y + 78}" class="block-x" />
      {/if}
      {#if startMark && cell === setup.start}
        <text x={p.x + 93} y={p.y + 28} class="start" text-anchor="end">start</text>
      {/if}
      <text x={p.x + 7} y={p.y + 28} class="letter" class:on-dark={blocked.includes(cell)}>{cell}</text>
      {#if counts && counts[cell] !== undefined}
        <text x={p.x + 93} y={p.y + 92} class="count" text-anchor="end">{counts[cell]}</text>
      {/if}
    </g>
  {/each}

  {#each { length: cols + 1 } as _, i (i)}
    <line x1={i * 100} y1="0" x2={i * 100} y2={rows * 100} class="rule" />
  {/each}
  {#each { length: rows + 1 } as _, i (i)}
    <line x1="0" y1={i * 100} x2={cols * 100} y2={i * 100} class="rule" />
  {/each}

  {#if showPath && path.length > 1}
    <polyline points={pathPoints} class="trail" />
  {/if}

  {#each setup.cups as cup (cup)}
    {@const c = center(cup)}
    <g
      transform="translate({c.x + (robotOnCup && cup === current ? -24 : 10)} {c.y + 14}) scale(1.05)"
      class="cup"
      class:won={robotOnCup && cup === current}
    >
      <path d="M-13,-16 H13 V-6 A13,13 0 0 1 -13,-6 Z" />
      <path d="M-13,-13 h-5 a5,6 0 0 0 5,9" fill="none" />
      <path d="M13,-13 h5 a5,6 0 0 1 -5,9" fill="none" />
      <rect x="-2.5" y="6" width="5" height="7" />
      <rect x="-9" y="13" width="18" height="4" rx="1" />
    </g>
  {/each}

  {#if robot}
    <g
      class="robot"
      style:transform="translate({robot.x + (robotOnCup ? 20 : 10)}px, {robot.y + 12}px)"
      style:transition="transform {moveMs}ms ease-in-out"
    >
      <line x1="0" y1="-25" x2="0" y2="-17" />
      <circle cx="0" cy="-27" r="3.5" class="ink" />
      <rect x="-17" y="-17" width="34" height="24" rx="6" class="shell" />
      <circle cx="-7" cy="-5" r="4.6" fill={eyeColor} />
      <circle cx="7" cy="-5" r="4.6" fill={eyeColor} />
      <rect x="-12" y="9" width="24" height="15" rx="4" class="shell" />
    </g>
  {/if}
</svg>

<style>
  .grid {
    width: 100%;
    height: auto;
    display: block;
    user-select: none;
  }
  .bg {
    fill: var(--cell, #fff);
  }
  .bg.visited {
    fill: var(--visited, #d4d4d8);
  }
  .bg.blocked {
    fill: var(--blocked, #52525b);
  }
  .letter.on-dark {
    fill: #f4f4f5;
  }
  .block-x {
    stroke: #a1a1aa;
    stroke-width: 6;
    stroke-linecap: round;
  }
  .start {
    font: 600 15px/1 var(--font, system-ui, sans-serif);
    fill: var(--muted, #6b7280);
  }
  .rule {
    stroke: var(--ink, #111);
    stroke-width: 3;
  }
  .letter {
    font: 600 26px/1 var(--font, system-ui, sans-serif);
    fill: var(--ink, #111);
  }
  .count {
    font: 700 22px/1 var(--font, system-ui, sans-serif);
    fill: var(--ink, #111);
  }
  .trail {
    fill: none;
    stroke: var(--trail, #475569);
    stroke-width: 4;
    stroke-dasharray: 2 9;
    stroke-linecap: round;
    stroke-linejoin: round;
    opacity: 0.8;
  }
  .cup path,
  .cup rect {
    fill: var(--cup, #fbbf24);
    stroke: var(--ink, #111);
    stroke-width: 2.2;
  }
  .cup path[fill='none'] {
    fill: none;
    stroke-width: 3;
  }
  .cup.won path,
  .cup.won rect {
    fill: #facc15;
  }
  .robot line {
    stroke: #334155;
    stroke-width: 3;
  }
  .robot .ink {
    fill: #334155;
  }
  .robot .shell {
    fill: #f8fafc;
    stroke: #334155;
    stroke-width: 3;
  }
  .cell.pick {
    cursor: pointer;
  }
  .cell.pick:hover .bg,
  .cell.pick:focus-visible .bg {
    fill: var(--hover, #e0e7ff);
  }
  .cell:focus-visible {
    outline: none;
  }
  .pulse {
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    50% {
      stroke-opacity: 0.35;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .pulse {
      animation: none;
    }
    .robot {
      transition: none !important;
    }
  }
</style>
