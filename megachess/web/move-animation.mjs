// Recognize only adjacent single-piece moves, in either direction through history.
export function detectPieceMovement(previous, position) {
  if (!previous || !position || !Number.isInteger(position.size) || position.size < 1 ||
      previous.size !== position.size || !Array.isArray(previous.board) ||
      !Array.isArray(position.board) || previous.board.length !== position.board.length ||
      position.board.length !== position.size ** 2) return null;
  const delta = position.ply - previous.ply;
  if (delta !== 1 && delta !== -1) return null;
  const side = delta === 1 ? previous.turn : position.turn;
  if (side !== 1 && side !== -1) return null;
  let from = null, to = null;
  for (let index = 0; index < position.board.length; index++) {
    if (previous.board[index] === position.board[index]) continue;
    if (previous.board[index] * side > 0) {
      if (from !== null) return null;
      from = index;
    }
    if (position.board[index] * side > 0) {
      if (to !== null) return null;
      to = index;
    }
  }
  if (from === null || to === null || from === to) return null;
  return {from, to, piece:previous.board[from], reverse:delta < 0};
}

// Pure timing state; the board owns drawing and requestAnimationFrame scheduling.
export class MoveAnimation {
  constructor({duration = 180, trailDuration = 700, clock = () => performance.now()} = {}) {
    this.duration = Number.isFinite(duration) && duration >= 0 ? duration : 180;
    this.trailDuration = Number.isFinite(trailDuration) && trailDuration >= 0 ? trailDuration : 700;
    this.clock = clock;
    this.move = null;
  }

  start({from, to, piece, duration = this.duration}, now = this.clock()) {
    if (!Number.isInteger(from) || from < 0 || !Number.isInteger(to) || to < 0 ||
        from === to || !Number.isInteger(piece) || piece === 0 || Math.abs(piece) > 6 ||
        !Number.isFinite(now) || !Number.isFinite(duration) || duration < 0) {
      this.clear();
      return false;
    }
    this.move = {from, to, piece, duration, startedAt:now,
      lifetime:Math.max(duration, this.trailDuration)};
    return true;
  }

  clear() { this.move = null; }

  frame(now = this.clock()) {
    const move = this.move;
    if (!move || !Number.isFinite(now)) return null;
    const elapsed = Math.max(0, now - move.startedAt);
    if (elapsed >= move.lifetime) { this.clear(); return null; }
    const progress = move.duration > 0 ? Math.min(1, elapsed / move.duration) : 1;
    const moving = progress < 1;
    const fadeDuration = move.lifetime - move.duration;
    const trailOpacity = moving ? 1 : fadeDuration > 0 ?
      Math.max(0, 1 - (elapsed - move.duration) / fadeDuration) : 0;
    return {from:move.from, to:move.to, piece:move.piece, progress,
      easedProgress:1 - (1 - progress) ** 3, moving, trailOpacity, active:true};
  }
}
