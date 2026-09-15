import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { artifactSignaturePayload } from '../electron/client-release-contract.ts'
import { buildSignedReleaseManifest } from './sign-client-release.mjs'

test('creates matching server policy and pinned client trust from one signed installer', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-client-release-'))
  const artifact = path.join(root, 'Hermes-Setup.exe')
  const privateKeyFile = path.join(root, 'release-private.pem')
  const releaseFile = path.join(root, 'client-release.json')
  const { privateKey } = crypto.generateKeyPairSync('ed25519')

  fs.writeFileSync(artifact, 'installer fixture')
  fs.writeFileSync(privateKeyFile, privateKey.export({ format: 'pem', type: 'pkcs8' }))
  fs.writeFileSync(
    releaseFile,
    JSON.stringify({
      schema_version: 1,
      release: '0.21.3-adrian.1',
      release_sequence: 2026091501,
      protocol_epoch: 1,
      bundle_version: '1'
    })
  )

  const manifest = await buildSignedReleaseManifest({
    arch: 'x64',
    artifact,
    keyId: 'adrian-release-1',
    minimumSequence: 2026090101,
    platform: 'win32',
    privateKeyFile,
    releaseFile
  })

  const configured = manifest.server_config.artifacts['win32-x64']
  const policy = {
    artifact: {
      arch: 'x64',
      download_path: '/api/client-release/artifacts/win32/x64',
      key_id: configured.key_id,
      platform: 'win32',
      sha256: configured.sha256,
      signature: configured.signature,
      signature_algorithm: 'ed25519'
    },
    target: {
      bundle_version: manifest.server_config.desktop_bundle_version,
      minimum_client_sequence: manifest.server_config.minimum_sequence,
      protocol_epoch: manifest.server_config.protocol_epoch,
      release: manifest.server_config.target_release,
      release_sequence: manifest.server_config.target_sequence
    }
  }
  const publicKey = manifest.client_trust.keys['adrian-release-1']

  assert.equal(configured.sha256, crypto.createHash('sha256').update('installer fixture').digest('hex'))
  assert.equal(
    crypto.verify(null, artifactSignaturePayload(policy), publicKey, Buffer.from(configured.signature, 'base64')),
    true
  )
  assert.equal(manifest.server_config.mode, 'advisory')
})

test('rejects a compatibility floor above the target release', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-client-release-floor-'))
  const artifact = path.join(root, 'Hermes-Setup.exe')
  const privateKeyFile = path.join(root, 'release-private.pem')
  const releaseFile = path.join(root, 'client-release.json')
  const { privateKey } = crypto.generateKeyPairSync('ed25519')

  fs.writeFileSync(artifact, 'installer fixture')
  fs.writeFileSync(privateKeyFile, privateKey.export({ format: 'pem', type: 'pkcs8' }))
  fs.writeFileSync(
    releaseFile,
    JSON.stringify({
      schema_version: 1,
      release: '0.21.3-adrian.1',
      release_sequence: 20,
      protocol_epoch: 1,
      bundle_version: '1'
    })
  )

  await assert.rejects(
    buildSignedReleaseManifest({
      arch: 'x64',
      artifact,
      keyId: 'adrian-release-1',
      minimumSequence: 21,
      platform: 'win32',
      privateKeyFile,
      releaseFile
    }),
    /minimum_sequence/
  )
})
