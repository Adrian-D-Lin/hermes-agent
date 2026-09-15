import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { appendSessionResumeNotices } from './index'


describe('session resume notices', () => {
  it('appends ordered client-only system messages without timestamps', () => {
    const transcript: ChatMessage[] = [
      { id: 'stored-1', role: 'user', parts: [{ type: 'text', text: 'hello' }] }
    ]

    const projected = appendSessionResumeNotices(transcript, [
      { id: ' startup ', text: ' Choose a project ' },
      { id: 'second', text: 'Choose an initiative' }
    ])

    expect(projected).toHaveLength(3)
    expect(projected.slice(1)).toEqual([
      {
        id: 'session-resume-notice:startup',
        role: 'system',
        parts: [{ type: 'text', text: 'Choose a project' }]
      },
      {
        id: 'session-resume-notice:second',
        role: 'system',
        parts: [{ type: 'text', text: 'Choose an initiative' }]
      }
    ])
    expect(projected[1]).not.toHaveProperty('timestamp')
  })

  it('deduplicates first-valid-wins and preserves identity when nothing is added', () => {
    const existing: ChatMessage[] = [
      {
        id: 'session-resume-notice:startup',
        role: 'system',
        parts: [{ type: 'text', text: 'Already shown' }]
      }
    ]

    const malformed = [
      null,
      { id: 1, text: 'wrong id type' },
      { id: 'wrong-text', text: 2 },
      { id: 'blank', text: '   ' },
      { id: 'startup', text: 'duplicate of existing' },
      { id: ' startup ', text: 'second duplicate' }
    ] as unknown as Array<{ id: string; text: string }>

    expect(appendSessionResumeNotices(existing, malformed)).toBe(existing)

    const withDuplicate = appendSessionResumeNotices([], [
      { id: 'one', text: 'first' },
      { id: ' one ', text: 'second' }
    ])

    expect(withDuplicate).toEqual([
      {
        id: 'session-resume-notice:one',
        role: 'system',
        parts: [{ type: 'text', text: 'first' }]
      }
    ])
  })

  it('returns the original array when the gateway supplies no notices', () => {
    const transcript: ChatMessage[] = []

    expect(appendSessionResumeNotices(transcript, undefined)).toBe(transcript)
    expect(appendSessionResumeNotices(transcript, [])).toBe(transcript)
  })
})
