import { describe, expect, it } from 'vitest'

import {
  hostedRoomIsIdle,
  hostedRoomLogToMessages,
  projectHostedRoom,
  TERMINAL_TURN_KINDS,
  type HostedRoomLog
} from './hosted-room-projection'

// Fixture mirrors a real `groups.log` tail captured from the Python self-check:
// message.user (seq 1) -> message.member (seq 2) -> turn.settled (seq 3) ->
// room.activity (seq 4). The projection turns that into two visible chat lines
// and reports the room idle.
const settledLog: HostedRoomLog = {
  cursor: 4,
  latest_seq: 4,
  events: [
    {
      seq: 1,
      event_id: 'user:abc',
      kind: 'message.user',
      actor: { kind: 'user', id: 'desktop' },
      created_at: 1_700_000_000,
      payload: { text: '@ops confirm the pilot control is wired', thread_id: 'pilot-thread-1' }
    },
    {
      seq: 2,
      event_id: 'dmessage:xyz',
      kind: 'message.member',
      actor: { kind: 'member', id: 'ops', profile: 'ops' },
      created_at: 1_700_000_001,
      payload: { text: 'reply from ops', thread_id: 'pilot-thread-1', member_id: 'ops' }
    },
    {
      seq: 3,
      event_id: 'dterminal:xyz',
      kind: 'turn.settled',
      actor: { kind: 'gateway', id: 'install:x' },
      created_at: 1_700_000_002,
      payload: { thread_id: 'pilot-thread-1', member_id: 'ops' }
    },
    {
      seq: 4,
      event_id: 'dactivity:1:settled',
      kind: 'room.activity',
      actor: { kind: 'gateway', id: 'install:x' },
      created_at: 1_700_000_003,
      payload: { thread_id: 'pilot-thread-1' }
    }
  ]
}

describe('hostedRoomLogToMessages', () => {
  it('projects only the two message-bearing kinds into ordered chat lines', () => {
    const messages = hostedRoomLogToMessages(settledLog)

    expect(messages.map(m => m.text)).toEqual(['@ops confirm the pilot control is wired', 'reply from ops'])
    expect(messages.map(m => m.from.kind)).toEqual(['user', 'member'])
    expect(messages[1].from.name).toBe('ops')
    expect(messages[0].thread).toBe('pilot-thread-1')
  })

  it('is idempotent by event id (re-projection never duplicates a line)', () => {
    const a = hostedRoomLogToMessages(settledLog)
    const b = hostedRoomLogToMessages(settledLog)

    expect(a.map(m => m.id)).toEqual(b.map(m => m.id))
    expect(new Set(a.map(m => m.id)).size).toBe(a.length)
    expect(a[0].id).toBe('hosted:user:abc')
  })

  it('sorts by monotonic seq regardless of input order', () => {
    const shuffled: HostedRoomLog = { ...settledLog, events: [...(settledLog.events || [])].reverse() }

    expect(hostedRoomLogToMessages(shuffled).map(m => m.text)).toEqual([
      '@ops confirm the pilot control is wired',
      'reply from ops'
    ])
  })

  it('skips empty-text and coordination events', () => {
    const messages = hostedRoomLogToMessages(settledLog)

    // 4 events in, 2 visible out: turn.settled and room.activity are dropped.
    expect(messages).toHaveLength(2)
  })
})

describe('hostedRoomIsIdle', () => {
  it('is idle when the newest turn event is terminal', () => {
    expect(hostedRoomIsIdle(settledLog)).toBe(true)
  })

  it('is not idle when a message has no following terminal event', () => {
    const inFlight: HostedRoomLog = {
      cursor: 2,
      latest_seq: 2,
      events: (settledLog.events || []).slice(0, 2)
    }

    expect(hostedRoomIsIdle(inFlight)).toBe(false)
  })

  it('treats an empty log as idle', () => {
    expect(hostedRoomIsIdle({ events: [] })).toBe(true)
  })

  it('recognises every terminal kind', () => {
    for (const kind of TERMINAL_TURN_KINDS) {
      expect(hostedRoomIsIdle({ events: [{ seq: 1, kind: 'message.user', payload: { text: 'x' } }, { seq: 2, kind }] })).toBe(
        true
      )
    }
  })
})

describe('projectHostedRoom', () => {
  it('carries identity + cursor + idle into the store-ready shape', () => {
    const projection = projectHostedRoom('selfcheck-atlas-ops', settledLog)

    expect(projection.roomId).toBe('selfcheck-atlas-ops')
    expect(projection.cursor).toBe(4)
    expect(projection.latestSeq).toBe(4)
    expect(projection.idle).toBe(true)
    expect(projection.log).toHaveLength(2)
  })
})
