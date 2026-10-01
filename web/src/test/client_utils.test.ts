import { afterEach, describe, expect, it } from 'vitest';
import { getToken, idempotencyKey, setToken } from '../api/client';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('idempotency keys', () => {
  it('emits unique UUIDv4 keys for write retries', () => {
    const keys = new Set(Array.from({ length: 20 }, () => idempotencyKey()));
    expect(keys.size).toBe(20);
    for (const k of keys) expect(k).toMatch(UUID_V4);
  });
});

describe('token store', () => {
  afterEach(() => setToken(null));
  it('round-trips the bearer token', () => {
    setToken('tok-123');
    expect(getToken()).toBe('tok-123');
    expect(localStorage.getItem('fs_token')).toBe('tok-123');
    setToken(null);
    expect(getToken()).toBe(null);
    expect(localStorage.getItem('fs_token')).toBe(null);
  });
});
