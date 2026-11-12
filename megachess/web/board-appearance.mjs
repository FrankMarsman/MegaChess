const ORIGINAL = {background:'#e6eadf',light:'#f0eadb',dark:'#819a82'};

export function normalizeBoardAppearance(settings = {}) {
  const source = settings && typeof settings === 'object' ? settings : {};
  const clamp = (value, low, high, fallback) => Number.isFinite(value) ?
    Math.min(high, Math.max(low, value)) : fallback;
  return {tone:clamp(source.tone, -100, 100, 0),
    opacity:clamp(source.opacity, 0, 100, 100)};
}

function channels(hex) {
  return [1,3,5].map(index => parseInt(hex.slice(index,index+2),16));
}

function blend(from, to, amount) {
  const target = channels(to);
  return '#' + channels(from).map((value,index) =>
    Math.round(value + (target[index]-value)*amount).toString(16).padStart(2,'0')).join('');
}

function luminance(hex) {
  const linear = channels(hex).map(channel => {
    const value = channel/255;
    return value <= .04045 ? value/12.92 : ((value+.055)/1.055)**2.4;
  });
  return linear[0]*.2126 + linear[1]*.7152 + linear[2]*.0722;
}

// Only checkerboard colors fade: sprites, team colors, and move indicators are
// drawn independently and must stay opaque as the board becomes transparent.
export function boardAppearance(settings = {}) {
  const {tone,opacity} = normalizeBoardAppearance(settings);
  const toward = tone < 0 ? '#000000' : '#ffffff';
  const toneAmount = Math.abs(tone)/100;
  const background = blend(ORIGINAL.background,toward,toneAmount);
  const light = blend(background,blend(ORIGINAL.light,toward,toneAmount),opacity/100);
  const dark = blend(background,blend(ORIGINAL.dark,toward,toneAmount),opacity/100);
  const darkBackground = luminance(background) < .18;
  return {background,light,dark,
    label:darkBackground?'#ffffff':tone<0?'#000000':'#61755b',
    border:darkBackground?'#c2d2ba':tone<0?'#263b2e':'#62785a',
    guide:darkBackground?'#a9bda2':tone<0?'#20382f':'#364d3b'};
}
