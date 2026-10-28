// Browser port of server/app/game/engine.py and search.py, for the offline playground.
//
// Same rules, node ids and trace events as the server, so the shared Tree component
// draws both. It also allows other grid sizes and blocked cells:
//   - cells are named A, B, C, ... row by row;
//   - a blocked cell can never be entered;
//   - a position is the path from the start cell, and its node id is that path as a string.

export const MAX = 'MAX';
export const MIN = 'MIN';
export const SIZES = ['3x3', '3x4', '4x3', '4x4'];

export const VARIANTS = {
  1: { name: '1', rows: 3, cols: 3, start: 'A', cups: ['C', 'H'], blocked: [] },
  2: { name: '2', rows: 3, cols: 3, start: 'A', cups: ['I'], blocked: [] },
};

export const other = (player) => (player === MAX ? MIN : MAX);

export function cellNames(rows, cols) {
  return Array.from({ length: rows * cols }, (_, i) => String.fromCharCode(65 + i));
}

/** Problems with a setup, as sentences; empty when it can be played. */
export function validate(setup) {
  const cells = cellNames(setup.rows, setup.cols);
  const errors = [];
  if (!cells.includes(setup.start)) errors.push('Pick a start cell.');
  if (!setup.cups.length) errors.push('Place at least one cup.');
  if (setup.cups.includes(setup.start)) errors.push('The start cell cannot hold a cup.');
  if (setup.blocked.includes(setup.start)) errors.push('The start cell cannot be blocked.');
  if (setup.cups.some((c) => setup.blocked.includes(c))) errors.push('A cup cannot be on a blocked cell.');
  if (setup.cups.some((c) => !cells.includes(c)) || setup.blocked.some((c) => !cells.includes(c)))
    errors.push('Some cells are outside the grid.');
  return errors;
}

/** A game: the setup plus precomputed neighbours (alphabetical, blocked cells left out). */
export function makeGame(setup) {
  const { rows, cols } = setup;
  const cells = cellNames(rows, cols);
  const blocked = new Set(setup.blocked);
  const neighbours = new Map();
  cells.forEach((cell, i) => {
    const r = Math.floor(i / cols);
    const c = i % cols;
    const out = [];
    for (const [dr, dc] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) {
      const rr = r + dr;
      const cc = c + dc;
      if (rr >= 0 && rr < rows && cc >= 0 && cc < cols && !blocked.has(cells[rr * cols + cc])) out.push(cells[rr * cols + cc]);
    }
    neighbours.set(cell, out.sort());
  });
  return { ...setup, cells, cupSet: new Set(setup.cups), neighbours };
}

// Positions are paths: ['A', 'D', 'E'].
export const toMove = (path) => ((path.length - 1) % 2 === 0 ? MAX : MIN);
export const lastMover = (path) => (path.length > 1 ? other(toMove(path)) : null);
export const nodeId = (path) => path.join('');

export const onCup = (game, path) => path.length > 1 && game.cupSet.has(path[path.length - 1]);

export function legalMoves(game, path) {
  if (onCup(game, path)) return [];
  return game.neighbours.get(path[path.length - 1]).filter((n) => !path.includes(n));
}

/** 'cup_max' / 'cup_min' when a player reached a cup, 'draw' when stuck, else null. */
export function terminalKind(game, path) {
  if (onCup(game, path)) return lastMover(path) === MAX ? 'cup_max' : 'cup_min';
  if (!legalMoves(game, path).length) return 'draw';
  return null;
}

/** Outcome for MAX at a terminal position: +1 win, -1 loss, 0 draw. */
export function utility(game, path) {
  const kind = terminalKind(game, path);
  if (kind === 'cup_max') return 1;
  if (kind === 'cup_min') return -1;
  if (kind === 'draw') return 0;
  throw new Error('utility() called on a non-terminal position');
}

export function subtreeSize(game, path) {
  const moves = legalMoves(game, path);
  let n = 1;
  for (const m of moves) n += subtreeSize(game, [...path, m]);
  return n;
}

/** The full game tree below `path`, with exact minimax values on every node. */
export function buildTree(game, path) {
  const nodes = [];
  let maxDepth = path.length - 1;
  const rec = (p, parent) => {
    maxDepth = Math.max(maxDepth, p.length - 1);
    const node = {
      id: nodeId(p),
      cell: p[p.length - 1],
      depth: p.length - 1,
      player: toMove(p),
      terminal: terminalKind(game, p),
      value: null,
      parent,
      children: [],
    };
    nodes.push(node);
    if (node.terminal) {
      node.value = utility(game, p);
    } else {
      const values = legalMoves(game, p).map((m) => {
        const child = [...p, m];
        node.children.push(nodeId(child));
        return rec(child, node.id);
      });
      node.value = node.player === MAX ? Math.max(...values) : Math.min(...values);
    }
    return node.value;
  };
  const value = rec(path, null);
  const ab = alphabeta(game, path, { trace: false });
  return {
    root: nodeId(path),
    value,
    nodes,
    max_depth: maxDepth,
    leaves: nodes.filter((n) => n.terminal).length,
    stats: {
      nodes_total: nodes.length,
      minimax_visited: nodes.length,
      alphabeta_visited: ab.stats.nodes_visited,
      alphabeta_pruned: ab.stats.nodes_pruned,
    },
  };
}

