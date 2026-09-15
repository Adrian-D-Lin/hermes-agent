import crypto from 'node:crypto'
import fs from 'node:fs'

export interface ClientReleaseIdentity {
  arch: string
  build_sha: string
  bundle_version: string
  install_kind: 'development' | 'packaged' | 'source'
  platform: string
  protocol_epoch: number
  release: string
  release_sequence: number
}

export interface ClientReleaseArtifact {
  arch: string
  download_path: string
  key_id: string
  platform: string
  sha256: string
  signature: string
  signature_algorithm: 'ed25519'
}

export interface ClientReleaseTarget {
  bundle_version: string
  minimum_client_sequence: number
  protocol_epoch: number
  release: string
  release_sequence: number
}

export interface ClientReleasePolicy {
  allow_sessions: boolean
  artifact: ClientReleaseArtifact | null
  decision: 'current' | 'policy_disabled' | 'update_required' | 'update_when_idle'
  mode: 'advisory' | 'disabled' | 'enforce'
  policy_state: 'disabled' | 'enabled'
  reason: string
  schema_version: 1
  target: ClientReleaseTarget | null
}

export interface TrustedReleaseKeys {
  schema_version: 1
  keys: Record<string, string>
}

export interface InstalledClientReleaseStamp {
  clientProtocolEpoch?: unknown
  clientRelease?: unknown
  clientReleaseSequence?: unknown
  commit?: unknown
  desktopBundleVersion?: unknown
  source?: unknown
}

export interface ClientReleaseRuntime {
  arch: string
  isPackaged: boolean
  platform: string
}

function positiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0
}

function nonNegativeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0
}

