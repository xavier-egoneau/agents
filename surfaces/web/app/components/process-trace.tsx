import { useState } from "react";
import {
  traceLabel,
  traceState,
  visibleTraceTypes,
  type TraceEvent,
} from "../lib/trace";

export function ProcessTrace({
  events, live,
}: {
  events: TraceEvent[];
  live: boolean;
}) {
  return <ProcessTraceState key={live ? "live" : "terminal"} events={events} live={live} />;
}

function ProcessTraceState({
  events, live,
}: {
  events: TraceEvent[];
  live: boolean;
}) {
  const [expanded, setExpanded] = useState(live);
  const visible = events.filter((event) => visibleTraceTypes.has(event.type));
  if (visible.length === 0) return null;
  return (
    <section className={`process-trace ${expanded ? "expanded" : "collapsed"}`} aria-label="Traces d’exécution" aria-live="polite">
      <button
        type="button"
        className="trace-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <strong>Processus</strong>
        <span className="trace-summary">
          {live && <i className="trace-live-indicator" />}
          {live ? "en cours" : `${visible.length} étape${visible.length > 1 ? "s" : ""}`}
          <b aria-hidden="true">{expanded ? "−" : "+"}</b>
        </span>
      </button>
      {expanded && <ol>
        {visible.map((event, index) => {
          const state = traceState(event, index === visible.length - 1, live);
          const detail = String(
            event.payload.justification || event.payload.task || event.payload.reason ||
            event.payload.message || event.payload.error || "",
          );
          return (
            <li className={state} key={`${event.timestamp}-${event.type}-${index}`}>
              <span className="trace-dot" />
              <div>
                <strong>{traceLabel(event)}</strong>
                <small>{event.agent_id} · {new Date(event.timestamp).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small>
                {detail && <p>{detail}</p>}
              </div>
            </li>
          );
        })}
      </ol>}
    </section>
  );
}
