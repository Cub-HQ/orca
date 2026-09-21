import { describe, expect, it, vi } from 'vitest'
import {
  formatTaskPageFactoryStageElapsed,
  getTaskPageFactoryStageLabel
} from './task-page-github-factory-stage'

vi.mock('@/i18n/i18n', () => ({
  translate: (_key: string, fallback: string) => fallback
}))

describe('task-page-github-factory-stage', () => {
  it('maps factory stages to readable labels', () => {
    expect(getTaskPageFactoryStageLabel('building')).toBe('Building')
    expect(getTaskPageFactoryStageLabel('awaiting-review')).toBe('Awaiting review')
  })

  it('formats elapsed time from the stage entry timestamp', () => {
    const now = Date.parse('2026-09-21T12:00:00Z')

    expect(formatTaskPageFactoryStageElapsed('2026-09-21T11:55:00Z', now)).toBe('5m')
    expect(formatTaskPageFactoryStageElapsed('2026-09-21T09:00:00Z', now)).toBe('3h')
    expect(formatTaskPageFactoryStageElapsed('2026-09-18T12:00:00Z', now)).toBe('3d')
  })

  it('returns no elapsed label for invalid timestamps', () => {
    expect(formatTaskPageFactoryStageElapsed('not-a-date', Date.now())).toBeNull()
  })
})