const bound = (x) => (x === Infinity || x === -Infinity ? null : x);

function result(algorithm, game, path, value, childValues, ctx) {
  const best = pick(game, path, childValues);
  return {
    algorithm,
    root: nodeId(path),
    player: toMove(path),
    value,
    best,
    best_node: best ? nodeId(path) + best : null,
    child_values: childValues,
    trace: ctx.trace,
    stats: { nodes_visited: ctx.visited, nodes_pruned: ctx.pruned, nodes_total: subtreeSize(game, path) },
  };
}

function context(record) {
  const ctx = { trace: [], visited: 0, pruned: 0 };
  ctx.emit = (ev) => record && ctx.trace.push(ev);
  return ctx;
}

export function minimax(game, path, { trace = true } = {}) {
  const ctx = context(trace);
  const childValues = {};
  const rec = (p) => {
    const nid = nodeId(p);
    ctx.visited++;
    ctx.emit({ t: 'enter', node: nid });
    if (terminalKind(game, p)) {
      const v = utility(game, p);
      ctx.emit({ t: 'leaf', node: nid, value: v });
      return v;
    }
    const maximizing = toMove(p) === MAX;
    let best = null;
    for (const m of legalMoves(game, p)) {
      const child = [...p, m];
      const cv = rec(child);
      if (p === path) childValues[m] = cv;
      if (best === null || (maximizing ? cv > best : cv < best)) best = cv;
      ctx.emit({ t: 'update', node: nid, value: best, child: nodeId(child) });
    }
    ctx.emit({ t: 'return', node: nid, value: best });
    return best;
  };
  const value = rec(path);
  return result('minimax', game, path, value, childValues, ctx);
}

export function alphabeta(game, path, { trace = true } = {}) {
  const ctx = context(trace);
  const childValues = {};
  const rec = (p, alpha, beta) => {
    const nid = nodeId(p);
    ctx.visited++;
    ctx.emit({ t: 'enter', node: nid, alpha: bound(alpha), beta: bound(beta) });
    if (terminalKind(game, p)) {
      const v = utility(game, p);
      ctx.emit({ t: 'leaf', node: nid, value: v });
      return v;
    }
    const maximizing = toMove(p) === MAX;
    let v = maximizing ? -Infinity : Infinity;
    const moves = legalMoves(game, p);
    for (let i = 0; i < moves.length; i++) {
      const child = [...p, moves[i]];
      const cv = rec(child, alpha, beta);
      if (p === path) childValues[moves[i]] = cv;
      if (maximizing) {
        v = Math.max(v, cv);
        alpha = Math.max(alpha, v);
      } else {
        v = Math.min(v, cv);
        beta = Math.min(beta, v);
      }
      ctx.emit({ t: 'update', node: nid, value: v, child: nodeId(child), alpha: bound(alpha), beta: bound(beta) });
      if (alpha >= beta) {
        for (const rest of moves.slice(i + 1)) {
          const pruned = [...p, rest];
          const size = subtreeSize(game, pruned);
          ctx.pruned += size;
          if (p === path) childValues[rest] = null;
          ctx.emit({ t: 'prune', node: nodeId(pruned), parent: nid, size, reason: 'alpha>=beta' });
        }
        break;
      }
    }
    ctx.emit({ t: 'return', node: nid, value: v });
    return v;
  };
  const value = rec(path, -Infinity, Infinity);
  return result('alphabeta', game, path, value, childValues, ctx);
}

/**
 * Best move for the player to move; ties go to the first move in alphabetical order.
 * Only a strictly better value replaces the choice, so alpha-beta (whose later children
 * may return a bound equal to the best value) picks the same move as minimax.
 */
function pick(game, path, childValues) {
  let best = null;
  let bestV = null;
  const maximizing = toMove(path) === MAX;
  for (const m of legalMoves(game, path)) {
    const v = childValues[m];
    if (v === null || v === undefined) continue;
    if (bestV === null || (maximizing ? v > bestV : v < bestV)) {
      best = m;
      bestV = v;
    }
  }
  return best;
}

export function search(game, path, algorithm = 'minimax', opts) {
  return (algorithm === 'alphabeta' ? alphabeta : minimax)(game, path, opts);
}
