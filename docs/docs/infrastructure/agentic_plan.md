# Agentic Implementation Plan

## 1. Problem and Context
With the successful launch of the CLI, the next step is to make the GLaDOS platform compatible with Model Context Protocol (MCP) tool calls so that experiments can be carried out be autonomous Artificial General Inteligence (AGI) agents. Specifically, the goal is to give the user the ability to delegate actions they can take on the platform manually to an AI agent that performs them on their behalf.

After some consideration, the team decided that it is recommended to pursue a local MCP facade that calls to existing REST endpoints with authentication done via token introspection. For more details on this decision consult section 5 and 6 of this document. 


## 2. Scope and limitations
The following describes both the scope of the agentic implementation feature as well as possible limitations:

- The agentic model used by the user is not within scope; the mistakes the model might make in utilizing MCP tool calls or in adjusting the hyperparameters of an experiment for a new run is not within scope. Ergo, the infrastructure for such calls to occur securely and consistently is within scope; the quality of the model is not.
- The user of this feature is assumed to have a GitHub account for which a GitHub token can be gained through device flow authentication, which allows for authentication to GLADOS; Google OAuth or other provider specific Oauth routes are not within scope.
- We recommend limiting this feature to user's of priviledged or admin permission levels, however this feature does have the possibility to be open to users at all permission levels.
- All operations requires a verification step via GitHub api, users who hit their GitHub api limit of [5000 requests per hour](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2026-03-10) will not be able to access MCP features and have to use the browser GUI manually


## 3. Invariants
Let `t` be an MCP tool call, and `ENDPOINT` be an authenticated call (using a stored `token` for the active session) to the existing REST API counterpart for `t`.

#### **3.1 Functional Invariant**
For all tool invocation with arguments `t(args)`, the following premises must hold:

**I-1 - Structural**. The invocation of `t(args)` causes exactly one ENDPOINT call to be reported to the caller. The MCP _MAY_ retry on transport failure up to a bounded number of times before surfacing failure; retries are not counted as separate logical calls.

**I-2 - Payload**. Content of `args` must not be modified beyond translation to comply with `ENDPOINT` argument schema. A translation here is defined as a syntactic transformation using a declared schema, no values should be transformed.

**I-3 - Response Content**. Given that the MCP tool call is going to translate the REST response into an MCP response, the MCP response carries no information not present in the REST response; fields may be dropped, but never fabricated, inferred, or sourced from outside the REST response.

#### **3.2 Security Invariant**
The LLM agent and its provider sit outside of GLADOS's trust boundary and are therefore modeled as a Compromisable Host or External LLM Liaison (CHELL). The following invariants must hold against the CHELL:

**S-1 - Result Confidentiality**. CHELL shall not gain read access to result data returned by any `ENDPOINT` call. MCP responses, error messages, transport metadata, and diagnostic output shall not carry result content to CHELL.

**S-2 - Artifact Confidentiality**. CHELL shall not gain read or write access to artifacts that a tool call requires or produces. Artifact content reaches CHELL only through channels the user explicitly drives (e.g., paste, upload).

**S-3 - Credential Confinement**. The `token` shall not be observable by CHELL through any MCP tool result, error path, argument echo, or diagnostic output.

**S-4 - Bounded Tool Surface**. CHELL may only invoke `t` drawn from a statically declared allowlist. No `t` shall accept free-form filesystem paths, URLs, shell expressions, or ENDPOINT specifiers as arguments. This prevents CHELL from inducing arbitrary reads, writes, or egress through `args`.

**S-5 - Diagnostic Discipline**. MCP facade logs, stderr, crash output, and telemetry shall not contain result data, artifact content, or credential material in any form CHELL or its host can read.

**S-6 - Egress Confinement**. The MCP facade shall initiate no outbound network connection other than to ENDPOINT over verified TLS. CHELL shall not be able to induce arbitrary egress through `args`.

**S-7 - User Override**. Voluntary delivery of data to CHELL by the user (paste, upload, screen sharing, dictation) lies outside the protection of `S-1 … S-6`. The facade shall neither facilitate, conceal, nor attempt to detect such transfers.

