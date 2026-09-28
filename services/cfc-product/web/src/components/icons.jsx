/* Hand-drawn inline SVGs. No icon library — keeps the bundle small and the
 * visual language consistent (single 1.6 stroke, rounded caps, 24 grid). */

const base = {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
}

export function Leaf({ className }) {
  return (
    <svg {...base} className={className} width="26" height="26" aria-hidden="true">
      <path d="M4 20c0-8 5.5-13 16-13 0 9-5 14-11.5 14C6 21 4 20 4 20Z" />
      <path d="M9 21c0-5 2.5-8.5 7-11" />
    </svg>
  )
}

export function Calculator({ className }) {
  return (
    <svg {...base} className={className} width="34" height="34" aria-hidden="true">
      <rect x="4" y="3" width="16" height="18" rx="2.5" />
      <path d="M8 7h8M8 12h.01M12 12h.01M16 12h.01M8 16h.01M12 16h.01M16 16h.01" />
    </svg>
  )
}

export function CheckCircle({ className }) {
  return (
    <svg {...base} className={className} width="34" height="34" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12.2 2.4 2.4 4.6-5" />
    </svg>
  )
}

export function AlertTriangle({ className }) {
  return (
    <svg {...base} className={className} width="34" height="34" aria-hidden="true">
      <path d="M10.3 3.9 2.4 17.4A2 2 0 0 0 4.1 20.4h15.8a2 2 0 0 0 1.7-3l-7.9-13.5a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4.5M12 17h.01" />
    </svg>
  )
}

export function Caret({ className }) {
  return (
    <svg {...base} className={className} width="14" height="14" aria-hidden="true">
      <path d="m6 9 6 6 6-6" />
    </svg>
  )
}
