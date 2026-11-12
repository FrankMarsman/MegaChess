// This timer changes the displayed frame only. The live game keeps progressing.
export class ReplayPlayback {
  constructor({getLength, getIndex, showFrame, onChange = () => {},
               schedule = (callback, delay) => setInterval(callback, delay),
               cancel = timer => clearInterval(timer), interval = 100}) {
    Object.assign(this, {getLength, getIndex, showFrame, onChange, schedule, cancel});
    this.interval = Number.isFinite(interval) && interval > 0 ? interval : 100;
    this.timer = null;
    this.timerGeneration = 0;
  }

  get playing() { return this.timer !== null; }

  start() {
    if (this.playing || this.getLength() < 1) return false;
    const current = this.getIndex();
    const start = current === null ? 0 : Math.max(0, Math.min(this.getLength(), current));
    this.scheduleTimer();
    this.showFrame(start);
    this.onChange(true);
    return true;
  }

  stop() {
    if (!this.playing) return;
    this.timerGeneration++;
    this.cancel(this.timer);
    this.timer = null;
    this.onChange(false);
  }

  setInterval(milliseconds) {
    if (!Number.isFinite(milliseconds) || milliseconds <= 0 || milliseconds === this.interval) return this.interval;
    this.interval = milliseconds;
    if (this.playing) {
      this.timerGeneration++;
      this.cancel(this.timer);
      this.scheduleTimer();
    }
    return this.interval;
  }

  scheduleTimer() {
    const generation = ++this.timerGeneration;
    this.timer = this.schedule(() => {
      if (generation === this.timerGeneration) this.tick();
    }, this.interval);
  }

  tick() {
    if (!this.playing) return;
    const length = this.getLength();
    if (length < 1) { this.stop(); return; }
    const current = this.getIndex();
    this.showFrame(current === null || current >= length ? 0 : Math.max(0, current) + 1);
  }
}
