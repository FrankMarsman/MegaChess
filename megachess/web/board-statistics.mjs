// Position and capture totals are independent of an AI's evaluation weights.
export const MATERIAL_VALUES = [0, 1, 3.2, 3.3, 5, 9, 0];

export function materialValue(counts) {
  return counts.reduce((value, amount, kind) => value + amount * MATERIAL_VALUES[kind], 0);
}

export function countArmies(board) {
  const sides = {white: {counts: Array(7).fill(0), total: 0}, black: {counts: Array(7).fill(0), total: 0}};
  for (const piece of board) {
    if (!piece) continue;
    const army = sides[piece > 0 ? 'white' : 'black'];
    army.counts[Math.abs(piece)]++;
    army.total++;
  }
  for (const army of Object.values(sides)) army.material = materialValue(army.counts);
  return sides;
}

// Store actual victims, not initial-minus-current counts: promotion changes a
// piece's type without capturing it, and promoted pieces can later be captured.
export function captureTimeline(initialPosition, records, previous = []) {
  const timeline = previous.length && previous.length <= records.length + 1 ? previous : [
    {white: Array(7).fill(0), black: Array(7).fill(0)}
  ];
  for (let index = timeline.length - 1; index < records.length; index++) {
    const before = index ? records[index - 1].position : initialPosition;
    const move = records[index].move;
    const mover = before?.board?.[move.from];
    let victim = before?.board?.[move.to] || 0;
    if (Math.abs(mover) === 1 && !victim && before.ep_target === move.to && before.ep_pawn != null) {
      victim = before.board[before.ep_pawn] || 0;
    }
    const last = timeline[timeline.length - 1];
    if (mover && victim && Math.sign(victim) !== Math.sign(mover) && Math.abs(victim) !== 6) {
      const next = {white: last.white.slice(), black: last.black.slice()};
      next[mover > 0 ? 'white' : 'black'][Math.abs(victim)]++;
      timeline.push(next);
    } else {
      timeline.push(last);
    }
  }
  return timeline;
}
