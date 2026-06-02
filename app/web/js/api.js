// api.js — thin REST client for the Team backend.
//
// Wraps fetch; on a non-2xx response it surfaces the backend's structured
// `{error:{kind,message}}` body as an ApiError so callers (and the UI) can show
// a clean message instead of a raw stack. All endpoints documented in the Unit
// 12 brief are covered.

export class ApiError extends Error {
  constructor(kind, message, status) {
    super(message || kind || "request failed");
    this.name = "ApiError";
    this.kind = kind || "ApiError";
    this.status = status;
  }
}

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("NetworkError", String(e?.message || e), 0);
  }
  if (resp.status === 204) return null;
  const text = await resp.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!resp.ok) {
    const err = data && data.error ? data.error : {};
    throw new ApiError(err.kind || "HttpError", err.message || resp.statusText, resp.status);
  }
  return data;
}

const get = (p) => request("GET", p);
const post = (p, b) => request("POST", p, b);
const patch = (p, b) => request("PATCH", p, b);
const del = (p) => request("DELETE", p);

export const api = {
  health: () => get("/health"),

  // authors (FR-A1..A3)
  listAuthors: () => get("/authors"),
  createAuthor: (b) => post("/authors", b),
  updateAuthor: (id, b) => patch(`/authors/${id}`, b),
  deleteAuthor: (id) => del(`/authors/${id}`),

  // personas (FR-P1..P4)
  listPersonas: () => get("/personas"),
  listTemplates: () => get("/personas/templates"),
  getPersona: (id) => get(`/personas/${id}`),
  createPersona: (b) => post("/personas", b),
  updatePersona: (id, b) => patch(`/personas/${id}`, b),
  deletePersona: (id) => del(`/personas/${id}`),
  duplicatePersona: (id) => post(`/personas/${id}/duplicate`),
  personaFromTemplate: (id) => post(`/personas/${id}/from-template`),

  // rooms (FR-R*)
  listRooms: () => get("/rooms"),
  getRoom: (id) => get(`/rooms/${id}`),
  createRoom: (b) => post("/rooms", b),
  updateRoom: (id, b) => patch(`/rooms/${id}`, b),
  deleteRoom: (id) => del(`/rooms/${id}`),
  listMembers: (id) => get(`/rooms/${id}/members`),
  addMember: (id, persona_id) => post(`/rooms/${id}/members`, { persona_id }),
  removeMember: (id, persona_id) => del(`/rooms/${id}/members/${persona_id}`),

  // messages (chronological)
  listMessages: (id, { offset = 0, limit } = {}) => {
    const q = new URLSearchParams({ offset: String(offset) });
    if (limit != null) q.set("limit", String(limit));
    return get(`/rooms/${id}/messages?${q}`);
  },

  // session reset (FR-C4)
  resetSession: (roomId, personaId) =>
    post(`/rooms/${roomId}/personas/${personaId}/reset-session`),

  runLogUrl: (runId) => `/runs/${runId}/log`,
};
