import test from 'node:test';
import assert from 'node:assert/strict';
import {LiveUpdates} from '../megachess/web/live-updates.mjs';

const deferred = () => {let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
function fixture() {
  let game={id:'a',revision:0};
  const calls=[], applied=[], errors=[], timers=new Map(); let next=0;
  const updates=new LiveUpdates({getGame:()=>game,
    fetchUpdates:(g,signal)=>{const pending=deferred();calls.push({...pending,game:g,signal});return pending.promise;},
    apply:r=>applied.push(r),onError:e=>errors.push(e),
    timeout:fn=>{timers.set(++next,fn);return next;},clearTimeout:id=>timers.delete(id)});
  return {updates,calls,applied,errors,timers,setGame:g=>game=g};
}
test('polling is single-flight and each response reaches the renderer',async()=>{
 const f=fixture(), first=f.updates.refresh();await f.updates.refresh();assert.equal(f.calls.length,1);
 f.calls[0].resolve({ply:1});await first;assert.deepEqual(f.applied,[{ply:1}]);
 const second=f.updates.refresh();f.calls[1].resolve({ply:2});await second;assert.equal(f.applied.length,2);
});
test('a hung request times out, releases polling and ignores its late response',async()=>{
 const f=fixture(), first=f.updates.refresh();[...f.timers.values()][0]();await first;
 assert.equal(f.calls[0].signal.aborted,true);assert.equal(f.errors.length,1);
 const second=f.updates.refresh();f.calls[1].resolve({ply:3});await second;
 f.calls[0].resolve({ply:1});await Promise.resolve();assert.deepEqual(f.applied,[{ply:3}]);
});
test('forced refresh after a control supersedes an in-flight poll',async()=>{
 const f=fixture(), first=f.updates.refresh(), second=f.updates.refresh(true);
 assert.equal(f.calls[0].signal.aborted,true);f.calls[1].resolve({paused:true});await second;
 f.calls[0].resolve({paused:false});await first;assert.deepEqual(f.applied,[{paused:true}]);
});
test('switching games never applies an old board response',async()=>{
 const f=fixture(), first=f.updates.refresh();f.setGame({id:'b',revision:0});
 const second=f.updates.refresh();f.calls[0].resolve({id:'a'});await first;
 f.calls[1].resolve({id:'b'});await second;assert.deepEqual(f.applied,[{id:'b'}]);
});
test('stop cancels polling and discards pending responses',async()=>{
 const f=fixture(), first=f.updates.refresh();f.updates.stop();f.calls[0].resolve({ply:1});await first;assert.equal(f.applied.length,0);
});
