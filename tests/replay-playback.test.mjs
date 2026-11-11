import test from 'node:test';
import assert from 'node:assert/strict';
import {ReplayPlayback} from '../megachess/web/replay-playback.mjs';

function fixture(length = 3, index = null) {
  let callback, cancellations = 0, schedules = 0;
  const frames = [], changes = [];
  const playback = new ReplayPlayback({
    getLength: () => length, getIndex: () => index,
    showFrame: frame => { index = frame; frames.push(frame); },
    onChange: playing => changes.push(playing),
    schedule: fn => { callback = fn; schedules++; return 0; },
    cancel: () => { cancellations++; },
  });
  return {playback, frames, changes, tick: () => callback?.(),
    setLength: value => { length = value; },
    get index() { return index; }, get cancellations() { return cancellations; }, get schedules() { return schedules; }};
}

test('empty history cannot start playback', () => {
  const f = fixture(0);
  assert.equal(f.playback.start(), false);
  assert.equal(f.playback.playing, false);
  assert.deepEqual(f.frames, []);
});

test('Play starts at the initial position from live and loops every step', () => {
  const f = fixture();
  assert.equal(f.playback.start(), true);
  for (let i = 0; i < 5; i++) f.tick();
  assert.deepEqual(f.frames, [0, 1, 2, 3, 0, 1]);
});

test('a single recorded move loops between the start and its resulting position', () => {
  const f = fixture(1);
  f.playback.start(); f.tick(); f.tick(); f.tick();
  assert.deepEqual(f.frames, [0, 1, 0, 1]);
});

test('newly recorded steps extend the active loop without restarting playback', () => {
  const f = fixture(2);
  f.playback.start(); f.tick(); f.tick();
  f.setLength(4);
  f.tick(); f.tick(); f.tick();
  assert.deepEqual(f.frames, [0, 1, 2, 3, 4, 0]);
  assert.equal(f.schedules, 1);
});

test('Stop freezes the selected frame and stale timer calls cannot advance it', () => {
  const f = fixture();
  f.playback.start(); f.tick();
  f.playback.stop(); f.tick(); f.playback.stop();
  assert.equal(f.index, 1);
  assert.equal(f.cancellations, 1);
  assert.equal(f.playback.playing, false);
});

test('restarting from a manually selected frame continues there without duplicate timers', () => {
  const f = fixture(5, 3);
  f.playback.start(); f.playback.start(); f.tick();
  assert.deepEqual(f.frames, [3, 4]);
  assert.equal(f.schedules, 1);
});

test('history removal stops playback rather than displaying a missing frame', () => {
  const f = fixture();
  f.playback.start(); f.setLength(0); f.tick();
  assert.equal(f.playback.playing, false);
  assert.equal(f.cancellations, 1);
});

test('default timers advance asynchronously and Stop cancels them', {timeout:1000}, async () => {
  let index = null, signal;
  const advanced = new Promise(resolve => { signal = resolve; });
  const playback = new ReplayPlayback({getLength:()=>2,getIndex:()=>index,
    showFrame:frame=>{index=frame;if(frame===1)signal();},interval:5});
  try {
    playback.start();
    await advanced;
    assert.equal(index,1);
  } finally {
    playback.stop();
  }
  assert.equal(playback.playing,false);
});