function text(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

export function buildClientReleaseIdentity(
  stamp: InstalledClientReleaseStamp | null,
  runtime: ClientReleaseRuntime
): ClientReleaseIdentity {
  const stamped =
    stamp &&
    text(stamp.clientRelease) &&
    positiveInteger(stamp.clientReleaseSequence) &&
    positiveInteger(stamp.clientProtocolEpoch) &&
    text(stamp.desktopBundleVersion)

  return {
    arch: runtime.arch,
    build_sha: stamp && text(stamp.commit) ? stamp.commit : '',
    bundle_version: stamped ? String(stamp.desktopBundleVersion).trim() : 'unknown',
    install_kind: runtime.isPackaged ? 'packaged' : stamp?.source === 'local' ? 'source' : 'development',
    platform: runtime.platform,
    protocol_epoch: stamped ? Number(stamp.clientProtocolEpoch) : 0,
    release: stamped ? String(stamp.clientRelease).trim() : 'unversioned',
    release_sequence: stamped ? Number(stamp.clientReleaseSequence) : 0
  }
}

export function disabledClientReleasePolicy(reason: string): ClientReleasePolicy {
  return {
    allow_sessions: true,
    artifact: null,
    decision: 'policy_disabled',
    mode: 'disabled',
    policy_state: 'disabled',
    reason,
    schema_version: 1,
    target: null
  }
}

/**
 * Ask a remote server for its client policy. A 404 is the one compatibility
 * exception: it identifies a pre-contract Hermes server and must remain usable
 * while installations are upgraded server-first. Every other failure is real
 * policy/auth/transport state and is surfaced to boot rather than bypassed.
 */
export async function resolveClientReleasePolicy(
  request: () => Promise<unknown>
): Promise<ClientReleasePolicy> {
  try {
    return parseClientReleasePolicy(await request())
  } catch (error) {
    const statusCode = Number(
      error && typeof error === 'object' ? (error as { statusCode?: unknown }).statusCode : NaN
    )

    if (statusCode === 404) {
      return disabledClientReleasePolicy('The remote Hermes server predates client-release policy support.')
    }

    throw error
  }
}

function targetFrom(value: unknown): ClientReleaseTarget | null {
  if (value === null) {
    return null
  }

  const target = value as Partial<ClientReleaseTarget>

  if (
    !target ||
    !text(target.release) ||
    !positiveInteger(target.release_sequence) ||
    !positiveInteger(target.protocol_epoch) ||
    !text(target.bundle_version) ||
    !nonNegativeInteger(target.minimum_client_sequence) ||
    target.minimum_client_sequence > target.release_sequence
  ) {
    throw new Error('Remote client release policy has an invalid target.')
  }

  return target as ClientReleaseTarget
}

function artifactFrom(value: unknown): ClientReleaseArtifact | null {
  if (value === null) {
    return null
  }

  const artifact = value as Partial<ClientReleaseArtifact>

  if (
    !artifact ||
    !text(artifact.platform) ||
    !text(artifact.arch) ||
    !text(artifact.download_path) ||
    !artifact.download_path.startsWith('/api/client-release/artifacts/') ||
    !text(artifact.sha256) ||
    !/^[0-9a-f]{64}$/.test(artifact.sha256) ||
    !text(artifact.signature) ||
    !text(artifact.key_id) ||
    artifact.signature_algorithm !== 'ed25519'
  ) {
    throw new Error('Remote client release policy has invalid artifact metadata.')
  }

  return artifact as ClientReleaseArtifact
}

export function parseClientReleasePolicy(value: unknown): ClientReleasePolicy {
  const policy = value as Partial<ClientReleasePolicy>
  const decisions = new Set(['current', 'policy_disabled', 'update_required', 'update_when_idle'])
  const modes = new Set(['advisory', 'disabled', 'enforce'])

  if (
    !policy ||
    policy.schema_version !== 1 ||
    !decisions.has(String(policy.decision)) ||
    !modes.has(String(policy.mode)) ||
    !['disabled', 'enabled'].includes(String(policy.policy_state)) ||
    typeof policy.allow_sessions !== 'boolean' ||
    !text(policy.reason)
  ) {
    throw new Error('Remote client release policy response is invalid.')
  }

  const target = targetFrom(policy.target)
  const artifact = artifactFrom(policy.artifact)

  if (policy.policy_state === 'disabled') {
    if (policy.decision !== 'policy_disabled' || !policy.allow_sessions || target !== null || artifact !== null) {
      throw new Error('Disabled remote client release policy is internally inconsistent.')
    }
  } else if (target === null) {
    throw new Error('Enabled remote client release policy is missing its target.')
  }

  if (policy.decision === 'update_required' && policy.allow_sessions) {
    throw new Error('A required client update cannot permit sessions.')
  }

  return { ...(policy as ClientReleasePolicy), target, artifact }
}

export function artifactSignaturePayload(policy: ClientReleasePolicy): Buffer {
  if (!policy.target || !policy.artifact) {
    throw new Error('The release policy does not contain an artifact to verify.')
  }

  const { artifact, target } = policy

  const canonical = [
    'hermes-client-release:v1',
    `release=${target.release}`,
    `release_sequence=${target.release_sequence}`,
    `protocol_epoch=${target.protocol_epoch}`,
    `bundle_version=${target.bundle_version}`,
    `platform=${artifact.platform}`,
    `arch=${artifact.arch}`,
    `sha256=${artifact.sha256}`
  ].join('\n')

  return Buffer.from(`${canonical}\n`, 'utf8')
}

export function verifyClientReleaseArtifact(
  bytes: Buffer,
  policy: ClientReleasePolicy,
  trust: TrustedReleaseKeys
): void {
  const actualHash = crypto.createHash('sha256').update(bytes).digest('hex')

  verifyClientReleaseArtifactDigest(actualHash, policy, trust)
}

export function verifyClientReleaseArtifactDigest(
  actualHash: string,
  policy: ClientReleasePolicy,
  trust: TrustedReleaseKeys
): void {
  if (trust.schema_version !== 1 || !trust.keys || typeof trust.keys !== 'object') {
    throw new Error('The local client-release trust bundle is invalid.')
  }

  if (!policy.artifact) {
    throw new Error('The release policy does not contain an artifact to verify.')
  }

  if (!/^[0-9a-f]{64}$/.test(actualHash)) {
    throw new Error('The downloaded Hermes installer produced an invalid SHA-256 digest.')
  }

  if (!crypto.timingSafeEqual(Buffer.from(actualHash), Buffer.from(policy.artifact.sha256))) {
    throw new Error('The downloaded Hermes installer failed its SHA-256 integrity check.')
  }

  const publicKey = trust.keys[policy.artifact.key_id]

  if (!text(publicKey)) {
    throw new Error(`The Hermes installer was signed by an untrusted key (${policy.artifact.key_id}).`)
  }

  const signature = Buffer.from(policy.artifact.signature, 'base64')

  if (!crypto.verify(null, artifactSignaturePayload(policy), publicKey, signature)) {
    throw new Error('The Hermes installer signature is invalid.')
  }
}

export async function verifyClientReleaseArtifactFile(
  filePath: string,
  policy: ClientReleasePolicy,
  trust: TrustedReleaseKeys
): Promise<void> {
  const hash = crypto.createHash('sha256')

  for await (const chunk of fs.createReadStream(filePath)) {
    hash.update(chunk)
  }

  verifyClientReleaseArtifactDigest(hash.digest('hex'), policy, trust)
}

export function parseTrustedReleaseKeys(value: unknown): TrustedReleaseKeys {
  const trust = value as Partial<TrustedReleaseKeys>

  if (!trust || trust.schema_version !== 1 || !trust.keys || typeof trust.keys !== 'object') {
    throw new Error('The local client-release trust bundle is invalid.')
  }

  for (const [keyId, publicKey] of Object.entries(trust.keys)) {
    if (!text(keyId) || !text(publicKey)) {
      throw new Error('The local client-release trust bundle contains an invalid key.')
    }
  }

  return trust as TrustedReleaseKeys
}
