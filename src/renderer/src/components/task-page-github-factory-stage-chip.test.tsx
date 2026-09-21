// @vitest-environment happy-dom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import { GitHubFactoryStageChip } from './TaskPage'

vi.mock('@/i18n/i18n', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...(actual as object), translate: (_key: string, fallback: string) => fallback }
})

vi.mock('@/components/dashboard/useNow', () => ({
  useNow: () => Date.parse('2026-09-21T12:00:00Z')
}))

afterEach(cleanup)

describe('GitHubFactoryStageChip', () => {
  it('opens the issue URL without opening the task drawer', async () => {
    const openUrl = vi.fn()
    const onClick = vi.fn()
    Object.defineProperty(window, 'api', {
      configurable: true,
      value: { shell: { openUrl } }
    })

    render(
      <TooltipProvider>
        <div onClick={onClick}>
          <GitHubFactoryStageChip
            stage="building"
            enteredAt="2026-09-21T11:55:00Z"
            issueNumber={42}
            issueUrl="https://github.com/acme/widgets/issues/42"
          />
        </div>
      </TooltipProvider>
    )

    const chip = screen.getByRole('button', { name: 'Open issue #42 on GitHub' })
    fireEvent.click(chip)

    expect(openUrl).toHaveBeenCalledWith('https://github.com/acme/widgets/issues/42')
    expect(onClick).not.toHaveBeenCalled()

    await userEvent.hover(chip)
    expect((await screen.findByRole('tooltip')).textContent).toBe('Open issue #42 on GitHub')
  })
})
