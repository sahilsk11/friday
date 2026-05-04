// Tiny console-based logger. No external deps.
// debug output is gated on DEBUG=1 environment variable.
// info/warn use console.warn; error uses console.error (both allowed by lint).

const debugEnabled = process.env['DEBUG'] === '1';

function formatMessage(level: string, msg: string, meta?: unknown): string {
  const ts = new Date().toISOString();
  const metaStr = meta !== undefined ? ` ${JSON.stringify(meta)}` : '';
  return `[${ts}] ${level.padEnd(5)} ${msg}${metaStr}`;
}

export const logger = {
  debug(msg: string, meta?: unknown): void {
    if (debugEnabled) {
      console.warn(formatMessage('DEBUG', msg, meta));
    }
  },
  info(msg: string, meta?: unknown): void {
    console.warn(formatMessage('INFO', msg, meta));
  },
  warn(msg: string, meta?: unknown): void {
    console.warn(formatMessage('WARN', msg, meta));
  },
  error(msg: string, meta?: unknown): void {
    console.error(formatMessage('ERROR', msg, meta));
  },
};
