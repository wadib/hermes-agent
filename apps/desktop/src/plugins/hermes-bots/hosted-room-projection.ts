/**
 * Hosted-room -> Desktop projection.
 *
 * Desktop's group surfaces render a plugin-owned `GroupChatRoom` (log of
 * `GroupMessage`s keyed by roomId). The hosted-room RPC lifecycle, by contrast,
 * publishes a monotonic event log via `groups.log` (`message.user` /
 * `message.member` / `turn.*` / `room.activity`). This module is the pure,
 * testable bridge between the two: it maps a `groups.log` page onto the room
 * store so a gateway-driven hosted room becomes user-visible in the Bots pane —
 * WITHOUT the plugin having to own the drive loop.
 *
 * It is deliberately projection-only: it never mints credentials, never calls a
 * mutating `groups.*` method, and treats the gateway log as the source of truth
 * (idempotent by event `seq`). The mutating lifecycle stays behind the
 * authenticated operator control (Python `tui_gateway.hosted_room_pilot`).
 */

import type { GroupMessage, GroupMessageAuthor } from './types'

/** One event as returned inside `groups.log` `{ events: [...] }`. */
export interface HostedRoomEvent {
  actor?: {
    display_name?: string
    id?: string
    kind?: string
    profile?: string
  }
  created_at?: number
  event_id?: string
  kind?: string
  payload?: {
    member_id?: string
    text?: string
    thread_id?: string
  }
  seq?: number
}

/** The `groups.log` result envelope this projection consumes. */
export interface HostedRoomLog {
  cursor?: number
  events?: HostedRoomEvent[]
  latest_seq?: number
}

/** Room-log event kinds that carry a user-visible chat line. */
const VISIBLE_MESSAGE_KINDS = new Set(['message.user', 'message.member'])

/** Terminal member-turn kinds published by the Discussion planner. */
export const TERMINAL_TURN_KINDS = new Set(['turn.settled', 'turn.failed', 'turn.cancelled', 'turn.deferred'])

/** True when the log's newest turn event is terminal (room is idle, not driving). */
export function hostedRoomIsIdle(log: HostedRoomLog): boolean {
  const events = log.events || []

  for (let i = events.length - 1; i >= 0; i -= 1) {
    const kind = String(events[i]?.kind || '')

    if (TERMINAL_TURN_KINDS.has(kind)) {
      return true
    }

    if (kind === 'message.user' || kind === 'message.member') {
      // A message with no following terminal event means a turn is (or should be) in flight.
      return false
    }
  }

  return true
}

function eventAuthor(event: HostedRoomEvent): GroupMessageAuthor {
  if (String(event.actor?.kind) === 'member') {
    const name = String(event.actor?.display_name || event.actor?.profile || event.actor?.id || event.payload?.member_id || 'member')

    return { kind: 'member', name }
  }

  return { kind: 'user', name: 'You' }
}

/**
 * Convert a `groups.log` page into ordered `GroupMessage`s. Only the two
 * message-bearing kinds project to visible lines; `turn.*` / `room.activity`
 * are coordination events and are skipped. Ordering follows the log's monotonic
 * `seq`, so a partial page merges deterministically with an earlier one.
 */
export function hostedRoomLogToMessages(log: HostedRoomLog): GroupMessage[] {
  const events = [...(log.events || [])].sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0))
  const messages: GroupMessage[] = []

  for (const event of events) {
    const kind = String(event.kind || '')

    if (!VISIBLE_MESSAGE_KINDS.has(kind)) {
      continue
    }

    const text = String(event.payload?.text || '')

    if (!text) {
      continue
    }

    messages.push({
      // Event id keeps the projection idempotent: re-projecting the same page
      // never duplicates a line, and a merge can dedupe by `id`.
      id: event.event_id ? `hosted:${event.event_id}` : `hosted:seq:${event.seq}`,
      at: typeof event.created_at === 'number' ? Math.round(event.created_at * 1000) : Number(event.seq || 0),
      from: eventAuthor(event),
      text,
      thread: event.payload?.thread_id ? String(event.payload.thread_id) : undefined
    })
  }

  return messages
}

/** The projected room shape written into the Desktop room store. */
export interface HostedRoomProjection {
  cursor: number
  idle: boolean
  latestSeq: number
  log: GroupMessage[]
  roomId: string
}

/**
 * Project one hosted room (identity + a `groups.log` page) into the shape the
 * Desktop room store consumes. Pure: the caller writes it via `updateGroupChat`.
 */
export function projectHostedRoom(roomId: string, log: HostedRoomLog): HostedRoomProjection {
  return {
    roomId,
    log: hostedRoomLogToMessages(log),
    cursor: Number(log.cursor || 0),
    latestSeq: Number(log.latest_seq || 0),
    idle: hostedRoomIsIdle(log)
  }
}
