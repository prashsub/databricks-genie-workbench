import { SqlCodeBlock } from '@/components/SqlCodeBlock'

// A structured, human-readable view of a version's serialized_space (Genie schema v2:
// data_sources.{tables,metric_views}[].column_configs, instructions.{text_instructions,
// example_question_sqls, join_specs, sql_snippets.{filters,expressions,measures},
// sql_functions}). The snapshot type is `unknown`, so every read is defensive: unknown
// keys are preserved under "Other", empty sections are omitted, and the raw JSON stays
// available behind a disclosure for power users.

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function text(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function firstText(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = text(record[key])
    if (value) return value
  }
  return ''
}

// text_instructions entries may be a plain string or { content: string[] } — the API
// concatenates content elements (each ends with \n) into one instruction.
function instructionText(value: unknown): string {
  if (typeof value === 'string') return value
  const record = asObject(value)
  if (Array.isArray(record.content)) return record.content.map(text).join('')
  return text(value)
}

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-secondary">
        {title}{typeof count === 'number' ? <span className="ml-1 text-muted">({count})</span> : null}
      </h4>
      {children}
    </section>
  )
}

function SqlEntry({ record, index, labelKeys }: { record: Record<string, unknown>; index: number; labelKeys: string[] }) {
  const label = firstText(record, labelKeys) || `#${index + 1}`
  const sql = firstText(record, ['sql', 'answer', 'query'])
  return (
    <div className="space-y-1">
      <p className="text-xs text-secondary">{label}</p>
      {sql ? <SqlCodeBlock code={sql} maxLines={8} /> : null}
    </div>
  )
}

