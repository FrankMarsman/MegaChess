export const occupiedSquarePalettes = Object.freeze({
  none:null,
  'red-blue':Object.freeze({white:'#ef9195',black:'#91b8ee'}),
  'blue-red':Object.freeze({white:'#91b8ee',black:'#ef9195'}),
  'amber-violet':Object.freeze({white:'#efc269',black:'#b79be1'}),
  'teal-orange':Object.freeze({white:'#7bc9bd',black:'#efa477'}),
});

// Pawn, knight, bishop, rook, queen, king: light to dark within each team hue.
const LIGHTNESS = [.84,.76,.68,.60,.51,.43];
const SATURATION_OFFSET = [-.08,.04,-.02,.08,.02,.10];
const cache = new Map();
const HEX = /^#[0-9a-f]{6}$/i;

function hsl(hex) {
  const [r,g,b] = [1,3,5].map(index=>parseInt(hex.slice(index,index+2),16)/255);
  const high=Math.max(r,g,b),low=Math.min(r,g,b),difference=high-low,lightness=(high+low)/2;
  let hue=0;
  if(difference) {
    if(high===r)hue=((g-b)/difference+(g<b?6:0))/6;
    else if(high===g)hue=((b-r)/difference+2)/6;
    else hue=((r-g)/difference+4)/6;
  }
  return {hue,saturation:difference?difference/(1-Math.abs(2*lightness-1)):0};
}

function hexFromHsl(hue,saturation,lightness) {
  const amplitude=saturation*Math.min(lightness,1-lightness);
  const channel=offset=>{
    const wave=(offset+hue*12)%12;
    const value=lightness-amplitude*Math.max(-1,Math.min(wave-3,9-wave,1));
    return Math.round(value*255).toString(16).padStart(2,'0');
  };
  return '#'+channel(0)+channel(8)+channel(4);
}

export function occupiedSquareColors(palette,shaded=false) {
  if(!palette||!HEX.test(palette.white)||!HEX.test(palette.black))return null;
  const key=`${palette.white}|${palette.black}|${Boolean(shaded)}`;
  if(cache.has(key))return cache.get(key);
  const colors={};
  for(const [side,base] of [[1,palette.white],[-1,palette.black]]) {
    const {hue,saturation}=hsl(base);
    for(let type=1;type<=6;type++) {
      const vividness=Math.min(.92,Math.max(.65,saturation+SATURATION_OFFSET[type-1]));
      colors[side*type]=shaded?hexFromHsl(hue,vividness,LIGHTNESS[type-1]):base;
    }
  }
  Object.freeze(colors);cache.set(key,colors);
  return colors;
}
