# SWW — WaterlooWorks 简历匹配与申请排序

本仓库在原 JSM 项目基础上增加了本地 WaterlooWorks 匹配工具：上传 PDF 简历，在专用浏览器手动登录学校账号，分页采集 Co-op Full-Cycle 职位与详情，生成最多 100 个职位的申请优先级并导出 CSV。

```bash
./scripts/setup-matcher.sh
./scripts/start-matcher.sh
```

打开 **http://127.0.0.1:8765/waterlooworks**。需要 Python 3.10+、Node.js 20+；匹配工具可独立运行，无需启动原有 Go、MongoDB、Redis 或创建 JSM 账号。

使用说明、评分规则、登录步骤及当前验证范围见 **[WATERLOOWORKS.md](WATERLOOWORKS.md)**。真实的 Top 100 需要你自己的简历和有效的 WaterlooWorks 登录。仓库不附带个人简历、登录会话或真实职位数据。

原项目来源：[Icannotcode0/JSM](https://github.com/Icannotcode0/JSM)。保留原 Git 历史，`upstream` 指向原仓库，`origin` 指向本人的私有 SWW 仓库。下方保留原 JSM 文档；它描述的是原求职管理器。

---

# JSM — Job Search Manager

A local-first job application tracker. Log every application, the resume version you sent, compensation notes, and where each one stands — on a pipeline board that runs entirely on your own machine.

No cloud, no accounts, no telemetry. Mongo and Redis run in Docker on loopback, the Go server binds `127.0.0.1`, and nothing leaves your computer.

---

## Why local-only

This is a single-user personal tool holding a fairly sensitive dataset: where you're applying, what you're paid, and what you said about it. The simplest way to keep that private is not to host it.

That constraint shapes the design throughout — including the security model, which deliberately does *not* assume "local means trusted". Any page in another browser tab can attempt a drive-by `fetch()` to `localhost`, and cookies aren't port-scoped, so a page on any other localhost port shares this one's cookie jar. The backend is written as if it were internet-facing.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | Go 1.24, `net/http` (stdlib `ServeMux`, method + wildcard patterns) |
| Database | MongoDB 7 — applications and users |
| Sessions | Redis 7 — session hashes with a TTL |
| Frontend | TypeScript + Vite, zero runtime dependencies |
| Extension | Chrome MV3 (auto-capture from job boards — in progress) |

No web framework, no frontend framework, no CSS library. A page advertising that nothing leaves your machine shouldn't open a connection to a font CDN on load.

---

## Request lifecycle

```
Browser
  |
  |  fetch(credentials: "include")
  |  Cookie: jsm_session (HttpOnly) + jsm_csrf (readable by JS)
  |  X-CSRF-TOKEN: <the jsm_csrf value, echoed back>
  v
Vite dev server :5173 --> proxies named API routes ------+   dev only; in
  |                                                      |   production Go
  +-- anything else: the HTML page                       |   serves the pages
                                                         v
                                        Go server :8080, bound to 127.0.0.1
                                                         |
  +------------------------------------------------------+
  |
  v
RecoverMiddleware    a panic becomes 500; the process survives
  |
  v
LoggingMiddleware    method, path, status, duration
  |
  v
CSRFMiddleWare       safe methods    issue jsm_csrf, pass through
  |                  unsafe methods  cookie == header, HMAC valid,
  |                                  and token bound to this session
  v
root mux
  |
  +--> GET  /health            public
  +--> POST /signup            public
  +--> POST /login             public
  +--> POST /logout            public
  |
  +--> everything else
         |
         v
       SessionRequired -----> protected mux
         |                      GET    /me
         |                      POST   /reset-password
         |                      GET    /applications
         |                      POST   /applications
         |                      GET    /applications/{id}
         |                      PATCH  /applications/{id}
         |                      DELETE /applications/{id}
         |
         +-- resolves jsm_session against Redis and puts the
             user ID on the request context
```

Routing is **protected by default**: the public surface is a three-line list and
everything else falls through to the authenticated mux. Forgetting to guard a new
route is therefore impossible — the failure is a `401` you notice on the first
request, not an endpoint quietly serving your job search to anyone.

CSRF wraps every route rather than only the mutating ones, because the middleware
that *validates* a token on `POST` is also what *issues* it on `GET`. A cold
client calls `GET /health` to obtain one.

---

## Getting started

**Requires:** Go 1.24+, Node 18+, Docker.

```bash
git clone git@github.com:Icannotcode0/JSM.git
cd JSM

# 1. Databases (both bind to 127.0.0.1 only)
docker compose up -d

# 2. Config
cp .env.example .env
#    Optional but recommended:
#      echo "SESSION_SECRET=$(openssl rand -base64 32)" >> .env

# 3. Backend  — http://127.0.0.1:8080
cd backend && go run ./cmd/api

# 4. Frontend — http://localhost:5173  (separate terminal)
cd frontend && npm install && npm run dev
```

> **Note:** `config.Load()` reads `.env` relative to the process working directory. Running the server from `backend/` means it won't be found and every value silently falls back to its default. Run from the repo root, or export the variables.

### Creating an account

Open `http://localhost:5173/signup` and fill in the form. You'll be sent to the
sign-in page afterwards — creating an account doesn't sign you in, because
that's the path that has to keep working once email verification stands between
the two.

The password must be at least 8 characters and at most 72 bytes, with one
uppercase letter and one special character. The 72-byte cap is bcrypt's: it
truncates there, so anything longer would be silently ignored rather than hashed.

Addresses are folded to lowercase, so `You@Example.com` and `you@example.com`
are the same account — signing up as one and signing in as the other works.

On a local install the account is usable immediately. Set
`REQUIRE_EMAIL_VERIFICATION=true` and new accounts are created unverified
instead, pending the verification flow.

<details>
<summary>Creating one over HTTP instead</summary>

Every mutating request needs a CSRF token, and the server only issues one in
response to a safe request — so fetch one first, then send it back in the
header. This is the same two-step the frontend performs.

```bash
curl -s -c jar.txt http://127.0.0.1:8080/health > /dev/null
TOKEN=$(awk '$6=="jsm_csrf"{print $7}' jar.txt)

curl -s -b jar.txt -X POST http://127.0.0.1:8080/signup \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-TOKEN: $TOKEN" \
  -d '{"email":"you@example.com","password":"ChangeMe1!","name":"Your Name"}'

rm jar.txt
```

</details>

---

## API

Base URL `http://127.0.0.1:8080`. Full reference in [`API.md`](API.md).

| Method | Path | Auth | Status |
|---|---|---|---|
| `GET` | `/health` | — | ✅ |
| `POST` | `/signup` | CSRF | ✅ |
| `POST` | `/login` | CSRF | ✅ |
| `POST` | `/logout` | CSRF | ✅ |
| `GET` | `/me` | session | ✅ |
| `POST` | `/reset-password` | session + CSRF | ✅ |
| `GET` | `/applications` | session | ✅ |
| `POST` | `/applications` | session + CSRF | ✅ |
| `GET` | `/applications/{id}` | session | ✅ |
| `PATCH` | `/applications/{id}` | session + CSRF | ✅ |
| `DELETE` | `/applications/{id}` | session + CSRF | ✅ |
| `POST` | `/applications/{id}/resumes` | session + CSRF | 📝 planned |
| `GET` | `/applications/{id}/resumes/{resumeId}` | session | 📝 planned |
| `POST` | `/api/extension/applications` | session + CSRF + origin | 📝 planned |

`GET /applications` accepts `?status=`, `?tag=`, `?q=` (substring search over company, role, tags, and notes), `?page=`, and `?page_size=`, returning `{applications, page, page_size, total}`.

### Conventions

Success is `{"<resource>": {...}}`; errors are `{"error": "..."}`.

Every mutating request needs a session cookie **and** an `X-CSRF-TOKEN` header echoing the `jsm_csrf` cookie. That cookie is issued by any safe request, so a cold client calls `GET /health` first to obtain one.

---

## Security model

Notes on the decisions that aren't obvious from the code:

**CSRF tokens are signed and session-bound.** A plain double-submit cookie assumes an attacker can't write cookies into your browser. That assumption is weak here: cookies ignore ports, so a page on any other `localhost` origin shares this one's jar and could choose *both* halves of the pair. `SameSite` doesn't help either, since "site" also ignores the port. Tokens are therefore `<nonce>!<sessionID>.<HMAC>` — valid only if this server minted them *and* they're bound to the session presenting them. Binding also gives rotation for free: logging in changes the session ID, retroactively invalidating every token issued before the privilege change.

**Login is constant-time across both failure modes.** Returning the same error message for "no such user" and "wrong password" is pointless if only one of them runs bcrypt — the ~30× timing gap enumerates accounts just as well. The unknown-user path burns an equivalent bcrypt comparison against a throwaway hash.

**Ownership lives in the query filter, not a post-read check.** Every application query is scoped by `user_id` inside the filter itself, so there's no code path that can read or write another user's document. A record belonging to someone else returns the same `404` as one that doesn't exist.

**Routing is protected by default.** Authenticated routes sit on their own mux wrapped once in `SessionRequired`; the public surface is a three-line list, and everything else falls through to the protected group. Forgetting to guard a new route is therefore impossible — the failure mode is a `401` you notice immediately, not a silently public endpoint.

**All input is treated as untrusted,** including on the endpoints only the UI talks to. Length caps, HTML escaping at write time, `job_link` restricted to `http`/`https` (`javascript:`, `data:`, and `file:` are rejected), `regexp.QuoteMeta` on search terms before they reach Mongo, and a 1 MiB body cap. The browser extension will eventually POST scraped page content into the same models, and one validation path is safer than a "trusted" and an "untrusted" one that drift apart.

### Known gaps

- No rate limiting on `/login` — bcrypt gives ~60 ms of natural throttling, nothing more.
- The CSRF cookie hardcodes `Secure: true`, which Safari rejects over `http://localhost`. Chrome and Firefox accept it.
- `SESSION_SECRET` unset means a random per-boot signing key: safe, but outstanding CSRF tokens don't survive a restart.

---

## Layers

```
cmd/api                  wiring, startup order, graceful shutdown
    |
    v
internal/http            router: the public list, then SessionRequired
  http/handlers          decode, map errors to status codes, set cookies
  http/middleware        recover, logging
    |
    v
internal/service         validation, sanitising, business rules
    |                    the only layer that decides what is allowed
    v
internal/store           capability interfaces:
  store/mongo              HealthCheck | Authenticator | Applications
    |                    + their Mongo implementations
    v
internal/common
  mongoWrap ---> MongoDB     users, applications
  redisWrap ---> Redis       sessions
  jsmHttp                    JSON envelope, body cap
  logbuilder, metrics

used by every layer, owned by none:
  internal/authentication    sessions, signed CSRF tokens, bcrypt
  internal/domain            wire and storage models
```

Dependencies point one way — `http` → `service` → `store` — and nothing imports
upward. Handlers depend on a single-capability interface rather than the whole
service aggregate, so each one is testable with a one-method fake, and the store
is swappable by changing `NewStore` alone.

Validation lives in the service layer and nowhere else. There is no second
opinion in a Mongo schema validator or a handler, so the rules cannot drift apart.

---

## Layout

```
backend/
  cmd/api/            entrypoint — wiring, lifecycle, graceful shutdown
  internal/
    http/             router (public vs. authenticated) + handlers
    service/          business logic and all validation
    store/            persistence interfaces + Mongo implementations
    authentication/   sessions, CSRF, password hashing
    common/           mongoWrap, redisWrap, jsmHttp, logbuilder, metrics
    domain/           wire and storage models
frontend/
  src/                api client, dashboard, motion system
extension/            Chrome MV3 auto-capture (in progress)
```

Dependencies point inward: `http` → `service` → `store`. Handlers depend on a single-capability interface rather than the whole service aggregate, so each is testable with a one-method fake.

## Tests and CI

```bash
cd backend
go test ./...              # unit tests; store tests skip without a database
go test -race ./...        # what CI runs
```

52 tests across four layers. The store-layer tests run against a real MongoDB
and **skip themselves** when none is reachable, so the suite stays green on a
machine with no database — each one creates a throwaway `jsm_test_*` database
and drops it on cleanup, so they never touch your data. Point them elsewhere
with `MONGO_TEST_URI`.

`.github/workflows/ci.yml` runs on every pull request:

| Job | Checks |
|---|---|
| Backend | `go mod tidy` drift, `gofmt`, `go vet`, build, `go test -race` against real Mongo and Redis service containers |
| Frontend | `npm ci`, `tsc --noEmit`, production build |
| Secret scan | refuses a tracked `.env`, or a non-blank `SESSION_SECRET` in `.env.example` |

Because a skipped test looks like a passing one, CI asserts the database-backed
tests actually ran rather than trusting a green summary.

---

## Documentation

| File | Contents |
|---|---|
| [`API.md`](API.md) | Endpoint reference, auth model, error codes |
| [`DATABASE.md`](DATABASE.md) | Schema, embed-vs-reference reasoning, indexes, multi-tenancy rule |
| [`DESIGN_GUIDE.md`](DESIGN_GUIDE.md) | Architecture and build order by milestone |
| [`frontend/DESIGN_SYSTEM.md`](frontend/DESIGN_SYSTEM.md) | Visual language, motion principles, the API boundary |

---

## Roadmap

- [x] Auth: sessions, CSRF, login/logout
- [x] Applications: full CRUD, filter, search, pagination
- [x] Dashboard: pipeline board, stats, inline editor
- [x] Sign-up, with email validation and a shared password policy
- [x] Change password, with session invalidation
- [x] Test suite and CI on every pull request
- [ ] Email verification (`Mailer` seam and config flag are in place)
- [ ] Resume uploads (Milestone 8)
- [ ] Browser extension auto-capture (Milestone 14)
- [ ] Rate limiting on auth endpoints