export function ConfigView({ snapshot }: { snapshot: unknown }) {
  const root = asObject(snapshot)
  const dataSources = root.data_sources
  const tables = Array.isArray(dataSources) ? dataSources : asArray(asObject(dataSources).tables)
  const metricViews = Array.isArray(dataSources) ? [] : asArray(asObject(dataSources).metric_views)

  const instructions = asObject(root.instructions)
  const textInstructions = (asArray(instructions.text_instructions).length
    ? asArray(instructions.text_instructions)
    : asArray(root.text_instructions))
  const exampleSqls = asArray(instructions.example_question_sqls)
  const joins = asArray(instructions.join_specs)
  const snippets = asObject(instructions.sql_snippets)
  const filters = asArray(snippets.filters)
  const expressions = asArray(snippets.expressions)
  const measures = asArray(snippets.measures)
  const sqlFunctions = asArray(instructions.sql_functions)
  const sampleQuestions = (asArray(root.sample_questions).length
    ? asArray(root.sample_questions)
    : asArray(instructions.sample_questions))

  const metadataEntries = Object.entries(root).filter(
    ([key, value]) =>
      !['data_sources', 'instructions', 'text_instructions', 'sample_questions'].includes(key) &&
      (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'),
  )
  const handled = new Set(['data_sources', 'instructions', 'text_instructions', 'sample_questions', ...metadataEntries.map(([key]) => key)])
  const otherEntries = Object.entries(root).filter(([key]) => !handled.has(key))

  const sqlSection = exampleSqls.length + expressions.length + measures.length + sqlFunctions.length
  const hasAnything =
    tables.length || metricViews.length || textInstructions.length || joins.length ||
    filters.length || sqlSection || sampleQuestions.length || metadataEntries.length || otherEntries.length

  const box = 'rounded-md border border-default bg-surface p-2'

  return (
    <div className="space-y-4">
      {(tables.length > 0 || metricViews.length > 0) && (
        <Section title="Data sources" count={tables.length + metricViews.length}>
          <div className="space-y-2">
            {[...tables, ...metricViews].map((entry, index) => {
              const table = asObject(entry)
              const columns = asArray(table.column_configs)
              return (
                <div key={index} className={box}>
                  <p className="font-mono text-xs text-secondary">{firstText(table, ['identifier', 'table', 'name']) || `Table ${index + 1}`}</p>
                  {firstText(table, ['description']) && <p className="mt-0.5 text-xs text-muted">{firstText(table, ['description'])}</p>}
                  {columns.length > 0 && (
                    <ul className="mt-1.5 space-y-0.5">
                      {columns.map((column, ci) => {
                        const col = asObject(column)
                        const description = firstText(col, ['description'])
                        return (
                          <li key={ci} className="text-xs text-muted">
                            <span className="font-mono text-secondary">{firstText(col, ['column_name', 'name'])}</span>
                            {description ? ` — ${description}` : ''}
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </div>
              )
            })}
          </div>
        </Section>
      )}

      {textInstructions.length > 0 && (
        <Section title="Instructions">
          <div className="space-y-2">
            {textInstructions.map((entry, index) => (
              <pre key={index} className={`${box} max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs text-secondary`}>
                {instructionText(entry)}
              </pre>
            ))}
          </div>
        </Section>
      )}

      {joins.length > 0 && (
        <Section title="Joins" count={joins.length}>
          <div className="space-y-2">
            {joins.map((entry, index) => {
              const join = asObject(entry)
              const left = firstText(asObject(join.left), ['identifier', 'alias'])
              const right = firstText(asObject(join.right), ['identifier', 'alias'])
              const sql = asArray(join.sql).map(text).filter(Boolean).join('\n') || firstText(join, ['sql'])
              return (
                <div key={index} className={`${box} space-y-1`}>
                  <p className="text-xs text-secondary">{left && right ? `${left} ⋈ ${right}` : firstText(join, ['id']) || `Join ${index + 1}`}</p>
                  {firstText(join, ['comment', 'instruction']) && <p className="text-xs text-muted">{firstText(join, ['comment', 'instruction'])}</p>}
                  {sql && <SqlCodeBlock code={sql} maxLines={6} />}
                </div>
              )
            })}
          </div>
        </Section>
      )}

      {filters.length > 0 && (
        <Section title="Filters" count={filters.length}>
          <div className="space-y-2">
            {filters.map((entry, index) => (
              <SqlEntry key={index} record={asObject(entry)} index={index} labelKeys={['display_name', 'id']} />
            ))}
          </div>
        </Section>
      )}

      {sqlSection > 0 && (
        <Section title="Sample SQL" count={sqlSection}>
          <div className="space-y-2">
            {exampleSqls.map((entry, index) => (
              <SqlEntry key={`q${index}`} record={asObject(entry)} index={index} labelKeys={['question', 'display_name', 'id']} />
            ))}
            {[...expressions, ...measures, ...sqlFunctions].map((entry, index) => (
              <SqlEntry key={`e${index}`} record={asObject(entry)} index={index} labelKeys={['display_name', 'name', 'id']} />
            ))}
          </div>
        </Section>
      )}

      {sampleQuestions.length > 0 && (
        <Section title="Sample questions" count={sampleQuestions.length}>
          <ul className="space-y-1">
            {sampleQuestions.map((entry, index) => (
              <li key={index} className="text-xs text-secondary">{firstText(asObject(entry), ['question', 'text']) || text(entry)}</li>
            ))}
          </ul>
        </Section>
      )}

      {metadataEntries.length > 0 && (
        <Section title="Metadata">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            {metadataEntries.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-muted">{key}</dt>
                <dd className="text-secondary break-words">{text(value)}</dd>
              </div>
            ))}
          </dl>
        </Section>
      )}

      {otherEntries.length > 0 && (
        <Section title="Other">
          <pre className={`${box} max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs text-muted`}>
            {JSON.stringify(Object.fromEntries(otherEntries), null, 2)}
          </pre>
        </Section>
      )}

      {!hasAnything && (
        <p className="text-xs text-muted">No structured configuration in this snapshot.</p>
      )}

      <details className="rounded-lg border border-default bg-surface-secondary/40">
        <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-secondary">
          View raw JSON
        </summary>
        <pre className="max-h-80 overflow-auto px-3 pb-3 text-xs text-muted whitespace-pre-wrap break-words">
          {JSON.stringify(snapshot, null, 2)}
        </pre>
      </details>
    </div>
  )
}
