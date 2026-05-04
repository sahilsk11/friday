// Speaking policy — decides whether a text delta should be forwarded to TTS.
//
// Phase 1: simple regex heuristics.  Phase 2 can refine these with
// per-chunk metadata from the agent adapter (e.g. channel tags).

/**
 * Returns true if the text chunk should be spoken aloud.
 *
 * Rules (all must pass):
 *  1. Not empty after trimming.
 *  2. Does not look like the opening of a Markdown code fence (``` or ~~~).
 *  3. Does not look like a shell command line (starts with $ or # followed by space).
 *  4. Not a bare tool/log prefix like "[tool:" or "[system:" or "[error:".
 */
export function shouldSpeak(text: string): boolean {
  const trimmed = text.trim();

  // Empty — nothing to say.
  if (trimmed.length === 0) return false;

  // Opening of a code block — skip everything until the block closes.
  // A caller maintaining state can detect the closing fence; for Phase 1 we
  // just skip the opening line.
  if (/^`{3,}|^~{3,}/.test(trimmed)) return false;

  // Shell command prompt lines.
  if (/^\$\s/.test(trimmed) || /^#\s/.test(trimmed)) return false;

  // Tool / log prefix lines emitted by coding agents.
  if (/^\[(?:tool|system|error|warn|debug|info):/.test(trimmed)) return false;

  return true;
}

/**
 * Given a full accumulated text block, return a speaking-safe version.
 *
 * Phase 1: strips code-fence blocks entirely; everything else passes through.
 * Phase 2: optionally summarise long blocks.
 */
export function filterForSpeaking(text: string): string {
  // Remove fenced code blocks (``` ... ``` or ~~~ ... ~~~).
  return text.replace(/(`{3,}|~{3,})[\s\S]*?\1/g, '').trim();
}
