import { useEffect, useState } from 'react'
import FootprintForm from './components/FootprintForm'
import ResultsPanel from './components/ResultsPanel'
import { Leaf } from './components/icons'

const API = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

const RECENT_KEY = 'carbon-ledger:recent'

export default function App() {
  const [regions, setRegions] = useState([])
  const [datasetVersion, setDatasetVersion] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetch(`${API}/api/regions`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r)))
      .then((data) => {
        setRegions(data.regions || [])
        setDatasetVersion(data.dataset_version)
      })
      .catch(() => setRegions([]))
  }, [])

  async function submit(payload) {
    setLoading(true)
    setError(null)
    try {
      const response = await fetch(`${API}/api/footprint/calculate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      const body = await response.json().catch(() => null)

      if (!response.ok) {
        // The Scenario 1 failure surfaces here as a 500 the user cannot act on.
        setResult(null)
        setError({
          status: response.status,
          code: body?.error || 'unknown_error',
          message: body?.message || 'The calculation could not be completed.',
        })
        return
      }

      setResult(body)
      setDatasetVersion(body.factor_dataset_version)

      // No account, no server-side identity. The browser keeps the last few
      // calculation ids so a user can revisit results within this browser.
      try {
        const recent = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
        localStorage.setItem(
          RECENT_KEY,
          JSON.stringify([body.calculation_id, ...recent.filter((id) => id !== body.calculation_id)].slice(0, 5)),
        )
      } catch {
        /* localStorage unavailable — not worth failing the calculation over */
      }
    } catch (err) {
      setResult(null)
      setError({ status: 0, code: 'network_error', message: String(err) })
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <header className="masthead">
        <div className="masthead__inner">
          <div className="brand">
            <Leaf className="brand__mark" />
            <span className="brand__name">Carbon Ledger</span>
            <span className="brand__sub">Household footprint</span>
          </div>

          {datasetVersion && (
            <span className="pill" title="Emission factor dataset in use">
              <span className="pill__dot" />
              Factors {datasetVersion}
            </span>
          )}
          <span className="pill pill--warn" title="This is a demonstration system">
            Demo data
          </span>
        </div>
      </header>

      <main className="shell">
        <FootprintForm
          regions={regions}
          loading={loading}
          onSubmit={submit}
        />
        <ResultsPanel result={result} error={error} loading={loading} />
      </main>

      <footer className="footnote">
        <hr className="footnote__rule" />
        <p>
          <strong>Not for reporting or disclosure use.</strong> Emission factors are real
          published values (DEFRA 2024, CEA CO<sub>2</sub> Baseline Database v20, EPA
          eGRID2022, Scarborough et&nbsp;al. 2014, IPCC AR6), but the dataset is
          incomplete and the accounting boundary is simplified. Results are indicative
          only — do not use them for carbon accounting, regulatory reporting, offset
          purchasing, or public disclosure.
        </p>
        <p>
          Boundary: household energy plus selected travel, diet, and waste. Excludes
          embodied emissions of goods and buildings, water, public services, and capital.
          Aviation figures include a 1.9× radiative forcing uplift.
        </p>
      </footer>
    </>
  )
}
