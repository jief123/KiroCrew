/**
 * A row's share controls on the MCP page: its one labelled button and its
 * Details. `McpManagement` mounts them per row.
 *
 * The page shows no launch command anywhere. A press reads the launch at that
 * moment and the write carries its `expected_launch`, so the approval is pinned
 * to the exact launch the gateway resolved whether or not it was ever on screen;
 * a later change to it still pauses sharing until the person presses again.
 */
import { useState } from 'react'
import { Check, ChevronRight } from 'lucide-react'
import type { McpManagedServer } from '../../api/client'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { type LaunchRefusal, launchIsApprovable } from './mcpManagementModel'

/**
 * Which agents declare a server, collapsed by default. It lists names only: the
 * command the server runs is not shown on this page.
 */
export function ServerDetails({ server }: { server: McpManagedServer }) {
  const [open, setOpen] = useState(false)
  if (server.agents.length === 0) return null
  return (
    <details
      className="group mt-1"
      open={open}
      onToggle={e => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[12px] text-[var(--muted)] hover:text-[var(--text)] [&::-webkit-details-marker]:hidden">
        <ChevronRight size={12} className="transition-transform group-open:rotate-90" aria-hidden="true" />
        {i18nT('pages.mcpManagement.details')}
      </summary>
      <p className="mt-1.5 break-words pl-4 text-[12px] leading-relaxed text-[var(--muted)] [overflow-wrap:anywhere]">
        {i18nT('pages.mcpManagement.details_used_by', { agents: server.agents.join(', ') })}
      </p>
    </details>
  )
}

/** The id of a row's server-name element, so a button can name its server. */
export function serverNameId(name: string): string {
  return `mcp-row-name-${name}`
}

/** A labelled button's size and focus ring, shared by every button on the page. */
export const SHARE_BUTTON_CLASS =
  'shrink-0 px-3 py-1.5 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]'

/**
 * A row's on/off control. The words and the track sit inside one
 * `<button role="switch">`, so the whole label is the click target, it takes
 * keyboard focus, and hover and focus show on all of it. Never a bare track.
 */
function ShareSwitch({
  on,
  disabled,
  labelId,
  labelledBy,
  describedBy,
  onClick,
}: {
  on: boolean
  disabled: boolean
  labelId: string
  labelledBy: string
  describedBy?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onClick}
      className="inline-flex shrink-0 items-center gap-2.5 rounded-lg border border-[var(--border)] px-3 py-1.5 text-[13px] font-medium text-[var(--text)] transition-colors hover:bg-[var(--hover)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span id={labelId}>{i18nT('pages.mcpManagement.share_toggle')}</span>
      <span
        aria-hidden="true"
        className={[
          'relative inline-flex h-[20px] w-[34px] shrink-0 items-center rounded-full transition-colors',
          on ? 'bg-accent' : 'bg-[var(--border-strong,var(--border))]',
        ].join(' ')}
      >
        {/* The knob rides on the accent fill, so it keeps one light face in every theme. */}
        <span
          className={[
            'absolute h-[16px] w-[16px] rounded-full bg-white shadow transition-all',
            on ? 'left-[16px]' : 'left-[2px]',
          ].join(' ')}
        />
      </span>
    </button>
  )
}

/**
 * The one control a row offers: an "Enable MCP sharing" switch.
 *
 * Turning it on is one click: the page reads the launch and, if it can be
 * approved, writes the share carrying its `expected_launch` (see
 * `McpManagement`). Turning it off stops sharing, or takes the mark off while
 * page-wide sharing is off. A row whose launch was refused runs nothing
 * shared, so its switch reads off, beside an "Approve update" button carrying
 * the refused launch's own `expected_launch`; turning the switch on sends the
 * same pinned write. A refused launch with no approvable identity offers no
 * button and says why, and its switch takes the leftover mark off.
 */
export function ShareAction({
  server,
  refusal,
  busy,
  reading,
  supported,
  describedBy,
  onShare,
  onConfirm,
  onStop,
}: {
  server: McpManagedServer
  /** Set when the gateway refused this shared server's current launch. */
  refusal?: LaunchRefusal
  busy: boolean
  /** True while this row's press is reading the launch it will pin. */
  reading: boolean
  supported: boolean
  describedBy?: string
  onShare: () => void
  onConfirm: (expectedLaunch: string) => void
  onStop: () => void
}) {
  if (!server.can_stub) return null
  // Each control's accessible name is its visible label followed by the row's
  // server name, read from the page itself, so a voice command for the label
  // on screen matches it.
  const labelId = `mcp-share-label-${server.name}`
  const approveId = `mcp-approve-label-${server.name}`
  const rowName = serverNameId(server.name)
  // On, the switch always turns off. Off, it turns on only where sharing works.
  const toggle = (
    <ShareSwitch
      on={server.stub}
      disabled={busy || (!server.stub && (!supported || reading))}
      labelId={labelId}
      labelledBy={`${labelId} ${rowName}`}
      describedBy={describedBy}
      onClick={server.stub ? onStop : onShare}
    />
  )
  if (server.stub && refusal) {
    const approvable = launchIsApprovable(refusal)
    // A server that reached the list without a press was never approved, so
    // its first approval is not an "update".
    const firstApproval = refusal.reason === 'added_outside_dashboard'
    // A refused row runs nothing shared, so its switch reads off. Turning it on
    // approves the refused launch, the same write as the button beside it. With
    // no approvable identity there is nothing to approve: the switch then takes
    // the leftover mark off, so it can never be approved by accident later.
    const refusedToggle = (
      <ShareSwitch
        on={false}
        disabled={busy || (approvable && !supported)}
        labelId={labelId}
        labelledBy={`${labelId} ${rowName}`}
        describedBy={describedBy}
        onClick={approvable ? () => onConfirm(refusal.expected_launch as string) : onStop}
      />
    )
    return (
      <div className="flex w-full min-w-0 flex-wrap items-center gap-2 @2xl:w-auto @2xl:justify-end">
        {refusedToggle}
        {approvable ? (
          <Btn
            type="button"
            primary
            className={SHARE_BUTTON_CLASS}
            disabled={busy || !supported}
            aria-labelledby={`${approveId} ${rowName}`}
            aria-describedby={describedBy}
            onClick={() => onConfirm(refusal.expected_launch as string)}
          >
            <Check size={14} aria-hidden="true" />
            <span id={approveId}>
              {i18nT(firstApproval ? 'pages.mcpManagement.approve_button' : 'pages.mcpManagement.new_command_button')}
            </span>
          </Btn>
        ) : null}
        {/* No approvable identity means no button; the row's hint says why. */}
      </div>
    )
  }
  return (
    <div className="flex w-full min-w-0 flex-col items-start gap-1 @2xl:w-auto @2xl:items-end">
      {toggle}
      {reading && (
        <span role="status" className="text-[12px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.share_checking')}
        </span>
      )}
    </div>
  )
}
