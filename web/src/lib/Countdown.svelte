<script>
  // Seconds left in a voting round, as a number inside a shrinking ring.
  let { round, now, total = 20, color = '#2563eb', size = 120 } = $props();

  const leftMs = $derived.by(() => {
    if (!round) return 0;
    if (round.ends_at) return Math.max(0, round.ends_at - now);
    if (round.remaining_ms !== null && round.remaining_ms !== undefined) return round.remaining_ms;
    return 0;
  });
  const secs = $derived(Math.ceil(leftMs / 1000));
  const frac = $derived(Math.max(0, Math.min(1, leftMs / Math.max(1, total * 1000))));
  const R = 44;
  const C = 2 * Math.PI * R;
</script>

<div class="countdown" style:width="{size}px" style:height="{size}px" aria-live="off">
  <svg viewBox="0 0 100 100" width={size} height={size} aria-hidden="true">
    <circle cx="50" cy="50" r={R} class="track" />
    <circle
      cx="50"
      cy="50"
      r={R}
      class="fill"
      stroke={secs <= 5 && !round?.paused ? '#dc2626' : color}
      stroke-dasharray={C}
      stroke-dashoffset={C * (1 - frac)}
    />
  </svg>
  <span class="num" class:paused={round?.paused} style:font-size="{size * 0.36}px">{secs}</span>
  {#if round?.paused}<span class="tag">paused</span>{/if}
</div>

<style>
  .countdown {
    position: relative;
    display: inline-grid;
    place-items: center;
    flex: none;
  }
  svg {
    position: absolute;
    inset: 0;
    transform: rotate(-90deg);
  }
  .track {
    fill: none;
    stroke: var(--line, #e4e4e7);
    stroke-width: 8;
  }
  .fill {
    fill: none;
    stroke-width: 8;
    stroke-linecap: round;
    transition: stroke-dashoffset 0.25s linear;
  }
  .num {
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .num.paused {
    opacity: 0.5;
  }
  .tag {
    position: absolute;
    bottom: 18%;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted, #6b7280);
  }
</style>
