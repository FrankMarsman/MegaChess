import test from 'node:test';
import assert from 'node:assert/strict';
import {countArmies, captureTimeline, materialValue} from '../megachess/web/board-statistics.mjs';

function position(pieces, extras = {}) {
  const board = Array(64).fill(0);
  for (const [square, piece] of Object.entries(pieces)) board[Number(square)] = piece;
  return {board, size: 8, ep_target: null, ep_pawn: null, ...extras};
}

test('remaining counts include kings, with conventional material excluding kings', () => {
  const result = countArmies(position({0:-6, 1:-4, 2:-1, 60:6, 59:5, 58:2}).board);
  assert.equal(result.white.total, 3);
  assert.equal(result.black.total, 3);
  assert.equal(result.white.counts[6], 1);
  assert.equal(result.white.material, 12.2);
  assert.equal(result.black.material, 6);
});

test('captures aggregate by capturing side and actual victim, preserving replay totals', () => {
  const initial = position({0:-6, 1:-4, 2:3, 59:-5, 60:6, 61:2});
  const first = position({0:-6, 1:3, 59:-5, 60:6, 61:2});
  const second = position({0:-6, 1:3, 60:6, 61:-5});
  const records = [{move:{from:2,to:1},position:first}, {move:{from:59,to:61},position:second}];
  const timeline = captureTimeline(initial, records);
  assert.equal(timeline[0].white[4], 0);
  assert.equal(timeline[1].white[4], 1);
  assert.equal(timeline[1].black[2], 0);
  assert.equal(timeline[2].black[2], 1);
  assert.equal(materialValue(timeline[2].white), 5);
});

test('en passant counts the off-destination pawn as captured', () => {
  const initial = position({0:-6, 28:1, 27:-1, 60:6}, {ep_target:19,ep_pawn:27});
  const next = position({0:-6,19:1,60:6});
  const timeline = captureTimeline(initial,[{move:{from:28,to:19},position:next}]);
  assert.equal(timeline[1].white[1],1);
  assert.equal(timeline[1].black.reduce((a,b)=>a+b),0);
});

test('promotion itself is not a capture; taking a promoted queen counts a queen', () => {
  const initial = position({0:-6,9:1,2:-4,60:6});
  const promoted = position({0:-6,1:5,2:-4,60:6});
  const taken = position({0:-6,1:-4,60:6});
  const records = [{move:{from:9,to:1,promotion:5},position:promoted}, {move:{from:2,to:1},position:taken}];
  const timeline = captureTimeline(initial,records);
  assert.equal(timeline[1].white.reduce((a,b)=>a+b),0);
  assert.equal(timeline[2].black[5],1);
  assert.equal(timeline[2].black[1],0);
});

test('capture-promotion counts its victim once and an incremental extension preserves history', () => {
  const initial = position({0:-6,9:1,2:-4,17:-2,60:6});
  const next = position({0:-6,2:5,17:-2,60:6});
  const record = {move:{from:9,to:2,promotion:5},position:next};
  const first = captureTimeline(initial,[record]);
  const reply = {move:{from:17,to:2},position:position({0:-6,2:-2,60:6})};
  const second = captureTimeline(initial,[record,reply],first);
  assert.equal(second.length,3);
  assert.equal(second[1].white[4],1);
  assert.equal(second[0].white[4],0);
  assert.equal(second[1].black[5],0);
  assert.equal(second[2].black[5],1);
  assert.equal(captureTimeline(initial,[record,reply],second).length,3);
});

test('large armies remain compact numeric counts', () => {
  const board = [...Array(1352).fill(1), ...Array(1352).fill(-1), 6, -6];
  const armies = countArmies(board);
  assert.equal(armies.white.counts[1],1352);
  assert.equal(armies.black.total,1353);
});
