import { useState } from 'react'
import { Caret } from './icons'

const DIETS = [
  ['high_meat', 'High meat — over 100 g/day'],
  ['medium_meat', 'Medium meat — 50–100 g/day'],
  ['low_meat', 'Low meat — under 50 g/day'],
  ['pescatarian', 'Pescatarian'],
  ['vegetarian', 'Vegetarian'],
  ['vegan', 'Vegan'],
]

// A realistic starting point rather than an empty form. Reviewers see plausible
// numbers immediately, and "Clear all" is the one-click route to the zero state
// that Scenario 1 depends on.
const PRESET = {
  electricity_kwh: 320,
  natural_gas_m3: 12,
  lpg_kg: 0,
  heating_oil_litres: 0,
  petrol_car_km: 800,
  diesel_car_km: 0,
  electric_car_km: 0,
  bus_km: 60,
  two_wheeler_km: 0,
  rail_km: 120,
  flights_short_haul: 1,
  flights_medium_haul: 0,
  flights_long_haul: 0,
  diet: 'medium_meat',
  waste_kg: 40,
  recycling_percent: 30,
}

const EMPTY = Object.fromEntries(
  Object.entries(PRESET).map(([k, v]) => [k, typeof v === 'number' ? 0 : '']),
)

function Num({ label, unit, value, onChange, step = 'any' }) {
  return (
    <div className="field">
      <label className="field__label">{label}</label>
      <div className="control">
        <input
          type="number"
          min="0"
          step={step}
          value={value}
          onChange={(e) => onChange(e.target.value === '' ? 0 : Number(e.target.value))}
        />
        {unit && <span className="control__unit">{unit}</span>}
      </div>
    </div>
  )
}

export default function FootprintForm({ regions, loading, onSubmit }) {
  const [period, setPeriod] = useState('monthly')
  const [region, setRegion] = useState('IN-KA')
  const [a, setA] = useState(PRESET)

  const set = (key) => (value) => setA((prev) => ({ ...prev, [key]: value }))

  function handleSubmit(event) {
    event.preventDefault()
    onSubmit({ period, region, activities: { ...a, diet: a.diet || null } })
  }

  return (
    <form className="panel panel--form" onSubmit={handleSubmit}>
      <div className="form__head">
        <h1 className="form__title">Estimate your footprint</h1>
        <p className="form__hint">
          Enter what you used over the period. Leave anything you don’t know at zero.
        </p>
      </div>

      <div className="form__body">
        <fieldset className="fieldset">
          <legend className="fieldset__legend">Period &amp; region</legend>

          <div className="field">
            <label className="field__label">Reporting period</label>
            <div className="segmented" role="group" aria-label="Reporting period">
              {['monthly', 'annual'].map((p) => (
                <button
                  key={p}
                  type="button"
                  aria-pressed={period === p}
                  onClick={() => setPeriod(p)}
                >
                  {p === 'monthly' ? 'Monthly' : 'Annual'}
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="region">Region</label>
            <div className="control">
              <select id="region" value={region} onChange={(e) => setRegion(e.target.value)}>
                {regions.length === 0 && <option value="IN-KA">Loading…</option>}
                {regions.map((r) => (
                  <option key={r.code} value={r.code}>
                    {r.name} — {r.country}
                  </option>
                ))}
              </select>
              <Caret className="control__caret" />
            </div>
          </div>
        </fieldset>

        <fieldset className="fieldset">
          <legend className="fieldset__legend">Electricity &amp; heating</legend>
          <Num label="Electricity" unit="kWh" value={a.electricity_kwh} onChange={set('electricity_kwh')} />
          <div className="field__row">
            <Num label="Natural gas" unit="m³" value={a.natural_gas_m3} onChange={set('natural_gas_m3')} />
            <Num label="LPG" unit="kg" value={a.lpg_kg} onChange={set('lpg_kg')} />
          </div>
          <Num label="Heating oil" unit="litres" value={a.heating_oil_litres} onChange={set('heating_oil_litres')} />
        </fieldset>

        <fieldset className="fieldset">
          <legend className="fieldset__legend">Road &amp; rail</legend>
          <div className="field__row">
            <Num label="Petrol car" unit="km" value={a.petrol_car_km} onChange={set('petrol_car_km')} />
            <Num label="Diesel car" unit="km" value={a.diesel_car_km} onChange={set('diesel_car_km')} />
          </div>
          <div className="field__row">
            <Num label="Electric car" unit="km" value={a.electric_car_km} onChange={set('electric_car_km')} />
            <Num label="Two-wheeler" unit="km" value={a.two_wheeler_km} onChange={set('two_wheeler_km')} />
          </div>
          <div className="field__row">
            <Num label="Bus" unit="km" value={a.bus_km} onChange={set('bus_km')} />
            <Num label="Rail" unit="km" value={a.rail_km} onChange={set('rail_km')} />
          </div>
        </fieldset>

        <fieldset className="fieldset">
          <legend className="fieldset__legend">Air travel — one-way flights</legend>
          <div className="field__row">
            <Num label="Short haul" unit="flights" value={a.flights_short_haul} onChange={set('flights_short_haul')} step="1" />
            <Num label="Medium haul" unit="flights" value={a.flights_medium_haul} onChange={set('flights_medium_haul')} step="1" />
          </div>
          <Num label="Long haul" unit="flights" value={a.flights_long_haul} onChange={set('flights_long_haul')} step="1" />
        </fieldset>

        <fieldset className="fieldset">
          <legend className="fieldset__legend">Diet</legend>
          <div className="field">
            <label className="field__label" htmlFor="diet">Dietary pattern</label>
            <div className="control">
              <select id="diet" value={a.diet || ''} onChange={(e) => set('diet')(e.target.value)}>
                <option value="">Not specified</option>
                {DIETS.map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
              <Caret className="control__caret" />
            </div>
          </div>
        </fieldset>

        <fieldset className="fieldset">
          <legend className="fieldset__legend">Waste</legend>
          <div className="field__row">
            <Num label="Waste produced" unit="kg/mo" value={a.waste_kg} onChange={set('waste_kg')} />
            <Num label="Recycled" unit="%" value={a.recycling_percent} onChange={set('recycling_percent')} />
          </div>
        </fieldset>

        <div className="form__actions">
          <button className="btn btn--primary" type="submit" disabled={loading}>
            {loading && <span className="spinner" />}
            {loading ? 'Calculating…' : 'Calculate footprint'}
          </button>
          <button
            className="btn btn--ghost"
            type="button"
            onClick={() => setA(EMPTY)}
            title="Set every input to zero"
          >
            Clear all
          </button>
        </div>
      </div>
    </form>
  )
}
