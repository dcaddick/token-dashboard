import { api, fmt } from '/web/app.js';
import { lineChart } from '/web/charts.js';

const RANGES = [
  { key: '90d', label: '90d', days: 90 },
  { key: '180d', label: '180d', days: 180 },
  { key: '1y', label: '1y', days: 365 },
  { key: 'all', label: 'All', days: null },
];
const METRICS = [
  { key: 'workload', label: 'Workload' },
  { key: 'billable', label: 'Billable' },
];
const GROUPS = [
  { key: 'provider', label: 'Providers' },
  { key: 'model', label: 'Models' },
];
const MAX_HEATMAP_DAYS = 365 * 5;

function readState() {
  const query = new URLSearchParams((location.hash.split('?')[1] || ''));
  const range = RANGES.find(item => item.key === query.get('range')) || RANGES[2];
  const metric = METRICS.find(item => item.key === query.get('metric')) || METRICS[0];
  const group = GROUPS.find(item => item.key === query.get('group')) || GROUPS[0];
  return { range, metric, group };
}

function writeState(range, metric, group) {
  const query = new URLSearchParams({ range, metric, group });
  location.hash = `#/burn?${query}`;
}

function localIso(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function parseDay(day) {
  return new Date(`${day}T00:00:00`);
}

function addDays(date, count) {
  const next = new Date(date);
  next.setDate(next.getDate() + count);
  return next;
}

function rangeStart(range, daily) {
  if (range.days) return addDays(new Date(), -(range.days - 1));
  const first = daily.map(row => row.day).sort()[0];
  return first ? parseDay(first) : new Date();
}

function dateLabel(day) {
  if (!day) return 'No activity';
  return parseDay(day).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
  });
}

function providerLabel(provider) {
  const labels = { claude: 'Anthropic', codex: 'OpenAI' };
  if (labels[provider]) return labels[provider];
  return provider ? provider.charAt(0).toUpperCase() + provider.slice(1) : '';
}

function accuracyLabel(accuracy) {
  return accuracy === 'exact' ? 'exact local usage' : `${accuracy || 'unknown'} usage`;
}

function controls(items, selected, attr, label) {
  return `
    <div class="range-tabs" role="group" aria-label="${label}">
      ${items.map(item => `
        <button type="button" data-${attr}="${item.key}" class="${item.key === selected ? 'active' : ''}" aria-pressed="${item.key === selected}">
          ${item.label}
        </button>`).join('')}
    </div>`;
}

function heatLevel(value, maximum) {
  if (!value || !maximum) return 0;
  return Math.max(1, Math.ceil((Math.log1p(value) / Math.log1p(maximum)) * 5));
}

function buildDays(start, end) {
  const days = [];
  for (let cursor = new Date(start); cursor <= end; cursor = addDays(cursor, 1)) {
    days.push(localIso(cursor));
  }
  return days;
}

function laneMarkup({ name, detail, total, accuracy, values, days, gridDays, maximum, metric }) {
  const safeName = fmt.htmlSafe(name);
  const cells = gridDays.map(day => {
    if (!day) return '<span class="burn-cell outside" aria-hidden="true"></span>';
    const tokens = values.get(day) || 0;
    const level = heatLevel(tokens, maximum);
    const tooltip = `${dateLabel(day)} · ${name} · ${metric} · ${fmt.int(tokens)} tokens`;
    if (!tokens) return '<span class="burn-cell level-0" aria-hidden="true"></span>';
    return `<span class="burn-cell level-${level}" role="img" tabindex="0" title="${fmt.htmlSafe(tooltip)}" aria-label="${fmt.htmlSafe(tooltip)}"></span>`;
  }).join('');

  return `
    <section class="burn-lane">
      <div class="burn-lane-meta">
        <strong>${safeName}</strong>
        <span>${fmt.compact(total)}</span>
        ${detail ? `<small>${fmt.htmlSafe(detail)}</small>` : ''}
        ${accuracy ? `<small>${fmt.htmlSafe(accuracyLabel(accuracy))}</small>` : '<small>combined providers</small>'}
      </div>
      <div class="burn-grid" style="--burn-weeks:${Math.max(1, gridDays.length / 7)}" aria-label="${safeName} daily token usage">
        ${cells}
      </div>
    </section>`;
}

function calendarMarkup(summary, range, metric) {
  const end = new Date();
  const allStart = rangeStart(range, summary.daily);
  const capStart = addDays(end, -(MAX_HEATMAP_DAYS - 1));
  const isCapped = !range.days && allStart < capStart;
  const start = isCapped ? capStart : allStart;
  const days = buildDays(start, end);
  const leading = Array(start.getDay()).fill(null);
  const trailing = Array((7 - ((leading.length + days.length) % 7)) % 7).fill(null);
  const gridDays = [...leading, ...days, ...trailing];

  const byLane = new Map(summary.lanes.map(lane => [lane.key, new Map()]));
  const totals = new Map();
  summary.daily.forEach(row => {
    if (!byLane.has(row.lane)) byLane.set(row.lane, new Map());
    byLane.get(row.lane).set(row.day, row.tokens);
    totals.set(row.day, (totals.get(row.day) || 0) + row.tokens);
  });
  const maximum = Math.max(0, ...totals.values(), ...summary.daily.map(row => row.tokens));
  const lanes = [
    { name: 'Total', total: summary.total, accuracy: null, values: totals },
    ...summary.lanes.map(lane => ({
      name: lane.model ? fmt.burnModel(lane.provider, lane.model) : lane.label,
      detail: lane.model ? providerLabel(lane.provider) : null,
      total: lane.tokens,
      accuracy: lane.accuracy,
      values: byLane.get(lane.key) || new Map(),
    })),
  ];

  return `
    <div class="burn-calendar-scroll">
      <div class="burn-calendar" style="--burn-weeks:${Math.max(1, gridDays.length / 7)}">
        ${lanes.map(lane => buildLane({ ...lane, days, gridDays, maximum, metric })).join('')}
      </div>
    </div>
    <div class="burn-calendar-foot">
      <span>${dateLabel(days[0])} – ${dateLabel(days[days.length - 1])}</span>
      ${isCapped ? '<span>All-time totals and peak days remain complete; heatmap shows the latest 5 years.</span>' : ''}
      <span class="burn-legend">Log color <i class="level-0"></i><i class="level-1"></i><i class="level-2"></i><i class="level-3"></i><i class="level-4"></i><i class="level-5"></i></span>
    </div>`;
}

