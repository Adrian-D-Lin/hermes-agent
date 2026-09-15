import crypto from 'node:crypto'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  artifactSignaturePayload,
  buildClientReleaseIdentity,
  type ClientReleasePolicy,
  parseClientReleasePolicy,
  parseTrustedReleaseKeys,
  resolveClientReleasePolicy,
  verifyClientReleaseArtifact,
  verifyClientReleaseArtifactFile
} from './client-release-contract'


function unsignedPolicy(): ClientReleasePolicy {
  return {
    schema_version: 1,
    policy_state: 'enabled',
    decision: 'update_required',
    allow_sessions: false,
    mode: 'enforce',
    reason: 'Client is outside the supported range.',
    target: {
      release: '0.21.3-adrian.1',
      release_sequence: 2026091501,
      protocol_epoch: 1,
      bundle_version: '1',
      minimum_client_sequence: 2026090101
    },
    artifact: {
      platform: 'win32',
      arch: 'x64',
      download_path: '/api/client-release/artifacts/win32/x64',
      sha256: '',
      signature: '',
      key_id: 'adrian-release-1',
      signature_algorithm: 'ed25519'
    }
  }
}


function signedFixture() {
  const bytes = Buffer.from('installer fixture')
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519')
  const policy = unsignedPolicy()
  policy.artifact!.sha256 = crypto.createHash('sha256').update(bytes).digest('hex')
  policy.artifact!.signature = crypto.sign(null, artifactSignaturePayload(policy), privateKey).toString('base64')

  const trust = {
    schema_version: 1 as const,
    keys: { 'adrian-release-1': publicKey.export({ type: 'spki', format: 'pem' }).toString() }
  }

  return { bytes, policy, trust }
}


describe('remote client release contract', () => {
  it('builds the reported identity from the packaged install stamp', () => {
    expect(
      buildClientReleaseIdentity(
        {
          clientProtocolEpoch: 1,
          clientRelease: '0.21.3-adrian.1',
          clientReleaseSequence: 2026091501,
          commit: 'a'.repeat(40),
          desktopBundleVersion: '1',
          source: 'ci'
        },
        { arch: 'x64', isPackaged: true, platform: 'win32' }
      )
    ).toEqual({
      arch: 'x64',
      build_sha: 'a'.repeat(40),
      bundle_version: '1',
      install_kind: 'packaged',
      platform: 'win32',
      protocol_epoch: 1,
      release: '0.21.3-adrian.1',
      release_sequence: 2026091501
    })
  })

  it('reports an unstamped development client explicitly instead of guessing compatibility', () => {
    expect(buildClientReleaseIdentity(null, { arch: 'arm64', isPackaged: false, platform: 'darwin' })).toMatchObject({
      install_kind: 'development',
      protocol_epoch: 0,
      release: 'unversioned',
      release_sequence: 0
    })
  })

  it('allows only a 404 pre-contract server to bypass policy negotiation', async () => {
    const missing = Object.assign(new Error('not found'), { statusCode: 404 })

    await expect(resolveClientReleasePolicy(async () => Promise.reject(missing))).resolves.toMatchObject({
      allow_sessions: true,
      decision: 'policy_disabled'
    })
    await expect(resolveClientReleasePolicy(async () => Promise.reject(new Error('socket closed')))).rejects.toThrow(
      /socket closed/
    )
  })

  it('accepts the disabled compatibility response without an artifact', () => {
    const policy = parseClientReleasePolicy({
      schema_version: 1,
      policy_state: 'disabled',
      decision: 'policy_disabled',
      allow_sessions: true,
      mode: 'disabled',
      reason: 'Policy is disabled.',
      target: null,
      artifact: null
    })

    expect(policy.decision).toBe('policy_disabled')
  })

  it('rejects a required-update response that nevertheless permits sessions', () => {
    const { policy } = signedFixture()
    policy.allow_sessions = true

    expect(() => parseClientReleasePolicy(policy)).toThrow(/cannot permit sessions/)
  })

  it('verifies both the installer digest and its Ed25519 signature', () => {
    const { bytes, policy, trust } = signedFixture()

    expect(() => verifyClientReleaseArtifact(bytes, parseClientReleasePolicy(policy), trust)).not.toThrow()
  })

  it('verifies a staged installer without buffering the artifact in memory', async () => {
    const fixture = signedFixture()
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-client-release-'))
    const artifact = path.join(root, 'Hermes-Setup.exe')

    try {
      await fs.writeFile(artifact, fixture.bytes)
      await expect(verifyClientReleaseArtifactFile(artifact, fixture.policy, fixture.trust)).resolves.toBeUndefined()
    } finally {
      await fs.rm(root, { force: true, recursive: true })
    }
  })

  it('rejects bytes that do not match the signed manifest digest', () => {
    const { policy, trust } = signedFixture()

    expect(() => verifyClientReleaseArtifact(Buffer.from('tampered'), policy, trust)).toThrow(/SHA-256/)
  })

  it('rejects a valid signature made by a key outside the local trust bundle', () => {
    const { bytes, policy } = signedFixture()

    expect(() => verifyClientReleaseArtifact(bytes, policy, { schema_version: 1, keys: {} })).toThrow(/untrusted key/)
  })

  it('rejects malformed local trust material', () => {
    expect(() => parseTrustedReleaseKeys({ schema_version: 1, keys: { active: '' } })).toThrow(/invalid key/)
  })
})
