import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $clientReleasePolicy } from '@/store/client-release'

import { RemoteClientReleaseOverlay } from './remote-client-release-overlay'


const requiredPolicy = {
  allow_sessions: false,
  artifact: {
    arch: 'x64',
    download_path: '/api/client-release/artifacts/win32/x64',
    key_id: 'adrian-release-1',
    platform: 'win32',
    sha256: 'a'.repeat(64),
    signature: 'signature',
    signature_algorithm: 'ed25519' as const
  },
  decision: 'update_required' as const,
  mode: 'enforce' as const,
  policy_state: 'enabled' as const,
  reason: 'This client is outside the supported range.',
  schema_version: 1 as const,
  target: {
    bundle_version: '1',
    minimum_client_sequence: 2026090101,
    protocol_epoch: 1,
    release: '0.21.3-adrian.1',
    release_sequence: 2026091501
  }
}

describe('RemoteClientReleaseOverlay', () => {
  beforeEach(() => {
    $clientReleasePolicy.set(null)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('is absent when the server permits sessions', () => {
    $clientReleasePolicy.set({ ...requiredPolicy, allow_sessions: true, decision: 'update_when_idle' })
    const { container } = render(<RemoteClientReleaseOverlay />)

    expect(container.innerHTML).toBe('')
  })

  it('blocks without a dismiss action and invokes the authenticated update handoff', async () => {
    const apply = vi.fn(async () => ({ ok: true, handedOff: true }))
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { clientRelease: { apply } }
    })
    $clientReleasePolicy.set(requiredPolicy)

    render(<RemoteClientReleaseOverlay />)
    expect(screen.getByRole('heading', { name: /update required/i })).not.toBeNull()
    expect(screen.queryByRole('button', { name: /close|dismiss|later/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /install approved update/i }))
    await waitFor(() => expect(apply).toHaveBeenCalledOnce())
  })
})
