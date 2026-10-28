// Run with `npm test`. The same checks as server/tests/test_engine.py and test_search.py,
// for the browser port that the playground uses.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { VARIANTS, alphabeta, buildTree, legalMoves, makeGame, minimax, subtreeSize, validate } from './game.js';

const game1 = makeGame(VARIANTS[1]);
const game2 = makeGame(VARIANTS[2]);

test('root values and tree sizes of the lecture games', () => {
  assert.equal(buildTree(game1, ['A']).value, 1);
  assert.equal(buildTree(game2, ['A']).value, -1);
  assert.equal(buildTree(game1, ['A']).nodes.length, 23);
  assert.equal(buildTree(game2, ['A']).nodes.length, 51);
});

test('game 1: moving to B loses at once, D wins', () => {
  const r = minimax(game1, ['A']);
  assert.deepEqual(r.child_values, { B: -1, D: 1 });
  assert.equal(r.best, 'D');
});

test('alpha-beta agrees with minimax in every reachable position', () => {
  for (const game of [game1, game2, makeGame({ rows: 4, cols: 4, start: 'F', cups: ['P'], blocked: ['K'] })]) {
    const tree = buildTree(game, [game.start]);
    for (const node of tree.nodes) {
      const path = [...node.id];
      const mm = minimax(game, path, { trace: false });
      const ab = alphabeta(game, path, { trace: false });
      assert.equal(mm.value, node.value, node.id);
      assert.equal(ab.value, node.value, node.id);
      assert.equal(ab.best, mm.best, node.id);
      assert.ok(ab.stats.nodes_visited <= mm.stats.nodes_visited);
    }
  }
});

test('alpha-beta prunes in game 2', () => {
  const ab = alphabeta(game2, ['A']);
  assert.ok(ab.stats.nodes_pruned > 0);
  assert.equal(ab.stats.nodes_visited + ab.stats.nodes_pruned, subtreeSize(game2, ['A']));
  assert.ok(ab.trace.some((e) => e.t === 'prune'));
});

test('blocked cells are never entered', () => {
  const game = makeGame({ rows: 3, cols: 3, start: 'A', cups: ['I'], blocked: ['B'] });
  assert.deepEqual(legalMoves(game, ['A']), ['D']);
  assert.ok(buildTree(game, ['A']).nodes.every((n) => !n.id.includes('B')));
});

test('validate', () => {
  assert.deepEqual(validate(VARIANTS[1]), []);
  assert.equal(validate({ rows: 3, cols: 3, start: 'A', cups: [], blocked: [] }).length, 1);
  assert.equal(validate({ rows: 3, cols: 3, start: 'A', cups: ['A'], blocked: [] }).length, 1);
});
