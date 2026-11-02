import fs from 'fs';

// Append-only JSON-lines error log with a size cap. When the file would grow
// past maxBytes it is renamed to `<path>.1` (replacing any older one) and a
// fresh file is started, so disk use stays under ~2 * maxBytes. The log lives
// in the container's writable layer, which Docker's log rotation does not
// cover; in 2026-10 an uncapped copy reached 44GB.
//
// Writes are synchronous so rotation is exact: an async stream opens its file
// lazily, and a burst of writes could rotate before the open, or the old
// stream could recreate the file after the rename.
export const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;

export function createErrorLog(filePath, { maxBytes = DEFAULT_MAX_BYTES } = {}) {
  let fd = fs.openSync(filePath, 'a');
  let size = fs.fstatSync(fd).size;
  let failed = false;

  function rotate() {
    fs.closeSync(fd);
    try {
      fs.renameSync(filePath, `${filePath}.1`);
    } catch (err) {
      console.error(`[error-log] rotate failed for ${filePath}: ${err.message}`);
    }
    fd = fs.openSync(filePath, 'a');
    size = fs.fstatSync(fd).size;
  }

  return {
    write(line) {
      if (fd === null) return;
      try {
        const bytes = Buffer.byteLength(line);
        if (size > 0 && size + bytes > maxBytes) rotate();
        fs.writeSync(fd, line);
        size += bytes;
      } catch (err) {
        // Never let the error log take the collector down (e.g. ENOSPC).
        if (!failed) console.error(`[error-log] write failed for ${filePath}: ${err.message}`);
        failed = true;
      }
    },
    end(cb) {
      if (fd !== null) {
        try { fs.closeSync(fd); } catch { /* already closed */ }
        fd = null;
      }
      if (cb) cb();
    },
  };
}