**S-8 - Audit Integrity**. No state-mutating tool call shall complete without a corresponding append-only audit record being durably written. Records shall be neither modifiable nor deletable through any system interface. The audit log shall contain no silent gaps: any period during which audit writes were unavailable shall itself be reflected in the log as a degraded-mode record.
## 4. Architecture Overview

### 4.1 Authentication Subsystem
The system establishes caller identity at the REST API via RFC 7662 token introspection. The MCP facade is the sole custodian of the user's token within the local environment; this placement is what realizes `S-3` (credential confinement).
> No MCP tool surface can expose the token to CHELL, by construction.

#### Components
1. **AI agent (within CHELL).** Issues MCP tool calls. Never observes the token.
2. **MCP facade.** Holds the token, attaches it to REST calls, translates responses per `I-2` and `I-3`, returns them to the agent subject to `Section 3.2`.
3. **REST API**. Validates the token via introspection, resolves identity, executes the requested operation under the resolved identity.
GitHub. Issues the token and serves the RFC 7662 introspection endpoint. Source of external identity.
4. **MongoDB.** Maps external (GitHub) identity to internal system identity U. Source of truth for per-user authorization.

```mermaid
sequenceDiagram
    actor User
    participant Agent as AI Agent (CHELL)
    participant MCP as MCP Facade
    participant REST as REST API
    participant GH as GitHub
    participant DB as MongoDB

    User->>Agent: prompt: task A on data D
    Agent->>MCP: t(ref(D))
    Note over Agent,MCP: S-2: D passed by reference
    MCP->>REST: request task A (Authorization: Bearer <token>)
    Note over MCP,REST: S-3: token held only by MCP
    REST->>GH: POST /introspect (token)
    GH-->>REST: { active, external identity }
    REST->>DB: lookup(external identity)
    DB-->>REST: internal identity U
    REST->>REST: execute task A as U
    REST-->>MCP: result
    MCP-->>Agent: translated result (subject to S-1)
    Agent-->>User: response
```
1. User prompts the AI agent to perform task `A` on data `D`.
2. The agent invokes the corresponding MCP tool with a reference to `D`, not its contents (`S-2`).
3. The MCP facade issues a REST call for task A with the token attached (per `I-2`).
4. The REST API introspects the token at GitHub and receives the external identity.
5. The REST API resolves external identity to internal identity `U` via MongoDB.
6. The REST API executes task `A` under `U` and returns the result. The facade translates the response per `I-3` and surfaces it to the agent subject to `S-1`.
### 4.2 Audit Logs

To appropriately log the actions of the agent, we recommend a two-pronged approach with an audit log both locally available for a user as well as a remote audit log for GLADOS's server. This ensures that the user can reread the local log to be aware of the actions of the agent, and developers can monitor agent activity via the remote log. 

#### **1. Remote Audit Log**
Creating a new capped collection on GLADOS’s MongoDB with role-based restrictions that prevent editing operations is our recommended implementation of a synchronous append-only sink. 

In this collection, record an agent request’s  `event_id, user_id, timestamp, action_type, action_result, and agent_id`. A successful write/insertion to the collection allows the request to go through (the action to occur on the server); otherwise, the request is denied. A health check, implemented by a ping command to the database via a Next.js endpoint on GLADOS’s server, checks if an insertion could happen to the collection; if the check returns unhealthy, allow agentic non-mutating requests (i.e. read operations) but return degraded-mode error on mutating requests.

The following subsections provide more implementation details on the above description.

##### Document Fields:  

For this collection, we recommend enforcing that each of the documents have the following fields for ensuring an accurate and thorough log of agentic activities is recorded: 

- event_id — Unique identifier for each audit event 
- user_id — Unique identifier for the user who initiated the action 
- timestamp — UTC timestamp indicating when the action occurred 
- action_type — Type of action performed (e.g., file_read, file_write) 
- action_result — Outcome of the action (e.g., success, failure) 
- agent_id — Identifier of the agent or model that performed the action 

##### Synchronous: 

In this setup, when an agent sends a request via an endpoint to GLADOS, an entry would be made in the MongoDB collection reflecting the event. And once this insertion is successfully made, the requested action can go through. Otherwise, the action would fail as the log insertion was not successful. 

##### Append-Only:  

