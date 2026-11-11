// This timer changes the displayed frame only. The live game keeps progressing.
export class ReplayPlayback {
  constructor({getLength, getIndex, showFrame, onChange = () => {},
               schedule = (callback, delay) => setInterval(callback, delay),
               cancel = timer => clearInterval(timer), interval = 1000}) {
    Object.assign(this, {getLength, getIndex, showFrame, onChange, schedule, cancel, interval});
    this.timer = null;
  }

  get playing() { return this.timer !== null; }

  start() {
    if (this.playing || this.getLength() < 1) return false;
    const current = this.getIndex();
    const start = current === null ? 0 : Math.max(0, Math.min(this.getLength(), current));
    this.timer = this.schedule(() => this.tick(), this.interval);
    this.showFrame(start);
    this.onChange(true);
    return true;
  }

  stop() {
    if (!this.playing) return;
    this.cancel(this.timer);
    this.timer = null;
    this.onChange(false);
  }

  tick() {
    if (!this.playing) return;
    const length = this.getLength();
    if (length < 1) { this.stop(); return; }
    const current = this.getIndex();
    this.showFrame(current === null || current >= length ? 0 : Math.max(0, current) + 1);
  }
}
