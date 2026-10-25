export default function FilterTabs({ label, tabs, value, onChange }) {
  return (
    <nav className="order-tabs" aria-label={label}>
      {tabs.map(([id, text]) => (
        <button
          key={id}
          aria-current={value === id ? 'page' : undefined}
          className={value === id ? 'active' : ''}
          onClick={() => onChange(id)}
        >
          {text}
        </button>
      ))}
    </nav>
  )
}
