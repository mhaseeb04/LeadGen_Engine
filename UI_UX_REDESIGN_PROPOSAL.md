# LeadGen Engine — UI/UX Redesign Proposal

## Objective

Refresh the operator dashboard into a commercial-grade lead operations workspace while preserving the current backend APIs, payloads, data fields, DOM contracts, and campaign workflow. The redesign will be implemented as a frontend-only modernization of the existing vanilla HTML/CSS/JavaScript dashboard.

## Current Interface Audit

The existing dashboard has a strong neon/glass visual foundation, but it currently feels closer to a prototype command center than a polished production SaaS product. The main issues are dense spacing, inconsistent visual hierarchy, emoji-led iconography, limited navigation context, an oversized campaign form, weak mobile composition, a table that requires too much scanning, and a review drawer that presents generated-email failures as raw technical text. The live deployment and supplied screenshots also show that the primary action hierarchy is not sufficiently clear: loading, exporting, sending, running, reviewing, and approving all compete for attention.

The repository exposes a static dashboard at `dashboard/index.html` with behavior split between `dashboard/js/app.js` and `dashboard/js/campaign.js`. The redesign will retain the existing identifiers and handlers, including `campaign-query`, `campaign-state`, `campaign-city`, `category-picker`, `force-refresh-toggle`, `btn-run-campaign`, `leads-tbody`, `review-panel`, `review-subject`, `review-body`, `review-demo-url`, `btn-regenerate`, and `btn-approve`. No backend route, request payload, authentication header, API base, or CSV data contract will be changed.

## Proposed Product Direction

The new interface will be positioned as **LeadGen OS**, a focused lead-operations workspace for discovering, auditing, reviewing, and dispatching outreach. The visual language will move to a refined light theme: warm white page surfaces, cool gray panels, charcoal typography, crisp borders, soft shadows, and subtle blue-gray depth. Electric cyan will remain the primary action accent, violet will identify AI assistance, mint will identify verified states, and amber will be reserved for attention states. The interface will use a restrained icon set, stronger typography, clearer spacing, and a consistent elevation model instead of relying on repeated glow effects.

## Proposed Layout

| Area | Proposed experience | Functional preservation |
| --- | --- | --- |
| Global shell | Persistent left sidebar with compact brand mark, Campaigns, Triage Queue, Settings, support, and operator profile/status. On narrow screens it becomes a top bar with a slide-over navigation drawer. | Existing single-page dashboard remains the active route; navigation is visual context only unless a future route exists. |
| Header | Page title, campaign status, last sync context, and a compact action group. The primary CTA is Run Campaign; import/export/send become secondary utilities in a menu or grouped toolbar. | Existing `importCSV()`, `exportVerifiedCSV()`, and `sendApprovedEmails()` handlers remain attached. |
| Campaign composer | A focused “Launch a campaign” workspace with a natural-language query as the hero input, AI Fill as a clearly labeled assist action, and advanced filters in a collapsible section. Categories become searchable selectable chips with selected-count feedback. | Existing inputs, category picker, force-refresh toggle, progress elements, and `runCampaign()` behavior remain intact. |
| Progress state | A compact live status rail showing current phase, progress, cached/fresh mode, and result count. | Existing campaign polling and progress IDs are preserved. |
| KPI strip | Four cards with small trend/context labels, strong numerals, semantic color, and consistent icon containers. | Existing `stat-total`, `stat-noweb`, `stat-upgrade`, and `stat-verified` elements remain unchanged. |
| Triage workspace | A toolbar with search/filter/sort affordances, a clearer queue header, sticky table header, improved row density, status badges, and an explicit review CTA. On mobile, rows become stacked lead cards rather than an overflowing table. | Existing `leads-tbody`, `openReviewPanel(idx)`, strategy badges, and approval states remain supported. |
| Review drawer | A better structured “Review outreach” drawer: prospect summary, opportunity signal, editable subject, editable email body, generation status, demo link, and clear footer actions. Technical errors become human-readable alert blocks with retry guidance while preserving the underlying response text where useful. | Existing drawer IDs and calls to `generateEmailForLead()`, `regenerateEmail()`, `approveCurrentLead()`, and `closeReviewPanel()` remain intact. |
| Notifications | More legible toast notifications with success/error/info variants, dismiss controls, and reduced duplicate noise. | Existing `showToast()` utility remains the source of notifications. |