Developers should restrict editing or delete permissions for this collection- this log is intended as a non-mutable record of events. Should a correction need to be made, a new entry should be inserted. 

To implement this, developers can use a capped MongoDB collection, which is a type of collection ideal for maintaining logs because delete operations are restricted and, when the fixed size of the collection is reached, old entries are overwritten, which is ideal for audit logs as the most recent entries are most valued. Additionally, edit operations from users should be restricted via role-based checks in GLADOS server endpoints. 

##### Continuous Health Check:  

The MCP endpoint can ping the database to ensure that the audit log is healthy can be inserted into. Should this ping fail, agent action requests should be rejected automatically as the log cannot be inserted into. Otherwise, successful health checks mean that the database is healthy and ready for insertion. 

To implement this, developers can create a Next.js health check endpoint that pings the MongoDB pod every certain time increments.  

#### **2. Local Audit Log**
While the current CLI produces a brief log of results/errors as the user runs commands, given the nature of agents, users likely would expect a more thorough record in order to examine what actions the agents took while utilizing their account. Utilizing the Python logging module may be most effective and allow flexibility. The module allows to write to a user’s local file to implement more extensive logging beyond the CLI’s current printouts to the console. Record agentic actions, including `datetime, action_taken, user_approval, result (Success or Failure), error (High Level Description Based Off Stack Trace If it Exists)`, to ensure that the dynamic information is included efficiently within a file that can then be scanned for certain keywords.

A basic logging example is the following: 

```
>>> import logging 
>>> logging.basicConfig( 
...    filename='glados_audit.log', 
...    level=logging.INFO, 
... ) 
>>> logging.error("Something went wrong!")
```

By saving the logs to a file, such as “glados_audit.log” as in the above example, instead of simply printing to a terminal, we can ensure that the dynamic information is included efficiently within a file that can then be scanned for certain keywords. 

### 4.3 MCP Facade
The system follows a facade architecture: a local process on the user's machine sits between the agent (running under the Claude Code TUI, or any equivalent host) and the remote REST API. The facade is the only component that holds credentials, makes outbound REST calls, and decides what crosses the trust boundary to the agent. Data classified high-sensitivity does not flow to the agent through facade responses; instead, the facade places it in a handoff region on the local filesystem, and the user performs an explicit gesture to release it. The mechanism for that gesture is intentionally underspecified at this layer; suggested implementations are listed below.

#### **Components**
```mermaid
flowchart TB
    User((User))

    subgraph LocalHost["Local host (user's machine)"]
        TUI["Claude Code TUI<br/>(user-facing surface)"]
        MCP["MCP Facade<br/>(token custodian, allowlisted tools)"]
        FS[("Artifact handoff region<br/>(filesystem)")]
    end

    subgraph CHELLBox["CHELL (untrusted)"]
        Agent["AI Agent + LLM provider"]
    end

    subgraph Remote["Remote infrastructure (GLADOS)"]
        REST["REST API"]
    end

    User -->|prompt| TUI
    User -->|reviews| FS
    User -.->|consent gesture| FS
    TUI <-->|input / output stream| Agent
    Agent -->|MCP tool calls| MCP
    MCP -->|LOW data response| Agent
    MCP <-->|TLS, bearer token| REST
    MCP -->|HIGH artifact write| FS
```
- **Claude Code TUI.** User's terminal interface to the agent. Treated as a trusted surface for input/display; does not enforce policy itself.
- **AI Agent + LLM provider.** The reasoning and tool-invocation loop. Modeled as CHELL — anything the agent observes is assumed to be observable by an adversary.
- **MCP Facade.** Local process. Holds the REST API credentials, exposes a static allowlist of tools, and decides per tool whether the result returns inline (low-sensitivity path) or via the handoff region (high-sensitivity path).
- **Artifact handoff region.** A filesystem location where the facade deposits high-sensitivity results. The release of content from this region to the agent is mediated entirely by the user.
REST API. Remote service; authenticates via token introspection, authorizes per-identity.

#### **Data paths**

