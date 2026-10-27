import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, ExternalLink, ListChecks, RefreshCw, Server as ServerIcon } from 'lucide-react'
import {
  api,
  ApiError,
  type McpManagedServer,
  type McpMeasureProgress,
} from '../../api/client'
import {
  Tabs,
  TabsContent,
  TabsCount,
  TabsList,
  TabsTrigger,
  type TabItem,
} from '../../components/ui/tabs'
import { TABS_RAIL_ROW_CLASS } from '../../components/ui/tabsPill'
import { Btn } from '../../components/ui'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog'
import ErrorNotice from '../../components/ErrorNotice'
import { splitOnPlaceholder } from '../../lib/splitOnPlaceholder'
import { i18nT } from '../../i18n/t'
import {
  DOCS_URL,
  type GatewayStatus,
  launchIsApprovable,
  launchPreviewErrorKey,
  changedHintKey,
  stubErrorKey,
  type McpView,
  rowState,
  stateLabelKey,
  CONTRARY_STRENGTHS,
  sharedWithoutSupport,
  waitForMeasurePass,
} from './mcpManagementModel'
import { ServerDetails, serverNameId, SHARE_BUTTON_CLASS, ShareAction } from './McpManagementLaunchReview'
import { AssessmentView } from './McpManagementAssessment'

/**
 * MCP Management: one labelled switch per server, and what it buys.
 *
 * Each row's "Enable MCP sharing" switch is one `<button role="switch">` whose
 * words are part of the target. Turning it on is one click: it reads the
 * launch the server runs right then and, if that launch can be approved,
 * writes the share carrying its `expected_launch`. Page-wide sharing stays its
 * own switch, which asks first when it turns on. The page shows no launch
 * command anywhere; the compare-and-set on `expected_launch` is what pins the
 * approval to the exact launch the gateway resolved. When the command, args or
 * declared env later change, the row reads "Paused · command changed" and
 * waits for "Approve update", which pins the refused launch the same way.
 * Turning the switch off stops sharing. "Enable all shareable" lists the
 * servers by name first and shares only on the button under that list.
 *
 * The second sub-view, "Sharing assessment", is read-only evidence for anyone
 * who wants to know why a server is or is not a good fit. It changes nothing.
 */

function Switch({
  on,
  disabled,
  onClick,
  label,
  describedBy,
}: {
  on: boolean
  disabled?: boolean
  onClick: () => void
  label: string
  describedBy?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onClick}
      className={[
        'relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full transition-colors',
        on ? 'bg-accent' : 'bg-[var(--border-strong,var(--border))]',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          // The knob rides ON the accent fill, so it needs the same light face in
          // every theme; there is no token for that pairing.
          'absolute h-[18px] w-[18px] rounded-full bg-white shadow transition-all',
          on ? 'left-[18px]' : 'left-[2px]',
        ].join(' ')}
      />
    </button>
  )
}

