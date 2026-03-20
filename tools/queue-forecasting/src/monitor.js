export function createNoOpMonitor() {
  const noop = () => {};
  const noopMonitor = {
    reportError: (err, extra) => {
      console.error('[pulse error]', err.message || err, extra || '');
    },
    log: {
      pulseConnected: noop,
      pulseDisconnected: noop,
    },
    timer: (_name, fn) => fn(),
    timedHandler: (_name, fn) => fn,
    count: noop,
    measure: noop,
    childMonitor: () => noopMonitor,
  };
  return noopMonitor;
}