- `LOW` - **Low-sensitivity path.** Agent → MCP tool call → REST → translated MCP response → agent. Constrained by S-1 (no high-sensitivity content in responses) and §3.1 (response translation).
- `HIGH` - **High-sensitivity path.** Agent → MCP tool call → REST → artifact written to handoff region. The agent receives only an acknowledgement; the result content sits at rest until the user acts.

#### **Consent gesture - suggested mechanisms**
The architecture does not mandate a specific gesture; future contributors should pick what fits their platform. Non-exhaustive options:

- **Copy / paste.** User pastes artifact content into the TUI. Unforgeable; manual.
- **Permission flip (chmod).** User grants the agent's runtime UID read access; an allowlisted tool then fetches. Requires UID separation between facade and agent.
- **Move into a watched inbox.** User drops the artifact into a designated directory the agent reads on demand. Same UID consideration as above.
Confirmation token. User reviews the artifact, types a short token displayed alongside it into the TUI; facade releases on next reference. Adds binding/expiry complexity.
- **Out-of-band CLI helper.** User runs a small command that pushes content into the agent's input stream. Useful where clipboard integration is poor.

These are not mutually exclusive, a system may offer several.

## 5. Implementation notes
Some useful implementation notes
### 5.1 Strive for enforcement through architecture and design rather than prompt engineering
An LLM at the end of the day is a probabilistic model. Because of that, banking on the fact that it output will be governable and deterministic is a naive assumption. If you wish for the agent to not attempt an action, then the architecture should never allow for such cases.

### 5.2 Default new operations to HIGH sensitivity
Sensitivity classification is the most consequential decision per tool: LOW means the result flows directly to CHELL. Defaulting to HIGH forces contributors to argue explicitly that a given response field is safe to expose, and creates an auditable trail of those arguments. LOW-by-default silently leaks whatever a less careful contributor didn't think to question.

### 5.3 Allowlist, never blocklist
The set of tools, response fields, and outbound destinations the facade may emit must be statically enumerated. Resist any pattern that lets configuration, runtime conditions, or model output expand these sets. Every such pattern is a path by which CHELL or a misconfigured deployment widens the attack surface.

### 5.4 Treat error paths like response paths
The agent reads errors as eagerly as it reads successful responses. A stack trace, an exception message, or a "field X was missing" complaint is a data channel. The invariants on response content (`S-1`, `S-3`, `S-5`) apply equally to every place the facade can emit text (exceptions, transport errors, debug output, etc...).

### 5.5 Fail closed
When in doubt (introspection times out, schema validation fails, the audit sink is unavailable, an argument is unrecognized) reject the call. A broken feature is recoverable; a silent leak is not. This is the default that makes `S-1` (unknown fields dropped) and `S-8` (no mutation without audit) cheap to implement: when behavior is ambiguous, the safe answer is to do nothing.

### 5.6 Encode invariants as CI checks
Formal invariants are written to be mechanically translatable. Every invariants that admits a test should have one wired into the CI pipeline: shape assertions for `S-1`, emit-path scanning for `S-3`, static analysis of the tool registry for `S-4`, failure-injection on the audit sink for `S-8`, and so on. This moves security from contributor discipline (which decays under deadline pressure) to executable proof (which is regenerated on every commit), and makes audits cheap by giving reviewers running tests rather than arguments. The residual judgment (recognizing when a new tool introduces a surface no existing test covers) cannot be mechanized, but it becomes narrower and more honest once routine checks are automatic.

### 5.7 The user is a cooperator, not an obstacle
The high-sensitivity consent gesture exists because **the user is a participant in the security model, not friction to be optimized away**. Future requests to "make consent easier" by weakening the gesture should be treated with skepticism. UX friction in the consent mechanism is a deliberate choice.

### 5.8 Beware third-party SDK telemetry
Logging libraries, error trackers, and observability SDKs frequently capture function arguments, request bodies, and stack-local variables. Any such SDK installed in the facade process becomes part of `S-5`'s threat surface. Audit every dependency for outbound behavior; prefer libraries with explicit, controllable telemetry.

### 5.9 Test refusal, not just success
The interesting tests in this system are the ones that confirm the facade *won't* do something: malformed args are rejected, unknown response fields are dropped, audit failure blocks the mutation. Tests that only exercise the happy path leave the actual security properties unverified.