export function McpManagement() {
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // A change that waits for a restart is information, not a failure.
  const [restartNotice, setRestartNotice] = useState<string | null>(null)
  const [resolveNotice, setResolveNotice] = useState<string | null>(null)
  const [resolveError, setResolveError] = useState<string | null>(null)

  const statusQ = useQuery<GatewayStatus>({
    queryKey: ['mcpGatewayStatus'],
    queryFn: () => api.mcpGatewayStatus(),
  })
  const serversQ = useQuery<{ servers: McpManagedServer[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers(),
  })

  const status = statusQ.data
  const servers = useMemo(() => serversQ.data?.servers ?? [], [serversQ.data])
  const stubCount = useMemo(() => servers.filter(s => s.stub).length, [servers])
  // Marked servers the assessment argues against: they start the moment
  // page-wide sharing turns on, so the page names them before that press.
  // Marked servers that turning sharing on actually shares: not one waiting
  // for an approval and not one the rewriter keeps unshared for its env.
  const willShare = useMemo(
    () =>
      servers.filter(
        s => s.stub && !status?.launch_refused?.[s.name] && s.pooling_blocked_by_env !== true,
      ),
    [servers, status?.launch_refused],
  )
  const contraryMarked = useMemo(
    () =>
      willShare
        .filter(s => s.recommendation && CONTRARY_STRENGTHS.has(s.recommendation.strength))
        .map(s => s.name),
    [willShare],
  )
  // The header counts the rows whose chip reads "Shared": a row waiting for its
  // new command, or one paused because sharing is off, is not counted.
  const switchedOnCount = useMemo(
    () =>
      servers.filter(s => rowState(s, !!status?.enabled, status?.launch_refused) === 'shared').length,
    [servers, status],
  )
  const supported = status?.supported ?? true

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
    void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
  }

  const [confirmSharing, setConfirmSharing] = useState(false)

  const setSharing = useMutation({
    mutationFn: (enabled: boolean) => api.mcpGatewayEnable(enabled),
    // Enabling can persist and still leave sharing unreachable; `ping_ok` says so.
    onSuccess: (res, enabled) => {
      invalidate()
      if (enabled && !res.ping_ok) setError(i18nT('pages.mcpManagement.sharing_not_live'))
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.sharing_failed'))
    },
  })

  // The write half of a share. Sharing records the exact launch the server runs
  // and marks it shared; the endpoint refuses the classes that can never be
  // shared. Every stub=true carries the `expected_launch` that was read, so a
  // launch that moved in between is refused rather than approved. It never
  // turns page-wide sharing on by itself: that moves every marked server at
  // once. A server marked while sharing is off opens the switch's own
  // confirm, so the press reaches sharing in the same flow, still asked first.
  // The confirm counts and names the marked servers, so it opens only once the
  // rows a write changed have been read back, and only if that fresh status
  // still has sharing off (another tab may have turned it on). A failed read
  // keeps it closed rather than asking about a list that is not the current one.
  const askToTurnOnSharing = async () => {
    const reads = await Promise.allSettled([
      qc.refetchQueries({ queryKey: ['mcpGatewayStatus'] }, { throwOnError: true }),
      qc.refetchQueries({ queryKey: ['mcpGatewayServers'] }, { throwOnError: true }),
    ])
    const fresh = qc.getQueryData<GatewayStatus>(['mcpGatewayStatus'])
    if (reads.every(r => r.status === 'fulfilled') && fresh && !fresh.enabled) setConfirmSharing(true)
  }

  const setStub = useMutation({
    mutationFn: ({ name, stub, expectedLaunch }: { name: string; stub: boolean; expectedLaunch?: string }) =>
      api.mcpGatewaySetStub(name, stub, expectedLaunch),
    onSuccess: async (res, vars) => {
      invalidate()
      if (vars.stub && supported && res?.sharing_on !== true && !status?.enabled) {
        void askToTurnOnSharing()
      }
      if (res && res.restart_required) {
        setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      } else if (res && res.applied === false) {
        setError(i18nT('pages.mcpManagement.stub_not_live'))
      }
    },
    onError: (err, vars) => {
      invalidate()
      setError(i18nT(stubErrorKey(err), { name: vars.name }))
    },
  })

  // The read half, and the only gate: one click reads the launch and, if it can
  // be approved, writes the share straight away carrying its identity. An
  // unapprovable launch turns nothing on and says so; a read failure (a separate
  // table from the write's) likewise reports that nothing was turned on.
  const previewLaunch = useMutation({
    mutationFn: (name: string) => api.mcpGatewayLaunchPreview(name),
    onSuccess: (preview, name) => {
      if (launchIsApprovable(preview)) {
        setStub.mutate({ name, stub: true, expectedLaunch: preview.expected_launch as string })
      } else {
        setError(i18nT('pages.mcpManagement.approval_not_approvable_here'))
      }
    },
    onError: (err, name) => {
      setError(i18nT(launchPreviewErrorKey(err), { name }))
    },
  })

  const resolveRefresh = useMutation({
    mutationFn: () => api.mcpResolveRefresh(),
    onSuccess: res => {
      invalidate()
      if (!res.ok) {
        setResolveNotice(i18nT('pages.mcpManagement.resolve_no_targets'))
        return
      }
      const ready = res.ready?.length ?? 0
      // Every install failing must not read as "already up to date".
      const failed = Object.values(res.resolved ?? {}).filter(state => state === 'error').length
      if (failed > 0) {
        setResolveError(
          ready > 0
            ? i18nT('pages.mcpManagement.resolve_partly_ready', {
                ready: String(ready),
                failed: String(failed),
              })
            : i18nT('pages.mcpManagement.resolve_all_failed', { failed: String(failed) }),
        )
        return
      }
      setResolveNotice(
        ready > 0
          ? i18nT('pages.mcpManagement.resolve_ready', { ready: String(ready) })
          : i18nT('pages.mcpManagement.resolve_none_ready'),
      )
    },
    onError: err => {
      invalidate()
      // 409 is a pass already running elsewhere: information, not a failure.
      if (err instanceof ApiError && err.status === 409) {
        setResolveNotice(i18nT('pages.mcpManagement.resolve_already_running'))
        return
      }
      setError(i18nT('pages.mcpManagement.resolve_failed'))
    },
  })

  const busy = setStub.isPending || setSharing.isPending || previewLaunch.isPending
  // One count shared by the tab badge and the assessment banner.
  const unsupportedCount = useMemo(
    () =>
      servers.filter(s => sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)).length,
    [servers, status?.enabled, status?.launch_refused],
  )
  const canEnableSharing = supported && stubCount > 0

  // Rows the measurement pass would act on, including an older gateway's row
  // with no verdict and a handshake that did not reproduce (re-measured every pass).
  const unmeasuredCount = useMemo(
    () =>
      servers.filter(
        s =>
          !s.recommendation ||
          s.recommendation.strength === 'unknown' ||
          s.recommendation.reasons.some(r => r.code === 'handshake_not_reproducible'),
      ).length,
    [servers],
  )

  const { data: measureProgress } = useQuery<McpMeasureProgress>({
    queryKey: ['mcp-measure-progress'],
    queryFn: () => api.mcpMeasureProgress(),
    enabled: false,
  })
  // Deliberately NOT an eligibility test — only "is there anything worth asking
  // about". Eligibility is the server's to decide, and a second copy of that rule
  // here could disagree with it: too strict and the button is dead while servers
  // do qualify, too loose and it promises work that gets skipped. Gating on
  // stubbable-and-not-yet-stubbed keeps the control honest either way, because a
  // press that finds nothing qualifying reports exactly that.
  const stubbableNames = useMemo(
    () => servers.filter(s => s.can_stub && !s.stub).map(s => s.name),
    [servers],
  )

  // "Share all": measure the unmeasured first, then read each candidate's launch
  // and list the candidates by name. Nothing is shared until the person presses
  // the button under the list, and each share is pinned to the launch read for
  // it here. No command is shown.
  const [allPlan, setAllPlan] = useState<Array<{ name: string; expected: string }> | null>(null)
  // Candidates whose launch could not be approved: named inside the list, so a
  // server left out does not just vanish from it.
  const [allSkipped, setAllSkipped] = useState<string[]>([])
  const prepareAll = useMutation({
    mutationFn: async () => {
      if (unmeasuredCount > 0) {
        await api.mcpMeasureStart()
        if (!(await waitForMeasurePass(qc))) return { pending: true as const }
      }
      // Re-read directly: the shared QueryClient never treats its cache as stale.
      // The fresh rows go back into the cache, so the assessment view shows the
      // verdicts this pass just measured, not the ones read before it.
      const fresh = await api.mcpGatewayServers()
      qc.setQueryData(['mcpGatewayServers'], fresh)
      // Not an eligibility test: each write sends `resolve_eligibility`, and the
      // server's `_stub_eligibility` decides and reports what it skipped.
      const candidates = (fresh?.servers ?? [])
        .filter(s => s.can_stub && !s.stub)
        .map(s => s.name)
      const plan: Array<{ name: string; expected: string }> = []
      const unreadable: string[] = []
      const failed: string[] = []
      for (const name of candidates) {
        try {
          const preview = await api.mcpGatewayLaunchPreview(name)
          if (preview && launchIsApprovable(preview)) {
            plan.push({ name, expected: preview.expected_launch as string })
          } else unreadable.push(name)
        } catch {
          // A failed read is an error, not "nothing to share".
          failed.push(name)
        }
      }
      return { pending: false as const, plan, unreadable, failed }
    },
    onSuccess: r => {
      if (r.pending) {
        setNotice(i18nT('pages.mcpManagement.enable_all_still_measuring'))
        return
      }
      if (r.failed.length > 0) {
        setError(i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: r.failed.join(', ') }))
      }
      if (r.plan.length === 0) {
        // Servers whose launch can't be approved are still unshared: name them
        // rather than claiming there is nothing left.
        if (r.unreadable.length > 0) {
          setNotice(i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: r.unreadable.join(', ') }))
        } else if (r.failed.length === 0) {
          setNotice(i18nT('pages.mcpManagement.enable_all_review_nothing'))
        }
        return
      }
      setAllPlan(r.plan)
      setAllSkipped(r.unreadable)
    },
    onError: () => setError(i18nT('pages.mcpManagement.stub_failed')),
  })
  const enableAll = useMutation({
    mutationFn: async (plan: Array<{ name: string; expected: string }>) => {
      let shared = 0
      let skipped = 0
      const failed: string[] = []
      let restart = false
      // What the writes themselves report, not the status read before them:
      // another tab may have turned sharing on in between.
      let sharingOn = !!status?.enabled
      const skippedNames: string[] = []
      for (const { name, expected } of plan) {
        try {
          const res = await api.mcpGatewaySetStub(name, true, expected, true)
          if (res.stubbed?.includes(name)) shared++
          else {
            skipped++
            skippedNames.push(name)
          }
          if (res.restart_required) restart = true
          if (typeof res.sharing_on === 'boolean') sharingOn = res.sharing_on
        } catch {
          failed.push(name)
        }
      }
      return { shared, skipped, skippedNames, failed, restart, sharingOn }
    },
    onSuccess: r => {
      invalidate()
      setAllPlan(null)
      setAllSkipped([])
      setNotice(
        [
          // With sharing off the writes only mark: say so, then ask about sharing.
          i18nT(
            r.sharingOn
              ? 'pages.mcpManagement.enable_all_review_result_shared'
              : 'pages.mcpManagement.enable_all_review_result_marked',
            { count: r.shared },
          ),
          ...(r.skipped > 0
            ? [i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: r.skippedNames.join(', ') })]
            : []),
        ].join(' '),
      )
      // Failed requests are errors: they go through ErrorNotice, not the status line.
      if (r.failed.length > 0) {
        setError(i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: r.failed.join(', ') }))
      }
      if (!r.sharingOn && r.shared > 0 && supported) void askToTurnOnSharing()
      if (r.restart) setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.stub_failed'))
    },
  })

  const [view, setView] = useState<McpView>('servers')
  const views: Array<TabItem<McpView>> = [
    {
      key: 'servers',
      label: i18nT('pages.mcpManagement.view_servers'),
      icon: <ServerIcon size={14} />,
    },
    {
      key: 'assessment',
      label: i18nT('pages.mcpManagement.view_assessment'),
      icon: <ListChecks size={14} />,
      count: unsupportedCount,
    },
  ]

  return (
    <Tabs
      value={view}
      onValueChange={v => setView(v as McpView)}
      layoutId="mcp-management-view"
      className="min-w-0 space-y-4"
    >
      <div className={TABS_RAIL_ROW_CLASS}>
        <TabsList aria-label={i18nT('pages.mcpManagement.views_aria')}>
          {views.map(v => (
            <TabsTrigger key={v.key} value={v.key}>
              {v.icon}
              <span>{v.label}</span>
              <TabsCount value={v.count} />
            </TabsTrigger>
          ))}
        </TabsList>
      </div>

      <ErrorNotice
        message={statusQ.isError ? i18nT('pages.mcpManagement.status_failed') : null}
        askAgent
      />

      <TabsContent value="assessment" className="min-w-0">
        <AssessmentView
          servers={servers}
          sharingOn={!!status?.enabled}
          refused={status?.launch_refused}
          loading={serversQ.isLoading}
          isError={serversQ.isError}
          onOpenServers={() => setView('servers')}
          unsupportedCount={unsupportedCount}
          unmeasuredCount={unmeasuredCount}
        />
      </TabsContent>
      <TabsContent value="servers" className="min-w-0 space-y-4">
      <p className="max-w-[76ch] text-[13px] leading-relaxed text-[var(--muted)]">
        {/* One key, one sentence, a {{link}} placeholder a translator can move. */}
        {splitOnPlaceholder(i18nT('pages.mcpManagement.lede'), 'link').map((part, i) =>
          part === null ? (
            <a
              key="link"
              href={DOCS_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-[var(--accent)] hover:underline"
            >
              {i18nT('pages.mcpManagement.learn_more')}
              <ExternalLink size={12} />
            </a>
          ) : (
            <span key={i}>{part}</span>
          ),
        )}
      </p>

      <ErrorNotice message={error} askAgent onDismiss={() => setError(null)} />

      {restartNotice && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-lg border border-[var(--accent)] bg-[var(--accent-subtle,transparent)] px-3.5 py-2.5 text-[13px] text-[var(--text)]"
        >
          <RefreshCw size={14} className="mt-0.5 shrink-0 text-[var(--accent)]" aria-hidden="true" />
          <span>{restartNotice}</span>
        </div>
      )}

      {/* Per server: one labelled button is the decision. */}
      <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 border-b border-[var(--border)] px-4 py-2.5">
          <span className="text-[12.5px] text-[var(--muted)]">
            {i18nT('pages.mcpManagement.stubbed_of_total', {
              stubbed: switchedOnCount,
              // Every row below, so the count and the list always agree.
              total: servers.length,
            })}
          </span>
          <Btn
            type="button"
            className={SHARE_BUTTON_CLASS}
            onClick={() => {
              setError(null)
              setNotice(null)
              setRestartNotice(null)
              prepareAll.mutate()
            }}
            disabled={
              busy
              || prepareAll.isPending
              || enableAll.isPending
              || allPlan !== null
              || serversQ.isLoading
              || !supported
              || (stubbableNames.length === 0 && unmeasuredCount === 0)
            }
          >
            {i18nT('pages.mcpManagement.enable_all')}
          </Btn>
          {(prepareAll.isPending || enableAll.isPending || notice) && (
            <span role="status" className="basis-full text-[12.5px] text-[var(--muted)]">
              {prepareAll.isPending && (measureProgress?.running || unmeasuredCount > 0)
                ? i18nT('pages.mcpManagement.assessment.measure_running', {
                    done: measureProgress?.running ? measureProgress.done : 0,
                    total: measureProgress?.running ? measureProgress.total : unmeasuredCount,
                  })
                : prepareAll.isPending
                  ? i18nT('pages.mcpManagement.share_checking')
                  : notice}
            </span>
          )}
        </div>
        {allPlan && (
          <div
            role="region"
            aria-label={i18nT('pages.mcpManagement.share_all_list_aria')}
            className="min-w-0 space-y-3 border-b border-[var(--border)] px-4 py-3 text-[12px] leading-relaxed text-[var(--muted)] [overflow-wrap:anywhere]"
          >
            <p className="font-medium text-[var(--text)]">{i18nT('pages.mcpManagement.share_all_runs')}</p>
            {/* A plain list of names: nothing here can be edited. */}
            <ul className="list-disc space-y-1 pl-5">
              {allPlan.map(item => (
                <li key={item.name} className="font-mono text-[12.5px] text-[var(--text)]">
                  {item.name}
                </li>
              ))}
            </ul>
            {allSkipped.length > 0 && (
              <p>{i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: allSkipped.join(', ') })}</p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Btn
                type="button"
                primary
                className={SHARE_BUTTON_CLASS}
                disabled={enableAll.isPending || busy}
                onClick={() => enableAll.mutate(allPlan)}
              >
                <Check size={14} aria-hidden="true" />
                {i18nT('pages.mcpManagement.share_all_confirm', { count: allPlan.length })}
              </Btn>
              <Btn
                type="button"
                className={SHARE_BUTTON_CLASS}
                disabled={enableAll.isPending}
                onClick={() => {
                  setAllPlan(null)
                  setAllSkipped([])
                  setNotice(null)
                }}
              >
                {i18nT('pages.mcpManagement.share_all_cancel')}
              </Btn>
            </div>
          </div>
        )}
        {/* Rows stack by the list's own width, not the window's: beside the app's
            sidebar a wide window can still leave too little room for the name and
            a row's controls side by side. */}
        <ul className="@container divide-y divide-[var(--border)]">
          {servers.map(s => {
            const state = rowState(s, !!status?.enabled, status?.launch_refused)
            const waiting = state === 'needs_reapproval'
            const refusal = waiting ? status?.launch_refused?.[s.name] : undefined
            const flagged = sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)
            const hint = waiting
              ? i18nT(
                  // No approvable identity: no button offers a share, so say why.
                  refusal && !launchIsApprovable(refusal)
                    ? 'pages.mcpManagement.approval_not_approvable_here'
                    : refusal?.reason === 'added_outside_dashboard'
                    ? 'pages.mcpManagement.reapproval_reason_added_outside_dashboard_unapprovable'
                    : changedHintKey(refusal),
                )
              : state === 'direct_env'
                ? i18nT('pages.mcpManagement.state_direct_env_reason')
                : null
            const hintId = `mcp-share-hint-${s.name}`
            return (
              <li key={s.name} className="px-4 py-3">
                <div className="flex min-w-0 flex-col gap-3 @2xl:flex-row @2xl:items-start">
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <span
                        id={serverNameId(s.name)}
                        className="min-w-0 break-words font-mono text-[13px] text-[var(--text)] [overflow-wrap:anywhere]"
                      >
                        {s.name}
                      </span>
                      <span
                        className={[
                          'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px]',
                          flagged
                            ? 'border border-[var(--danger)] text-[var(--danger)]'
                            : state === 'shared'
                              ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
                              : 'text-[var(--muted)]',
                        ].join(' ')}
                      >
                        {flagged && <AlertTriangle size={11} aria-hidden="true" />}
                        {i18nT(stateLabelKey(state, refusal))}
                      </span>
                    </div>
                    {hint && (
                      <p id={hintId} className="mt-0.5 text-[12.5px] text-[var(--muted)]">
                        {hint}
                      </p>
                    )}
                    <ServerDetails server={s} />
                  </div>
                  <ShareAction
                    server={s}
                    refusal={refusal}
                    busy={busy}
                    reading={previewLaunch.isPending && previewLaunch.variables === s.name}
                    supported={supported}
                    describedBy={hint ? hintId : undefined}
                    onShare={() => {
                      setError(null)
                      setRestartNotice(null)
                      previewLaunch.mutate(s.name)
                    }}
                    onConfirm={expectedLaunch => {
                      setError(null)
                      setRestartNotice(null)
                      setStub.mutate({ name: s.name, stub: true, expectedLaunch })
                    }}
                    onStop={() => {
                      setError(null)
                      setRestartNotice(null)
                      setStub.mutate({ name: s.name, stub: false })
                    }}
                  />
                </div>
              </li>
            )
          })}
          {serversQ.isError && (
            <li className="px-4 py-4">
              <ErrorNotice message={i18nT('pages.mcpManagement.servers_failed')} askAgent />
            </li>
          )}
          {servers.length === 0 && !serversQ.isLoading && !serversQ.isError && (
            <li className="px-4 py-6 text-center text-[13px] text-[var(--muted)]">
              {i18nT('pages.mcpManagement.no_servers')}
            </li>
          )}
        </ul>
        <p className="border-t border-[var(--border)] px-4 py-2.5 text-[12.5px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.open_sessions_note')}
        </p>
      </section>

      {/* Page-wide: the main sharing switch. It asks before it turns on. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-4 py-3">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-[var(--text)]">
              {i18nT('pages.mcpManagement.sharing_label')}
            </div>
            <p id="mcp-sharing-desc" className="mt-0.5 text-[12.5px] text-[var(--muted)]">
              {!supported
                ? i18nT('pages.mcpManagement.unsupported_platform')
                : !status?.enabled && stubCount === 0
                  ? i18nT('pages.mcpManagement.sharing_needs_a_stub')
                  : i18nT('pages.mcpManagement.sharing_description')}
            </p>
            {/* Sharing left ON with no server marked is a switch with no
                observable effect, reachable by unmarking the last server. Name
                it instead of showing a live switch that governs nothing. */}
            {supported && status?.enabled && stubCount === 0 && (
              <p className="mt-1 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.sharing_on_but_nothing_stubbed')}
              </p>
            )}
            {/* The assessment's verdict sits beside the switch too, so it is read
                before the press and not only in the dialog that follows. */}
            {!status?.enabled && contraryMarked.length > 0 && (
              <p className="mt-1 flex items-start gap-1.5 text-[12.5px] text-[var(--danger)]">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                <span className="break-words [overflow-wrap:anywhere]">
                  {i18nT('pages.mcpManagement.sharing_contrary', { names: contraryMarked.join(', ') })}
                </span>
              </p>
            )}
          </div>
          <Switch
            on={!!status?.enabled}
            disabled={busy || statusQ.isLoading || (!status?.enabled && !canEnableSharing)}
            label={i18nT('pages.mcpManagement.sharing_label')}
            describedBy="mcp-sharing-desc"
            onClick={() => {
              setError(null)
              // Turning sharing ON moves every marked server out of the chat
              // sandboxes at once, so it asks first. Turning it OFF only ever
              // narrows, and a confirm on the safe direction trains people to
              // click through the dangerous one.
              if (!status?.enabled) setConfirmSharing(true)
              else setSharing.mutate(false)
            }}
          />
        </div>
      </section>

      {/* Page-wide: install npm servers once so chats start without a download. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-4 py-3">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-[var(--text)]">
              {i18nT('pages.mcpManagement.resolve_label')}
            </div>
            <p id="mcp-resolve-desc" className="mt-0.5 text-[12.5px] text-[var(--muted)]">
              {stubCount === 0
                ? i18nT('pages.mcpManagement.resolve_needs_a_stub')
                : i18nT('pages.mcpManagement.resolve_description')}
            </p>
            {resolveNotice && (
              <p role="status" className="mt-1 text-[12.5px] text-[var(--text)]">
                {resolveNotice}
              </p>
            )}
            <ErrorNotice variant="inline" className="mt-2" message={resolveError} askAgent />
          </div>
          <button
            type="button"
            aria-describedby="mcp-resolve-desc"
            disabled={resolveRefresh.isPending}
            onClick={() => {
              setError(null)
              setResolveNotice(null)
              setResolveError(null)
              resolveRefresh.mutate()
            }}
            className="shrink-0 rounded-lg border border-[var(--border)] px-3 py-1.5 text-[13px] text-[var(--text)] transition-colors hover:bg-[var(--hover)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {resolveRefresh.isPending
              ? i18nT('pages.mcpManagement.resolve_updating')
              : i18nT('pages.mcpManagement.resolve_update_now')}
          </button>
        </div>
      </section>
      </TabsContent>

      <ConfirmSharing
        open={confirmSharing}
        stubCount={willShare.length}
        marked={willShare.map(s => s.name)}
        unsupported={contraryMarked}
        busy={setSharing.isPending || setStub.isPending}
        // A stale or failed list must not be what the press confirms.
        ready={!statusQ.isFetching && !serversQ.isFetching && !statusQ.isError && !serversQ.isError}
        onUnmark={name => setStub.mutate({ name, stub: false })}
        onCancel={() => setConfirmSharing(false)}
        onConfirm={() => {
          setConfirmSharing(false)
          setSharing.mutate(true)
        }}
      />
    </Tabs>
  )
}

function ConfirmSharing({
  open,
  stubCount,
  marked,
  unsupported,
  onUnmark,
  ready,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  stubCount: number
  /** The marked servers this switch starts sharing, by name. */
  marked: string[]
  unsupported: string[]
  /** Leave one warned server out before turning sharing on. */
  onUnmark: (name: string) => void
  /** False while the list it shows is being re-read, or could not be. */
  ready: boolean
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  // Built on the repo's Radix Dialog rather than a bare `<div role="dialog">`:
  // that primitive owns the focus trap, initial focus, Escape-to-dismiss and
  // focus return. Hand-rolling the markup looked identical but let a keyboard
  // user Tab into the page behind the overlay and gave them no way out.
  return (
    <Dialog
      open={open}
      onOpenChange={next => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent maxWidth={520}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.mcpManagement.confirm_title')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <DialogDescription className="text-text">
            {i18nT('pages.mcpManagement.confirm_lede', { count: stubCount })}
          </DialogDescription>
          {/* Named, so the count is not one more number to decode. Data, not
              prose, so no catalog entry. */}
          {marked.length > 0 && (
            <p className="mt-1 break-words font-mono text-[12px] text-[var(--text)] [overflow-wrap:anywhere]">
              {marked.join(', ')}
            </p>
          )}
          {/* The verdict belongs at the decision point, not only after the fact:
              an operator who never opens the assessment view would otherwise
              reach the exact state this page exists to warn about. */}
          {unsupported.length > 0 && (
            <div className="mt-2 flex items-start gap-2 text-[13.5px] text-[var(--danger)]">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
              <div>
                <p>
                  {i18nT('pages.mcpManagement.confirm_unsupported')}
                </p>
                {/* Naming them is the difference between a number the operator has
                    to go hunting for and one they can act on here. Listed in full,
                    so nothing is hidden. Data, not prose, so no catalog entry. */}
                <ul className="mt-1 space-y-1">
                  {unsupported.map(name => (
                    <li key={name} className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[12px]">{name}</span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onUnmark(name)}
                        className="rounded-md border border-[var(--border)] px-2 py-0.5 text-[12px] text-[var(--text)] disabled:opacity-60"
                      >
                        {i18nT('pages.mcpManagement.unmark_button_aria', { name })}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
          <ul className="mt-2.5 list-disc space-y-1.5 pl-5 text-[13.5px] leading-relaxed text-muted">
            <li>{i18nT('pages.mcpManagement.confirm_stateful')}</li>
            <li>{i18nT('pages.mcpManagement.confirm_restart')}</li>
            <li>{i18nT('pages.mcpManagement.confirm_reversible')}</li>
          </ul>
        </DialogBody>
        <DialogFooter className="justify-between">
          <a
            href={DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline"
          >
            {i18nT('pages.mcpManagement.learn_more_docs')}
            <ExternalLink size={12} />
          </a>
          <div className="flex gap-2.5">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border px-3.5 py-2 text-[13.5px] text-text"
            >
              {i18nT('pages.mcpManagement.cancel')}
            </button>
            <button
              type="button"
              autoFocus
              disabled={busy || !ready}
              onClick={onConfirm}
              className="rounded-md bg-accent px-3.5 py-2 text-[13.5px] font-medium text-accent-fg disabled:opacity-60"
            >
              {i18nT('pages.mcpManagement.confirm_turn_on')}
            </button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default McpManagement
