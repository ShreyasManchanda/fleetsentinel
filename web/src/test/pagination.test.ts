import { describe, expect, it } from 'vitest';
import { decodeCursor, encodeCursor, nextCursor } from '../lib/helpers';

describe('keyset cursors', () => {
  it('round-trips offsets', () => {
    expect(decodeCursor(encodeCursor(120))).toBe(120);
    expect(decodeCursor(undefined)).toBe(0);
    expect(decodeCursor(null)).toBe(0);
  });
  it('falls back to 0 on garbage', () => {
    expect(decodeCursor('!!!not-base64!!!')).toBe(0);
    expect(decodeCursor(btoa('{"offset":-5}'))).toBe(0);
    expect(decodeCursor(btoa('{"offset":"x"}'))).toBe(0);
  });
  it('emits null cursor on the last page', () => {
    expect(nextCursor(0, 50, 180)).toBe(encodeCursor(50));
    expect(nextCursor(150, 50, 180)).toBe(null);
    expect(nextCursor(130, 50, 180)).toBe(null);
  });
});