## 6. Alternatives Considered
This section details alternative implementations that were considered during initial architecting and design of the agentic workflow integration.

### 6.1 MCP as an extention of the current `Next.js` REST API
Rather than running the MCP facade locally on the user's machine, MCP tool calls could be served directly from the existing Next.js REST API, with clients connecting over the network.

#### **Why not adopted**
The REST API authenticates callers by introspecting GitHub-issued tokens (Secion 6.2). In a co-hosted model, the MCP transport would need to deliver those tokens from client to backend. However, MCP's transport-level authorization, defined in the [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) specification, is OAuth-based and does not accommodate passing arbitrary upstream credentials. Adopting this alternative would require either re-implementing the backend's authentication layer to comply with the MCP authorization spec, or inventing a non-standard credential-passing mechanism that would break compatibility with conforming MCP clients. The current team judged the local-facade approach preferable, as it preserves the introspection-based auth model unchanged and reuses REST endpoints without modification.

#### **Conditions to reconsider**
This decision should be revisited if the system migrates authentication to a flow compatible with the MCP authorization spec, or if operating a per-user local facade becomes a deployment burden disproportionate to the cost of MCP-spec auth compliance.

### 6.2 Self-issued JWTs in place of GitHub token introspection
Rather than delegating authentication to GitHub and introspecting GitHub-issued tokens at the REST API (RFC 7662), the system could mint its own JWTs after a user authenticates via the web frontend. Clients (web, CLI, and MCP facade) would present these JWTs in REST calls; the REST API would verify them locally without a round trip to an external provider.

#### **Current state of the system**
The web frontend authenticates users via Auth.js against GitHub or Google. Auth.js manages session state for the browser flow; no JWT is minted or consumed anywhere in the system today. For the CLI (a browserless client that calls the REST API directly), authentication is delegated to GitHub via the OAuth device authorization flow (RFC 8628); the CLI then presents the resulting GitHub token to the REST API, which introspects it. The MCP facade is a browserless client of the same shape and inherits the same pattern.

#### **Why not adopted**
Introducing JWTs would require:

- A token issuance path on the REST API, including signing key management and rotation.
- A browserless acquisition flow, either user-driven copy/paste of a token minted in the web frontend, or a custom device-flow analogue against our own issuer.
- Token revocation and lifecycle handling at the API layer, since one of introspection's benefits (immediate revocation by the identity provider) would no longer come for free.

The current team judged this disproportionate to the value gained, particularly because:

- The CLI was built first under the same constraint; delegating browserless auth to GitHub's device flow solved the UX problem at zero implementation cost.
- The REST API already supports introspection, so the MCP facade reuses existing infrastructure without modification.
- Cross-client identity remains anchored at a single source (GitHub), removing the need for the system to maintain its own identity claims.

#### **Conditions to reconsider**
This decision should be revisited if any of the following becomes true:

- Introspection latency becomes a measurable bottleneck and caching proves insufficient.
- The system requires identity claims or scopes that GitHub does not carry.
- Dependency on GitHub's introspection endpoint availability becomes operationally unacceptable.
- A second-class identity provider (beyond GitHub and Google) is introduced and federation becomes preferable to per-provider token validation.

#### **Implementation Costs if reconsidered**
Future contributors taking this on should expect to implement token issuance, signing key rotation, a browserless token acquisition UX (the painful part), local JWT verification at REST, and a revocation strategy. None of this is intractable; the current team simply judged the present arrangement adequate for the system's needs.

## 7. Threat Modeling
This section enumerates threats within the system's scope using the STRIDE framework (Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of privilege). The aim is to document the attack surface so new contributors can reason about changes, and to make existing mitigations and known gaps legible. Threats outside the scope of the system (credentialed insiders, social engineering, supply-chain compromise of dependencies) are noted only where they meaningfully shape design decisions.

