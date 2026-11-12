import test from 'node:test';
import assert from 'node:assert/strict';
import {boardAppearance,normalizeBoardAppearance} from '../megachess/web/board-appearance.mjs';

function brightness(hex) {
  return [1,3,5].map(index=>parseInt(hex.slice(index,index+2),16)).reduce((a,b)=>a+b,0);
}

test('default appearance preserves the existing checkerboard and background', () => {
  assert.deepEqual(boardAppearance(), {background:'#e6eadf',light:'#f0eadb',dark:'#819a82',
    label:'#61755b',border:'#62785a',guide:'#364d3b'});
});

test('background tone reaches black and white independently of board opacity', () => {
  for (const opacity of [0,35,100]) {
    const black=boardAppearance({tone:-100,opacity});
    const white=boardAppearance({tone:100,opacity});
    assert.deepEqual([black.background,black.light,black.dark],Array(3).fill('#000000'));
    assert.deepEqual([white.background,white.light,white.dark],Array(3).fill('#ffffff'));
  }
});

test('intermediate tones darken or lighten both square colors and background', () => {
  const darker=boardAppearance({tone:-50}),normal=boardAppearance(),lighter=boardAppearance({tone:50});
  for (const color of ['background','light','dark']) {
    assert.ok(brightness(darker[color])<brightness(normal[color]));
    assert.ok(brightness(normal[color])<brightness(lighter[color]));
  }
  assert.equal(darker.background,'#737570');
});

test('transparent checkerboard disappears into the chosen background', () => {
  for (const tone of [-75,0,75]) {
    const colors=boardAppearance({tone,opacity:0});
    assert.equal(colors.light,colors.background);
    assert.equal(colors.dark,colors.background);
  }
});

test('opacity fades square contrast without changing the background tone', () => {
  const solid=boardAppearance({tone:-25}),faded=boardAppearance({tone:-25,opacity:25});
  assert.equal(solid.background,faded.background);
  assert.ok(Math.abs(brightness(faded.light)-brightness(faded.dark))<
    Math.abs(brightness(solid.light)-brightness(solid.dark)));
});

test('labels and grid marks switch to light ink on dark backgrounds', () => {
  const black=boardAppearance({tone:-100}),white=boardAppearance({tone:100});
  for (const key of ['label','border','guide']) {
    assert.ok(brightness(black[key])>500);
    assert.ok(brightness(white[key])<400);
  }
});

test('labels remain readable throughout the darker-background range', () => {
  const luminance=hex=>{
    const rgb=[1,3,5].map(index=>parseInt(hex.slice(index,index+2),16)/255)
      .map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4);
    return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
  };
  for(let tone=-100;tone<0;tone++) {
    const colors=boardAppearance({tone}),a=luminance(colors.background),b=luminance(colors.label);
    assert.ok((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=4.5,`label contrast at tone ${tone}`);
  }
});

test('settings clamp extremes and use defaults for invalid stored values', () => {
  assert.deepEqual(normalizeBoardAppearance({tone:-200,opacity:200}),{tone:-100,opacity:100});
  assert.deepEqual(normalizeBoardAppearance({tone:200,opacity:-1}),{tone:100,opacity:0});
  for (const value of [NaN,Infinity,-Infinity,'50',null,undefined]) {
    assert.deepEqual(normalizeBoardAppearance({tone:value,opacity:value}),{tone:0,opacity:100});
  }
  assert.deepEqual(normalizeBoardAppearance(null),{tone:0,opacity:100});
  assert.deepEqual(normalizeBoardAppearance('invalid'),{tone:0,opacity:100});
});

test('appearance computation is pure and always returns hex colors', () => {
  const settings=Object.freeze({tone:-35,opacity:40});
  assert.deepEqual(boardAppearance(settings),boardAppearance(settings));
  for (const color of Object.values(boardAppearance(settings)))assert.match(color,/^#[0-9a-f]{6}$/);
});
