-- FROZEN reference schema (the shape a fresh database reaches after every
-- migration in db/migrations/ has been applied). Never edited by hand.

CREATE TABLE widgets (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
