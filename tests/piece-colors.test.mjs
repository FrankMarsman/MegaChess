import test from 'node:test';
import assert from 'node:assert/strict';
import {occupiedSquarePalettes,occupiedSquareColors} from '../megachess/web/piece-colors.mjs';

function colorStats(hex) {
  const [r,g,b]=[1,3,5].map(index=>parseInt(hex.slice(index,index+2),16)/255);
  const high=Math.max(r,g,b),low=Math.min(r,g,b),difference=high-low;
  let hue=high===r?((g-b)/difference+(g<b?6:0)):high===g?(b-r)/difference+2:(r-g)/difference+4;
  return {hue:hue*60,lightness:(high+low)/2,saturation:difference/(1-Math.abs(high+low-1))};
}

const palettes=Object.entries(occupiedSquarePalettes).filter(([,palette])=>palette);

test('original team palette choices retain their exact base colors',()=>{
  assert.deepEqual(occupiedSquarePalettes,{
    none:null,
    'red-blue':{white:'#ef9195',black:'#91b8ee'},
    'blue-red':{white:'#91b8ee',black:'#ef9195'},
    'amber-violet':{white:'#efc269',black:'#b79be1'},
    'teal-orange':{white:'#7bc9bd',black:'#efa477'},
  });
});

test('None disables occupied-square colors even when type shading is selected',()=>{
  for(const value of [null,undefined,{},false,{white:'red',black:'blue'}]) {
    assert.equal(occupiedSquareColors(value),null);
    assert.equal(occupiedSquareColors(value,true),null);
  }
});

test('unshaded squares keep the chosen team color for all twelve signed pieces',()=>{
  for(const [,palette] of palettes) {
    const colors=occupiedSquareColors(palette);
    assert.equal(Object.keys(colors).length,12);
    for(let type=1;type<=6;type++) {
      assert.equal(colors[type],palette.white);
      assert.equal(colors[-type],palette.black);
    }
  }
});

test('each palette distinguishes all piece types while retaining both team hue families',()=>{
  for(const [name,palette] of palettes) {
    const colors=occupiedSquareColors(palette,true);
    assert.equal(new Set(Object.values(colors)).size,12,name);
    for(const [side,base] of [[1,palette.white],[-1,palette.black]]) {
      const baseHue=colorStats(base).hue;
      const lightness=[];
      for(let type=1;type<=6;type++) {
        const color=colors[side*type];
        assert.match(color,/^#[0-9a-f]{6}$/);
        const stats=colorStats(color),distance=Math.abs(stats.hue-baseHue);
        assert.ok(Math.min(distance,360-distance)<2,`${name}: hue for ${side*type}`);
        assert.ok(stats.saturation>=.64,`${name}: saturation for ${side*type}`);
        assert.ok(stats.lightness>=.42,`${name}: visible shade on a black background`);
        lightness.push(stats.lightness);
      }
      for(let index=1;index<lightness.length;index++) {
        assert.ok(lightness[index-1]-lightness[index]>.07,`${name}: adjacent type contrast`);
      }
      assert.ok(lightness[0]-lightness[5]>.39,`${name}: full type shade range`);
    }
  }
});

test('reversing team colors reverses every corresponding piece shade',()=>{
  for(const shaded of [false,true]) {
    const redBlue=occupiedSquareColors(occupiedSquarePalettes['red-blue'],shaded);
    const blueRed=occupiedSquareColors(occupiedSquarePalettes['blue-red'],shaded);
    for(let type=1;type<=6;type++) {
      assert.equal(redBlue[type],blueRed[-type]);
      assert.equal(redBlue[-type],blueRed[type]);
    }
  }
});

test('lookup creation preserves inputs, reuses colors, and cannot be mutated by callers',()=>{
  const palette=Object.freeze({white:'#ef9195',black:'#91b8ee'});
  const before=JSON.stringify(palette),colors=occupiedSquareColors(palette,true);
  assert.equal(JSON.stringify(palette),before);
  assert.equal(occupiedSquareColors({...palette},true),colors);
  assert.equal(Object.isFrozen(colors),true);
  assert.throws(()=>{colors[1]='#000000';},TypeError);
  assert.notEqual(occupiedSquareColors(palette,false),colors);
});
