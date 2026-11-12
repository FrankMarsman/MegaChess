import test from 'node:test';
import assert from 'node:assert/strict';
import {MoveAnimation, detectPieceMovement} from '../megachess/web/move-animation.mjs';

function position(ply, turn, pieces, size = 8) {
  const board = Array(size ** 2).fill(0);
  for (const [index, piece] of Object.entries(pieces)) board[Number(index)] = piece;
  return {size, board, ply, turn};
}

test('detects forward and backward adjacent quiet moves from the displayed boards', () => {
  const before = position(0, 1, {48:1,4:-6,60:6});
  const after = position(1, -1, {32:1,4:-6,60:6});
  assert.deepEqual(detectPieceMovement(before, after), {from:48,to:32,piece:1,reverse:false});
  assert.deepEqual(detectPieceMovement(after, before), {from:32,to:48,piece:1,reverse:true});
});

test('capture detection ignores the victim restored by reverse replay', () => {
  const before = position(5, -1, {18:-2,35:1,4:-6,60:6});
  const after = position(6, 1, {35:-2,4:-6,60:6});
  assert.deepEqual(detectPieceMovement(before, after), {from:18,to:35,piece:-2,reverse:false});
  assert.deepEqual(detectPieceMovement(after, before), {from:35,to:18,piece:-2,reverse:true});
});

test('en passant detects the moving pawn despite the off-destination capture', () => {
  const before = position(10, 1, {28:1,27:-1,4:-6,60:6});
  const after = position(11, -1, {19:1,4:-6,60:6});
  assert.deepEqual(detectPieceMovement(before, after), {from:28,to:19,piece:1,reverse:false});
  assert.deepEqual(detectPieceMovement(after, before), {from:19,to:28,piece:1,reverse:true});
});

test('promotion and reverse promotion travel using the source piece sprite', () => {
  const before = position(20, 1, {9:1,0:-4,4:-6,60:6});
  const after = position(21, -1, {0:5,4:-6,60:6});
  assert.deepEqual(detectPieceMovement(before, after), {from:9,to:0,piece:1,reverse:false});
  assert.deepEqual(detectPieceMovement(after, before), {from:0,to:9,piece:5,reverse:true});
});

test('history jumps, unchanged positions, and board changes cannot invent a trajectory', () => {
  const before = position(0, 1, {48:1,4:-6,60:6});
  const after = position(2, 1, {32:1,4:-6,60:6});
  assert.equal(detectPieceMovement(before, after), null);
  assert.equal(detectPieceMovement(before, before), null);
  assert.equal(detectPieceMovement(before, position(1,-1,{0:1},24)), null);
  assert.equal(detectPieceMovement(before, {...after,ply:1,board:[]}), null);
  assert.equal(detectPieceMovement(null, after), null);
});

test('castling or other multi-piece changes are rejected as ambiguous', () => {
  const before = position(0, 1, {60:6,63:4,4:-6});
  const after = position(1, -1, {62:6,61:4,4:-6});
  assert.equal(detectPieceMovement(before, after), null);
  assert.equal(detectPieceMovement(after, before), null);
});

test('a movement starts at its origin, eases toward its destination, and arrives', () => {
  const animation = new MoveAnimation();
  assert.equal(animation.start({from:8,to:24,piece:1}, 1000), true);
  assert.equal(animation.frame(1000).progress, 0);
  const halfway = animation.frame(1090);
  assert.equal(halfway.progress, .5);
  assert.equal(halfway.easedProgress, .875);
  assert.equal(halfway.moving, true);
  assert.equal(halfway.trailOpacity, 1);
  const arrived = animation.frame(1180);
  assert.equal(arrived.progress, 1);
  assert.equal(arrived.easedProgress, 1);
  assert.equal(arrived.moving, false);
  assert.equal(arrived.trailOpacity, 1);
});

test('the directional trail fades after arrival and disappears at expiry', () => {
  const animation = new MoveAnimation({duration:100,trailDuration:500});
  animation.start({from:3,to:27,piece:-4}, 0);
  assert.equal(animation.frame(300).trailOpacity, .5);
  assert.equal(animation.frame(499).active, true);
  assert.equal(animation.frame(500), null);
  assert.equal(animation.frame(600), null);
});

test('fast replay can shorten a move without losing its trail', () => {
  const animation = new MoveAnimation();
  animation.start({from:7,to:0,piece:4,duration:80}, 100);
  assert.equal(animation.frame(140).progress, .5);
  assert.equal(animation.frame(180).moving, false);
  assert.equal(animation.frame(180).trailOpacity, 1);
});

test('a new move replaces the old trajectory and supports reverse replay', () => {
  const animation = new MoveAnimation();
  animation.start({from:8,to:24,piece:1}, 0);
  animation.start({from:24,to:8,piece:1}, 50);
  const frame = animation.frame(50);
  assert.deepEqual([frame.from,frame.to,frame.piece], [24,8,1]);
  assert.equal(frame.progress, 0);
});

test('clear removes both motion and its lingering trail', () => {
  const animation = new MoveAnimation();
  animation.start({from:4,to:6,piece:6}, 0);
  animation.clear();
  assert.equal(animation.frame(50), null);
});

test('a zero-duration move leaves only its trail and clock rollback stays at the origin', () => {
  const animation = new MoveAnimation({clock:()=>50});
  animation.start({from:0,to:8,piece:-1,duration:0});
  assert.equal(animation.frame().moving, false);
  assert.equal(animation.frame().progress, 1);
  animation.start({from:0,to:8,piece:-1});
  assert.equal(animation.frame(20).progress, 0);
});

test('invalid movement data clears stale motion rather than drawing an invalid piece', () => {
  const animation = new MoveAnimation();
  for (const invalid of [{from:2,to:2,piece:1},{from:-1,to:3,piece:1},
    {from:2,to:3,piece:0},{from:2,to:3,piece:7},{from:2,to:3,piece:1,duration:NaN}]) {
    animation.start({from:2,to:3,piece:1}, 0);
    assert.equal(animation.start(invalid, 10), false);
    assert.equal(animation.frame(20), null);
  }
});
