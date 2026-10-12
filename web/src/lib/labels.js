// Shared wording, so the board, phones and admin describe each moment the same way.

const OWNER = { class: 'Class', robot: 'Robot', left: 'Left side', right: 'Right side' };

export function who(owner, player) {
  return `${OWNER[owner] ?? owner} (${player})`;
}

export function setupLabel(setup) {
  if (!setup) return '';
  const cups = setup.cups.join(' & ');
  if (setup.name === '1') return `Game 1 · cups on ${cups}`;
  if (setup.name === '2') return `Game 2 · cup on ${cups}`;
  return `Custom · start ${setup.start} · cups on ${cups}`;
}

export function resultLine(state) {
  const r = state?.result;
  if (!r) return '';
  if (!r.winner) return 'Draw: no legal moves left';
  return `${who(r.owner, r.winner)} wins`;
}

/** One line for "whose turn is it, and what is happening". */
export function banner(state) {
  if (!state) return 'Connecting…';
  const p = state.to_move;
  switch (state.phase) {
    case 'LOBBY':
      return state.game?.restored ? 'Game restored. Waiting to resume…' : 'Waiting for the game to start';
    case 'VOTING':
      if (state.round?.tie) return `Tie between ${state.round.tie.join(' and ')}. The TA decides.`;
      if (state.round?.closed) return `${who(state.round.team, state.round.player)} chose ${state.round.winner}`;
      return `${who(state.owner, p)} to move`;
    case 'THINKING':
      return `${who('robot', p)} thinking…`;
    case 'ROBOT_MOVING': {
      const m = state.moving;
      if (!m) return 'Moving…';
      if (m.failed) return `Robot stuck on the way to ${m.to}. TA is fixing it…`;
      return `${who(m.owner, m.player)} moves to ${m.to}`;
    }
    case 'GAME_OVER':
      return resultLine(state);
    default:
      return state.phase;
  }
}

export function fmtValue(v) {
  if (v === null || v === undefined) return '';
  return v > 0 ? `+${v}` : `${v}`;
}

export function fmtBound(b, isAlpha) {
  if (b === null || b === undefined) return isAlpha ? '−∞' : '+∞';
  return b > 0 ? `+${b}` : b < 0 ? `−${-b}` : '0';
}