## Visual System

| Token group | Direction |
| --- | --- |
| Typography | Inter for UI and display text; JetBrains Mono only for URLs, scores, audit snippets, and system status. Larger page headings, shorter labels, and sentence case instead of all-caps everywhere. |
| Color | `#F6F8FB` page background, `#FFFFFF` elevated surfaces, `#EEF2F6` controls, charcoal `#17202D` primary text, slate `#667085` secondary text, cyan `#0EA5C6` for primary actions, violet `#7357D9` for AI, mint `#168A5B` for verified, amber `#B97808` for warnings, red `#C43D52` for destructive/error states. |
| Shape | 12–16px surface radius, 10px controls, pill badges only for status/category semantics. |
| Elevation | Soft layered shadows and subtle borders; gradients reserved for primary/AI buttons and selected states. |
| Motion | 150–220ms transitions, drawer fade/slide, subtle hover/pressed feedback, and reduced-motion support. No distracting infinite animation in the work area. |
| Accessibility | Visible keyboard focus, minimum contrast targets, semantic buttons/labels, no color-only status encoding, appropriate dialog semantics, and mobile touch targets. |

## Key UX Improvements

The search query will become the most prominent starting point because it communicates the product’s natural-language advantage. The filters will be progressive rather than competing with the query. The campaign action will display a clear readiness state, and the force-refresh control will be explained in plain language rather than presented as a low-level toggle.

The queue will prioritize decision-making. Each lead row will expose the business name, contact context, opportunity type, audit severity, and one obvious Review action. Approved leads will remain visually present but clearly de-emphasized. Empty, loading, error, and populated states will each receive intentional layouts instead of inline placeholder text.

The review drawer will be redesigned around an operator’s workflow: understand the opportunity, edit the draft, inspect the demo link, regenerate if necessary, and approve. API/model failures will be presented as an actionable state rather than a large raw error dump. The existing backend error message will remain available in a collapsible technical detail region to aid debugging without overwhelming the operator.

## Reference Principles

The redesign borrows broad, well-established patterns from modern CRM and SaaS operations products: persistent navigation for orientation, a dominant primary workflow, KPI summaries above operational data, progressive disclosure for advanced filters, clear status semantics, responsive table-to-card transformation, and action hierarchy that separates destructive or irreversible operations from routine review. It will be an original LeadGen Engine interface rather than a visual copy of any external product.

## Implementation Boundaries

Only `dashboard/index.html`, `dashboard/css/dashboard.css`, and—if required for purely presentational states—small, non-business-logic additions to `dashboard/js/app.js` or `dashboard/js/campaign.js` will be changed. The backend, API routes, request payloads, authentication, CSV schema, data normalization, demo-site behavior, agency site, and campaign polling logic will not be altered.

## Approval Gate

After approval, implementation will proceed in this order: preserve the existing DOM/API contracts; replace the dashboard shell and visual hierarchy; upgrade responsive behavior; improve review/error presentation without changing API behavior; run local syntax/build checks; exercise the live UI flows with representative CSV data; and verify desktop, tablet, mobile, keyboard focus, empty, loading, success, and error states.

## Proposed Approval Response

The requested light white-gray direction is now incorporated. Reply with **“Approved — implement the redesign”** to authorize the frontend changes. If desired, specify one final preference before implementation, such as **more minimal**, **more premium**, **stronger cyan**, or **warmer neutrals**.

## References

[1]: https://tailadmin.com/blog/crm-dashboard-templates "CRM dashboard template patterns, TailAdmin, 2026"
[2]: https://www.highspot.com/blog/sales-dashboards/ "Sales dashboard information hierarchy and pipeline visibility, Highspot"
[3]: https://taqwah.agency/blog/saas-admin-panel-design-guide "SaaS admin panel UX principles: hierarchy, progressive disclosure, and responsiveness"