function buildLane(options) {
  return laneMarkup(options);
}

function peakRows(summary) {
  return summary.peak_days.map(item => {
    const labels = new Map(summary.lanes.map(lane => [
      lane.key, lane.model ? fmt.burnModel(lane.provider, lane.model) : lane.label,
    ]));
    const contributions = Object.entries(item.lanes)
      .sort((a, b) => b[1] - a[1])
      .map(([lane, tokens]) => `<span><b>${fmt.htmlSafe(labels.get(lane) || lane)}</b> ${fmt.compact(tokens)}</span>`)
      .join('');
    return `
      <tr>
        <td class="mono">${dateLabel(item.day)}</td>
        <td class="num">${fmt.compact(item.tokens)}</td>
        <td><div class="burn-contributions">${contributions}</div></td>
      </tr>`;
  }).join('') || '<tr><td colspan="3" class="muted">No usage in this range</td></tr>';
}

export default async function (root) {
  const { range, metric, group } = readState();
  const since = range.days ? localIso(addDays(new Date(), -(range.days - 1))) : null;
  const query = new URLSearchParams({ metric: metric.key, group: group.key });
  if (since) query.set('since', since);
  const summary = await api(`/api/burn?${query}`);

  root.innerHTML = `
    <div class="burn-head">
      <div>
        <h2>Token burn</h2>
        <p>Daily token volume across local AI providers. Workload includes all model-processed tokens; billable removes discounted cache reads.</p>
        <p class="burn-accuracy-note"><b>Exact</b> usage comes from local token counters. <b>Estimated</b> usage is reconstructed where exact provider totals are unavailable.</p>
      </div>
      <div class="burn-controls">
        ${controls(GROUPS, group.key, 'group', 'Burn grouping')}
        ${controls(METRICS, metric.key, 'metric', 'Token metric')}
        ${controls(RANGES, range.key, 'range', 'Date range')}
      </div>
    </div>

    <div class="row cols-3 burn-kpis">
      <div class="card kpi"><div class="label">${metric.label} in view</div><div class="value big" title="${fmt.int(summary.total)} tokens">${fmt.compact(summary.total)}</div><div class="sub">${range.days ? `last ${range.days} days` : 'all recorded usage'}</div></div>
      <div class="card kpi"><div class="label">Day to date</div><div class="value big" title="${fmt.int(summary.day_to_date)} tokens">${fmt.compact(summary.day_to_date)}</div><div class="sub">${dateLabel(localIso(new Date()))}</div></div>
      <div class="card kpi"><div class="label">Peak day</div><div class="value big" title="${fmt.int(summary.peak_day.tokens)} tokens">${fmt.compact(summary.peak_day.tokens)}</div><div class="sub">${dateLabel(summary.peak_day.day)}</div></div>
    </div>

    <section class="card burn-weekly">
      <div class="burn-section-head"><div><h3>Weekly total</h3><span class="muted">${summary.lanes.length} ${group.label.toLowerCase()} lane${summary.lanes.length === 1 ? '' : 's'} in view</span></div></div>
      <div id="ch-burn-weekly"></div>
    </section>

    <section class="card burn-heatmap">
      <div class="burn-section-head">
        <div><h3>Daily token burn</h3><span class="muted">Darker cells represent more tokens on a logarithmic scale.</span></div>
      </div>
      ${calendarMarkup(summary, range, metric.label)}
    </section>

    <section class="card burn-peaks">
      <div class="burn-section-head"><div><h3>Peak days</h3><span class="muted">Highest combined token days in this range.</span></div></div>
      <div class="burn-table-scroll">
        <table>
          <thead><tr><th>Date</th><th class="num">Burn</th><th>Provider contribution</th></tr></thead>
          <tbody>${peakRows(summary)}</tbody>
        </table>
      </div>
    </section>`;

  root.querySelectorAll('[data-range]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.range === range.key));
    button.addEventListener('click', () => writeState(button.dataset.range, metric.key, group.key));
  });
  root.querySelectorAll('[data-metric]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.metric === metric.key));
    button.addEventListener('click', () => writeState(range.key, button.dataset.metric, group.key));
  });
  root.querySelectorAll('[data-group]').forEach(button => {
    button.addEventListener('click', () => writeState(range.key, metric.key, button.dataset.group));
  });

  lineChart(document.getElementById('ch-burn-weekly'), {
    x: summary.weekly.map(item => item.week),
    series: [{ name: metric.label, data: summary.weekly.map(item => item.tokens), color: '#3FB68B' }],
  });
}
