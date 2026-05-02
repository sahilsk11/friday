export class SpeakingPolicy {
  shouldSpeak(text: string): boolean {
    if (!text || text.trim().length === 0) {
      return false;
    }

    const trimmed = text.trim();

    if (this.isToolOutput(trimmed)) {
      return false;
    }

    if (this.isCodeBlock(trimmed)) {
      return false;
    }

    if (this.isLogMessage(trimmed)) {
      return false;
    }

    return true;
  }

  private isToolOutput(text: string): boolean {
    const toolPatterns = [
      /^(Running|Executing|Ran|Completed|Failed)/i,
      /^\$ .+/,
      /^(bash|shell|cmd):/i,
      /tool:/i,
      /\[(tool|command|run)\]/i,
    ];

    return toolPatterns.some((pattern) => pattern.test(text));
  }

  private isCodeBlock(text: string): boolean {
    return /^(```| {4}|\t)/.test(text) || text.includes('```');
  }

  private isLogMessage(text: string): boolean {
    const logPatterns = [
      /^\d{4}-\d{2}-\d{2}/,
      /^\[ERROR\]/i,
      /^\[WARN\]/i,
      /^\[INFO\]/i,
      /^\[DEBUG\]/i,
      /^TRACE:/i,
    ];

    return logPatterns.some((pattern) => pattern.test(text));
  }

  filterForSpeaking(text: string): string {
    const lines = text.split('\n');
    const filtered = lines.filter((line) => this.shouldSpeak(line));
    return filtered.join('\n');
  }
}