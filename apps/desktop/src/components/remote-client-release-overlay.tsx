import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Loader } from '@/components/ui/loader'
import { $clientReleasePolicy } from '@/store/client-release'


export function RemoteClientReleaseOverlay() {
  const policy = useStore($clientReleasePolicy)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (policy?.allow_sessions !== false || policy.decision !== 'update_required') {
    return null
  }

  const target = policy.target?.release ?? 'the server-approved release'
  const canApply = Boolean(policy.artifact && window.hermesDesktop?.clientRelease)

  const apply = async () => {
    if (!canApply || applying) {
      return
    }

    setApplying(true)
    setError(null)

    try {
      const result = await window.hermesDesktop!.clientRelease.apply()

      if (!result.ok) {
        setError(result.error || 'The Desktop update could not be started.')
        setApplying(false)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setApplying(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-(--z-setup) flex items-center justify-center bg-(--ui-chat-surface-background) p-6"
      data-glass-opaque=""
      data-testid="remote-client-release-overlay"
    >
      <section className="w-full max-w-xl rounded-2xl border border-(--theme-border) bg-(--theme-surface) p-7 shadow-2xl">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-(--theme-muted-foreground)">
          Primus compatibility
        </p>
        <h1 className="mt-2 text-2xl font-semibold text-(--theme-foreground)">Hermes Desktop update required</h1>
        <p className="mt-3 text-sm leading-6 text-(--theme-muted-foreground)">{policy.reason}</p>
        <p className="mt-3 text-sm text-(--theme-foreground)">
          Required release: <span className="font-mono">{target}</span>
        </p>
        {!policy.artifact && (
          <p className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
            Primus has not published a signed installer for this computer. An administrator must add one before this
            client can open sessions.
          </p>
        )}
        {error && (
          <p className="mt-4 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-200" role="alert">
            {error}
          </p>
        )}
        <div className="mt-6 flex items-center gap-3">
          <Button disabled={!canApply || applying} onClick={() => void apply()}>
            {applying ? (
              <span className="flex items-center gap-2">
                <Loader className="size-5" /> Downloading and verifying…
              </span>
            ) : (
              'Install approved update'
            )}
          </Button>
          <span className="text-xs text-(--theme-muted-foreground)">
            Sessions stay unavailable until the approved client reconnects.
          </span>
        </div>
      </section>
    </div>
  )
}
