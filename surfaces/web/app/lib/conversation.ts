/**
 * ÉTAT D'UNE CONVERSATION
 * -----------------------------------------------------------------------------
 * Un réducteur plutôt que six `useState` : les messages, les approbations et
 * les traces changent ensemble au fil d'un run, et les faire varier séparément
 * exposait des états intermédiaires incohérents — des approbations rattachées
 * à un run déjà remplacé, par exemple.
 */

import type { Approval } from "../components/approval-panel";
import type { Message } from "./model";
import type { TraceEvent } from "./trace";

export type ConversationState = {
  messages: Message[];
  approvals: Approval[];
  activeSessionId: string | null;
  traceEvents: TraceEvent[];
  activeRunId: string | null;
};

export type ConversationAction = {
  type: "set";
  field: keyof ConversationState;
  value: unknown;
};

export function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  const current = state[action.field];
  const next = typeof action.value === "function"
    ? (action.value as (previous: typeof current) => typeof current)(current)
    : action.value;
  return { ...state, [action.field]: next };
}