```mermaid
flowchart TB
    User(["User"])
    subgraph CHELL_TB["CHELL (untrusted)"]
        Agent["AI Agent + LLM provider"]
    end

  

    subgraph LocalHost["Local host (trusted)"]
        TUI["Claude Code TUI"]
        MCP(("MCP Facade"))
        Token[("Token store")]
        FS[("Artifact handoff region")]
    end

    subgraph Remote["Remote infra (trusted)"]
        REST(("REST API"))
    end

    User -->|f1: prompt| TUI
    TUI <-->|f2: I/O stream| Agent
    Agent -->|f3: tool call args| MCP
    MCP -->|f4: LOW response| Agent
    MCP <==>|f5: REST over TLS — crosses public network| REST
    MCP -->|f6: HIGH artifact write| FS
    User -->|f7: review| FS
    User -.->|f8: consent gesture| FS
    Token -.->|read by| MCP
```

## 8. Testing strategy

To ensure that the features associated with enabling agentic workflows in GLADOS are properly implemented, creating a multi-faceted testing strategy is crucial. The following tests can establish correctness and also ensure implementation matches security specifications: 

### 8.1 Agent Testing to test End-to-end Operations and Monitor Nondeterministic Interactions 

This testing would verify that end-to-end operations of a local agent using the locally hosted MCP servers to call the REST API endpoints, be recorded in the audit log, and then return the expected payload. This would ensure that the correct sequency of calls could occur. 

This test would use the addnums experiment (located here: [Add Nums Experiment](https://github.com/AutomatingSciencePipeline/Monorepo/blob/main/example_experiments/python/addNums.py)) with a description of the experiment [on this documentation page](https://github.com/AutomatingSciencePipeline/Monorepo/blob/main/docs/docs/deafult-exp-guides/addNums.md).

This test would require a full sequence of calls to execute the experiment: Starting with authentication, then proceeding to submitting and running the experiment, querying to check on experiment status, and downloading all relevant experiment artifacts. See security testing in 8.3 which elaborates on some of the security standards that need to be tested for these actions; however, this end-to-end testing will check for functionality first. 

A 32 GB VM for running local models can be available at request from the CSSE Department system admin.  

Using Ollama to run models locally will enable effective model management- utilize a model with Ollama that uses approximately 8B-16B parameter size model in order to use resources available via the VM effectively. 

### 8.2 Unit Testing of MCP Endpoints 

Create a test suite composed of unit tests that mock the Next.js REST endpoints to ensure that the MCP server calls in order to test conformation of the functional invariants defined in 3.1. 

- To test the structural invariant, each MCP tool call should result in one HTTP request sent to the REST endpoint, with successful calls tested as well as calls that require retries before surfaced failure. 
- To test the payload invariant, create unit tests with a variety of arguments to ensure that the corresponding REST requests has the proper schema with no incorrect changing of the values. 
- To test the response invariant, mock custom payloads from the REST API to ensure that the MCP server does not create, add, or remove fabricated data in the responses.

### 8.3 Security Evaluation Involving Integration and Component Testing

To ensure our security architecture functions as expected, a variety of tests, mainly following under integration and component level testing, are recommended.

- Data access for the CHELL is restricted to protect against improper leakage of credentials, experiment results, and other sensitive information. To test this, walk through a full experiment creation process using an agent, with one walk through being happy path and another with a purposefully-thrown crash. Then monitor all generated telemetry streams for sensitive data to see if any such information was logged by the agent.
- The audit log collection is intended to record mutable actions taken by agents interacting with the system (full schema of the collection included in the architecture section). To test this, execute a series of mutable actions (running a new experiment, updating experiment information, deleting experiment information, or any other mutable actions that are implemented), and confirm that the collection writes that occurred to log these actions properly record each. To ensure full coverage beyond just the happy path testing, temporarily scale down the MongoDB pods (which should cause the health check associated with the audit log to fail), and then attempt to make the same mutable actions. Each action should be denied, with degraded-mode error returned as its reason.
 

## 9. Unresolved decisions

## 10. References

- [Utilizing Ollama](https://www.mindstudio.ai/blog/ollama-run-ai-models-locally-claude-code-workflows)
- [RFC7662](https://www.rfc-editor.org/rfc/rfc7662)
- [RFC8628](https://www.rfc-editor.org/rfc/rfc8628)
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25)
- [Github API Request Limit](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2026-03-10)
- [Logging in Python](https://realpython.com/python-logging/)
- [Audit Logs in MongoDB](https://oneuptime.com/blog/post/2026-03-31-mongodb-how-to-implement-audit-logging-in-mongodb/view)


