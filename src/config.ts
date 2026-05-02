import * as dotenv from 'dotenv';

dotenv.config();

const defaultOpencodeUrl = process.env.OPENCODE_URL || 'http://127.0.0.1:7395';

export const opencodeByProject: Record<string, string> = {
  factorbacktest: process.env.OPENCODE_FACTORBACKTEST_URL || 'http://127.0.0.1:7395',
  friday: process.env.OPENCODE_FRIDAY_URL || 'http://127.0.0.1:7399',
  strange: process.env.OPENCODE_STRANGE_URL || 'http://127.0.0.1:7397',
};

export const config = {
  port: process.env.PORT ? parseInt(process.env.PORT, 10) : 3000,
  opencodeUrl: defaultOpencodeUrl,
  opencodeByProject,
  elevenlabsApiKey: process.env.ELEVENLABS_API_KEY || '',
};