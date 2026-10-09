import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from '../index';

describe('authentication response redirect', () => {
  const assign = vi.fn();

  beforeEach(() => {
    assign.mockClear();
    vi.stubGlobal('window', {
      location: { pathname: '/settings', search: '?category=system', hash: '#desktop-version-info', assign },
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  it('preserves the complete destination on an expired session', async () => {
    const error = { response: { status: 401 }, message: 'expired session' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).toHaveBeenCalledWith('/login?redirect=%2Fsettings%3Fcategory%3Dsystem%23desktop-version-info');
  });

  it('does not redirect an unauthorized login request into a loop', async () => {
    window.location.pathname = '/login';
    const error = { response: { status: 401 }, message: 'incorrect password' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).not.toHaveBeenCalled();
  });

  it('does not treat a server error as a session expiry', async () => {
    const error = { response: { status: 500 }, message: 'server error' };
    await expect(apiClient.get('/fixture', { adapter: () => Promise.reject(error) })).rejects.toBe(error);
    expect(assign).not.toHaveBeenCalled();
  });
});
