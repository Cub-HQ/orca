import { translate } from '@/i18n/i18n'
import type { GitHubWorkItem } from '../../../shared/types'

const FACTORY_STAGE_LABELS: Record<NonNullable<GitHubWorkItem['factoryStage']>, string> = {
  building: 'Building',
  'pr-open': 'PR open',
  reviewed: 'Reviewed',
  'awaiting-review': 'Awaiting review',
  blocked: 'Blocked',
  merged: 'Merged',
  deployed: 'Deployed'
}

export function getTaskPageFactoryStageLabel(
  stage: NonNullable<GitHubWorkItem['factoryStage']>
): string {
  return translate(`taskPage.factoryStage.${stage}`, FACTORY_STAGE_LABELS[stage])
}

export function formatTaskPageFactoryStageElapsed(
  enteredAt: string,
  nowMs = Date.now()
): string | null {
  const enteredAtMs = Date.parse(enteredAt)
  if (!Number.isFinite(enteredAtMs)) {
    return null
  }
  const minutes = Math.max(0, Math.floor((nowMs - enteredAtMs) / 60_000))
  if (minutes < 60) {
    return `${minutes}m`
  }
  const hours = Math.floor(minutes / 60)
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`
}
