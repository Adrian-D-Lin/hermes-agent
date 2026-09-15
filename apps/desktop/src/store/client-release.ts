import { atom } from 'nanostores'

import type { ClientReleasePolicy, HermesConnection } from '@/global'


export const $clientReleasePolicy = atom<ClientReleasePolicy | null>(null)

export function applyConnectionClientReleasePolicy(connection: HermesConnection): boolean {
  const policy = connection.mode === 'remote' ? (connection.clientReleasePolicy ?? null) : null

  $clientReleasePolicy.set(policy)

  return policy?.allow_sessions !== false
}

export function clearClientReleasePolicy(): void {
  $clientReleasePolicy.set(null)
}
