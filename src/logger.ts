const isProduction = process.env.NODE_ENV === 'production';

type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const formatMessage = (level: LogLevel, message: string, meta?: object): string => {
  const timestamp = new Date().toISOString();
  const metaStr = meta ? ` ${JSON.stringify(meta)}` : '';
  return `[${timestamp}] [${level.toUpperCase()}] ${message}${metaStr}`;
};

export const logger = {
  debug: (message: string, meta?: object): void => {
    if (!isProduction) {
      console.log(formatMessage('debug', message, meta));
    }
  },
  info: (message: string, meta?: object): void => {
    console.log(formatMessage('info', message, meta));
  },
  warn: (message: string, meta?: object): void => {
    console.warn(formatMessage('warn', message, meta));
  },
  error: (message: string, meta?: object): void => {
    console.error(formatMessage('error', message, meta));
  },
};