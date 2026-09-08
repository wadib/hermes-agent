import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

// Test harness supplies the host's locale registration, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import { KANBAN_LOCALES } from './i18n'
import { BlockOwnerBadge, blockOwnerBadge } from './ui'

let disposeLocales: () => void

afterEach(() => {
  cleanup()
  disposeLocales()
})

beforeEach(() => {
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
})

describe('blocked card owner badges', () => {
  it.each([
    ['needs_input', 'YOU'],
    ['capability', 'WALL'],
    ['transient', 'AGENT'],
    [undefined, 'AGENT']
  ] as const)('renders %s as a visible icon-and-text %s badge', (blockKind, label) => {
    const { container } = render(<BlockOwnerBadge blockKind={blockKind} />)

    expect(screen.getByText(label)).toBeTruthy()
    expect(container.querySelector('.codicon')).toBeTruthy()
  })

  it('keeps an unknown block kind in the agent lane', () => {
    expect(blockOwnerBadge('unknown', { you: 'YOU', wall: 'WALL', agent: 'AGENT' })).toMatchObject({
      icon: 'robot',
      label: 'AGENT',
      variant: 'muted'
    })
  })
})
