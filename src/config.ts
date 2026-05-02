import * as dotenv from 'dotenv';

dotenv.config();

export const config = {
  port: process.env.PORT ? parseInt(process.env.PORT, 10) : 3000,
  opencodeUrl: process.env.OPENCODE_URL || 'http://127.0.0.1:7395',
  elevenlabsApiKey: process.env.ELEVENLABS_API_KEY || '',
};