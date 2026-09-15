import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const RELEASE_SCHEMA_VERSION = 1

function requiredText(value, name) {
  const normalized = typeof value === 'string' ? value.trim() : ''
  if (!normalized) throw new Error(`${name} is required.`)
  return normalized
}

function positiveInteger(value, name) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer.`)
  }
  return value
}

export function readReleaseIdentity(releaseFile) {
  const parsed = JSON.parse(fs.readFileSync(releaseFile, 'utf8'))
  if (parsed.schema_version !== RELEASE_SCHEMA_VERSION) {
    throw new Error(`Unsupported client release schema: ${parsed.schema_version}`)
  }

  return {
    bundleVersion: requiredText(parsed.bundle_version, 'bundle_version'),
    protocolEpoch: positiveInteger(parsed.protocol_epoch, 'protocol_epoch'),
    release: requiredText(parsed.release, 'release'),
    releaseSequence: positiveInteger(parsed.release_sequence, 'release_sequence')
  }
}

export function signaturePayload({ arch, bundleVersion, platform, protocolEpoch, release, releaseSequence, sha256 }) {
  const canonical = [
    'hermes-client-release:v1',
    `release=${release}`,
    `release_sequence=${releaseSequence}`,
    `protocol_epoch=${protocolEpoch}`,
    `bundle_version=${bundleVersion}`,
    `platform=${platform}`,
    `arch=${arch}`,
    `sha256=${sha256}`
  ].join('\n')

  return Buffer.from(`${canonical}\n`, 'utf8')
}

export async function sha256File(filePath) {
  const hash = crypto.createHash('sha256')
  for await (const chunk of fs.createReadStream(filePath)) hash.update(chunk)
  return hash.digest('hex')
}

export async function buildSignedReleaseManifest({
  arch,
  artifact,
  keyId,
  minimumSequence,
  platform,
  privateKeyFile,
  releaseFile
}) {
  const identity = readReleaseIdentity(releaseFile)
  const normalizedPlatform = requiredText(platform, 'platform').toLowerCase()
  const normalizedArch = requiredText(arch, 'arch').toLowerCase()
  const normalizedKeyId = requiredText(keyId, 'key_id')
  const artifactPath = path.resolve(artifact)
  const privateKey = crypto.createPrivateKey(fs.readFileSync(privateKeyFile, 'utf8'))
  const publicKey = crypto.createPublicKey(privateKey).export({ format: 'pem', type: 'spki' }).toString()
  const sha256 = await sha256File(artifactPath)
  const signature = crypto
    .sign(
      null,
      signaturePayload({
        arch: normalizedArch,
        bundleVersion: identity.bundleVersion,
        platform: normalizedPlatform,
        protocolEpoch: identity.protocolEpoch,
        release: identity.release,
        releaseSequence: identity.releaseSequence,
        sha256
      }),
      privateKey
    )
    .toString('base64')
  const floor = minimumSequence === undefined ? identity.releaseSequence : Number(minimumSequence)

  if (!Number.isSafeInteger(floor) || floor < 0 || floor > identity.releaseSequence) {
    throw new Error('minimum_sequence must be a non-negative integer no greater than release_sequence.')
  }

  const platformKey = `${normalizedPlatform}-${normalizedArch}`
  return {
    schema_version: RELEASE_SCHEMA_VERSION,
    generated_at: new Date().toISOString(),
    client_trust: {
      schema_version: RELEASE_SCHEMA_VERSION,
      keys: { [normalizedKeyId]: publicKey }
    },
    server_config: {
      enabled: true,
      mode: 'advisory',
      target_release: identity.release,
      target_sequence: identity.releaseSequence,
      minimum_sequence: floor,
      protocol_epoch: identity.protocolEpoch,
      desktop_bundle_version: identity.bundleVersion,
      artifacts: {
        [platformKey]: {
          file: artifactPath,
          key_id: normalizedKeyId,
          sha256,
          signature
        }
      }
    }
  }
}

export function parseArgs(argv) {
  const values = {}
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index]
    const value = argv[index + 1]
    if (!name?.startsWith('--') || value === undefined) {
      throw new Error(`Invalid argument near ${name || '<end>'}. Arguments use --name value pairs.`)
    }
    values[name.slice(2)] = value
  }

  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  return {
    arch: values.arch || 'x64',
    artifact: requiredText(values.artifact, '--artifact'),
    keyId: requiredText(values['key-id'], '--key-id'),
    minimumSequence: values['minimum-sequence'],
    output: requiredText(values.output, '--output'),
    platform: values.platform || 'win32',
    privateKeyFile: requiredText(values['private-key'], '--private-key'),
    releaseFile: values['release-file'] || path.join(root, 'client-release.json')
  }
}

async function main() {
  const { output, ...options } = parseArgs(process.argv.slice(2))
  const manifest = await buildSignedReleaseManifest(options)
  const outputPath = path.resolve(output)
  fs.mkdirSync(path.dirname(outputPath), { recursive: true })
  fs.writeFileSync(outputPath, `${JSON.stringify(manifest, null, 2)}\n`, { flag: 'wx' })
  process.stdout.write(`${outputPath}\n`)
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : ''
if (invokedPath === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    process.stderr.write(`${error.message || error}\n`)
    process.exitCode = 1
  })
}
