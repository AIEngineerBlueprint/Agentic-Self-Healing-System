import { AlertTriangle, Calculator, CheckCircle } from './icons'

const LABELS = {
  electricity: 'Electricity',
  heating: 'Heating',
  road_travel: 'Road travel',
  rail_travel: 'Rail travel',
  air_travel: 'Air travel',
  diet: 'Diet',
  waste: 'Waste',
}

const COMPONENT_LABELS = {
  grid_electricity: 'grid electricity',
  natural_gas: 'natural gas',
  lpg: 'LPG',
  heating_oil: 'heating oil',
  petrol_car: 'petrol car',
  diesel_car: 'diesel car',
  electric_car: 'electric car',
  two_wheeler: 'two-wheeler',
  bus: 'bus',
  rail: 'rail',
  flight_short_haul: 'short haul',
  flight_medium_haul: 'medium haul',
  flight_long_haul: 'long haul',
  landfilled: 'landfilled',
  recycled: 'recycled',
}

const fmt = (n, d = 0) =>
  Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })

export default function ResultsPanel({ result, error, loading }) {
  if (error) return <ErrorState error={error} />
  if (!result && loading) return <Shell><div className="state"><div className="spinner" style={{ color: 'var(--accent)', width: 22, height: 22 }} /></div></Shell>
  if (!result) return <EmptyState />
  if (result.total_kgco2e <= 0) return <ZeroState result={result} />

  const { comparison: cmp } = result
  const periodWord = result.period === 'monthly' ? 'this month' : 'this year'

  // Scale all three comparison bars against a common maximum so they are
  // visually honest relative to each other.
  const max = Math.max(result.annualised_tco2e, cmp.regional_average_tco2e || 0, cmp.target_tco2e) * 1.08
  const pct = (v) => `${Math.min(100, (v / max) * 100)}%`

  const overTarget = cmp.vs_target_pct > 0

  return (
    <div className="results">
      {/* ---------------------------------------------------------- hero */}
      <section className="panel hero">
        <div className="hero__label">Total footprint · {periodWord}</div>
        <div className="hero__figure">
          <span className="hero__value">{fmt(result.total_kgco2e, 1)}</span>
          <span className="hero__unit">kg CO₂e</span>
        </div>
        <p className="hero__meta">
          Equivalent to <strong>{fmt(result.annualised_tco2e, 2)} tonnes CO₂e</strong> per year
          {result.region_name && <> · {result.region_name}</>}
        </p>

        <div className="compare">
          <Row name="You, annualised" value={result.annualised_tco2e} width={pct(result.annualised_tco2e)} variant="you" />
          {cmp.regional_average_tco2e != null && (
            <Row name={`${result.region_name} average`} value={cmp.regional_average_tco2e} width={pct(cmp.regional_average_tco2e)} variant="region" />
          )}
          <Row name="1.5°C target" value={cmp.target_tco2e} width={pct(cmp.target_tco2e)} variant="target" />
        </div>

        <div className={`verdict verdict--${overTarget ? 'over' : 'under'}`}>
          {overTarget ? (
            <>
              You are <strong>{fmt(Math.abs(cmp.vs_target_pct), 0)}% above</strong> the{' '}
              {cmp.target_label.toLowerCase()}
              {cmp.vs_regional_average_pct != null && (
                <>, and {fmt(Math.abs(cmp.vs_regional_average_pct), 0)}%{' '}
                  {cmp.vs_regional_average_pct > 0 ? 'above' : 'below'} the {result.region_name} average</>
              )}.
            </>
          ) : (
            <>
              You are <strong>within</strong> the {cmp.target_label.toLowerCase()}. That is an
              unusual result — worth checking your inputs are complete.
            </>
          )}
        </div>
      </section>

      {/* ----------------------------------------------------- breakdown */}
      <section className="panel">
        <div className="section__head">
          <h2 className="section__title">Where it comes from</h2>
          <span className="section__note">{result.breakdown.length} categories</span>
        </div>
        <div className="section__body">
          <div className="bars">
            {result.breakdown.map((entry) => {
              const components = Object.entries(entry.components || {}).filter(([, v]) => v > 0.05)
              return (
                <div key={entry.category}>
                  <div className="bar__head">
                    <span className="bar__name">{LABELS[entry.category] || entry.category}</span>
                    <span className="bar__pct">{fmt(entry.percent, 1)}%</span>
                    <span className="bar__val">{fmt(entry.kgco2e, 1)} kg</span>
                  </div>
                  <div className="bar__track">
                    <div
                      className="bar__fill"
                      style={{
                        width: `${entry.percent}%`,
                        background: `var(--c-${entry.category})`,
                      }}
                    />
                  </div>
                  {components.length > 1 && (
                    <div className="bar__components">
                      {components
                        .sort((x, y) => y[1] - x[1])
                        .map(([k, v]) => `${COMPONENT_LABELS[k] || k} ${fmt(v, 1)}`)
                        .join(' · ')}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </section>

      {/* --------------------------------------------------- suggestions */}
      {result.suggestions?.length > 0 && (
        <section className="panel">
          <div className="section__head">
            <h2 className="section__title">Where to start</h2>
            <span className="section__note">Ranked by estimated annual saving</span>
          </div>
          <div className="section__body">
            <div className="suggestions">
              {result.suggestions.map((s, i) => (
                <div className="suggestion" key={s.category}>
                  <span className="suggestion__rank">{i + 1}</span>
                  <div>
                    <h3 className="suggestion__headline">{s.headline}</h3>
                    <p className="suggestion__detail">{s.detail}</p>
                    <div className="suggestion__saving">
                      Saves about {fmt(s.estimated_annual_saving_kgco2e, 0)} kg CO₂e per year
                    </div>
                    <div className="suggestion__basis">{s.basis}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

      <p className="footnote" style={{ padding: 0, margin: 0 }}>
        Calculated {new Date(result.computed_at).toLocaleString()} using emission factor
        dataset <strong>{result.factor_dataset_version}</strong> · reference{' '}
        <code>{result.calculation_id}</code>
      </p>
    </div>
  )
}

function Row({ name, value, width, variant }) {
  return (
    <div className="compare__row">
      <span className="compare__name">{name}</span>
      <div className="compare__track">
        <div className={`compare__fill compare__fill--${variant}`} style={{ width }} />
      </div>
      <span className="compare__value">{fmt(value, 2)} t</span>
    </div>
  )
}

function Shell({ children }) {
  return <div className="results"><section className="panel">{children}</section></div>
}

function EmptyState() {
  return (
    <Shell>
      <div className="state">
        <Calculator className="state__icon" />
        <h2 className="state__title">No calculation yet</h2>
        <p className="state__body">
          Fill in what you used over the period and select “Calculate footprint”.
          Everything runs on published emission factors for your region.
        </p>
      </div>
    </Shell>
  )
}

/* The zero state. A footprint of zero is a legitimate, correct result — not an
 * error — and rendering it properly is precisely what the Scenario 1 defect
 * fails to do. */
function ZeroState({ result }) {
  return (
    <Shell>
      <div className="state state--zero">
        <CheckCircle className="state__icon" />
        <h2 className="state__title">Your footprint is zero</h2>
        <p className="state__body">
          Every activity input was left at zero, so there is nothing to attribute.
          Add your electricity use, travel, or diet to see a breakdown.
        </p>
        <p className="state__body" style={{ marginTop: 10, fontSize: 12.5 }}>
          Dataset {result.factor_dataset_version} · reference <code>{result.calculation_id}</code>
        </p>
      </div>
    </Shell>
  )
}

function ErrorState({ error }) {
  return (
    <Shell>
      <div className="state state--error">
        <AlertTriangle className="state__icon" />
        <h2 className="state__title">We couldn’t complete that calculation</h2>
        <p className="state__body">{error.message}</p>
        <div className="error-detail">
          HTTP {error.status} · {error.code}
        </div>
      </div>
    </Shell>
  )
}
