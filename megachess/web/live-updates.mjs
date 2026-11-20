// A failed or stalled request must never stop later live-board updates.
export class LiveUpdates {
  constructor({getGame, fetchUpdates, apply, onError = () => {},
    schedule = (fn, ms) => setInterval(fn, ms), cancel = id => clearInterval(id),
    timeout = (fn, ms) => setTimeout(fn, ms), clearTimeout: clear = id => clearTimeout(id),
    interval = 300, timeoutMs = 8000}) {
    Object.assign(this, {getGame, fetchUpdates, apply, onError, schedule, cancel, timeout, clear, interval, timeoutMs});
    this.pending = null;
    this.timer = null;
  }
  start() {
    if (this.timer !== null) return;
    this.timer = this.schedule(() => this.refresh(), this.interval);
  }
  stop() {
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
    this.pending?.controller.abort();
    this.pending = null;
  }
  async refresh(force = false) {
    const game = this.getGame();
    if (!game) return;
    if (this.pending) {
      if (!force && this.pending.id === game.id) return;
      this.pending.controller.abort();
    }
    const request = {id: game.id, controller: new AbortController()};
    this.pending = request;
    let timer;
    try {
      const expired = new Promise((_, reject) => {
        timer = this.timeout(() => {
          request.controller.abort();
          reject(new Error('Live update timed out. Reconnecting…'));
        }, this.timeoutMs);
      });
      const result = await Promise.race([
        this.fetchUpdates(game, request.controller.signal), expired
      ]);
      if (this.pending === request && !request.controller.signal.aborted && this.getGame()?.id === game.id) {
        this.apply(result);
      }
    } catch (error) {
      if (this.pending === request && this.getGame()?.id === game.id) this.onError(error);
    } finally {
      this.clear(timer);
      if (this.pending === request) this.pending = null;
    }
  }
}
